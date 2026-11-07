"""Authentication and shape validation for inter-agent messages.

Agents run as separate processes and talk over plain HTTP (A2A/JSON-RPC) or
through the blackboard. Nothing in that transport authenticates the sender: any
local process, or a page that can reach the port, can post ``{"run_id", "role",
"instruction"}`` and have a CrewAI role act on it.

Two cheap controls cover that:

* a shared HMAC token (``ATHENA_A2A_SHARED_TOKEN``) over ``run_id`` + body, so a
  message cannot be forged or replayed against a different run;
* an explicit message shape with an allowlist of roles and keys, so a malformed
  or over-broad message is rejected before any role runs.

Token handling: when the variable is unset, :meth:`PeerAuth.token` generates one
with :func:`secrets.token_urlsafe` and writes it back into ``os.environ`` so
child processes inherit the *same* token instead of inventing a different one
and failing every call. The value is never logged; it is per-process (not
shared across machines) unless the operator sets it in ``.env``. Set
``ATHENA_A2A_REQUIRE_SIG=1`` to reject unsigned messages outright.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from athena.blackboard.schemas import GuardrailFlag
from athena.guardrails import env
from athena.guardrails.injection import scan_injection

TOKEN_ENV: Final[str] = "ATHENA_A2A_SHARED_TOKEN"
REQUIRE_SIG_ENV: Final[str] = "ATHENA_A2A_REQUIRE_SIG"
SIG_FIELD: Final[str] = "sig"
SIG_ALGORITHM: Final[str] = "hmac-sha256"

Mode = Literal["p2p", "blackboard"]
MODES: Final[tuple[str, ...]] = ("p2p", "blackboard")

STAGE = "input"
INSTRUCTION_LIMIT = 300
P2P_CONTEXT_LIMIT = 9000
RUN_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")

_RUN_ID_KEYS: Final[tuple[str, ...]] = ("run_id", "role", "instruction", SIG_FIELD, "alg")
P2P_KEYS: Final[tuple[str, ...]] = (*_RUN_ID_KEYS, "context")
BLACKBOARD_KEYS: Final[tuple[str, ...]] = _RUN_ID_KEYS


def _canonical(body: Any) -> bytes:
    """Stable bytes for a body: JSON with sorted keys and no whitespace."""
    if isinstance(body, (dict, list, tuple)):
        return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    if isinstance(body, bytes):
        return body
    return str(body if body is not None else "").encode("utf-8")


def _unsigned(message: dict[str, Any]) -> dict[str, Any]:
    """The part of a message that the signature covers."""
    return {
        key: value
        for key, value in message.items()
        if key not in {SIG_FIELD, "alg"}
    }


def _flag(rule: str, action: str, detail: str) -> GuardrailFlag:
    return GuardrailFlag(stage=STAGE, rule=rule, action=action, detail=detail[:300])


class PeerAuth:
    """HMAC signing and verification for one shared token."""

    def __init__(self, token: str | None = None, *, require_signature: bool | None = None):
        self._token = (token or "").strip()
        self._require = require_signature

    # ---- token -----------------------------------------------------------
    @staticmethod
    def ensure_token() -> str:
        """The process token, generating and exporting one when unset."""
        token = (os.getenv(TOKEN_ENV) or "").strip()
        if token:
            return token
        import secrets as _secrets

        token = _secrets.token_urlsafe(32)
        os.environ[TOKEN_ENV] = token
        return token

    @property
    def token(self) -> str:
        if not self._token:
            self._token = self.ensure_token()
        return self._token

    @property
    def require_signature(self) -> bool:
        if self._require is not None:
            return self._require
        return env.flag(REQUIRE_SIG_ENV, False)

    # ---- signatures ------------------------------------------------------
    def sign(self, run_id: str, body: Any) -> str:
        """Hex HMAC-SHA256 over ``run_id`` + canonical body."""
        message = str(run_id or "").encode("utf-8") + b"\n" + _canonical(body)
        return hmac.new(self.token.encode("utf-8"), message, hashlib.sha256).hexdigest()

    def verify(self, run_id: str, body: Any, signature: str | None) -> bool:
        """Constant-time comparison. An absent or malformed signature is False."""
        if not signature or not isinstance(signature, str):
            return False
        expected = self.sign(run_id, body)
        return hmac.compare_digest(expected, signature.strip().lower())

    def sign_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``message`` carrying its signature."""
        body = _unsigned(message)
        signed = dict(body)
        signed[SIG_FIELD] = self.sign(str(body.get("run_id") or ""), body)
        signed["alg"] = SIG_ALGORITHM
        return signed

    def verify_message(self, message: dict[str, Any]) -> bool:
        """Verify a message produced by :meth:`sign_message`."""
        if not isinstance(message, dict):
            return False
        body = _unsigned(message)
        return self.verify(
            str(body.get("run_id") or ""), body, message.get(SIG_FIELD)  # type: ignore[arg-type]
        )


@dataclass
class MessageVerdict:
    """The result of validating one inter-agent message."""

    ok: bool = False
    errors: list[str] = field(default_factory=list)
    flags: list[GuardrailFlag] = field(default_factory=list)
    mode: str = "blackboard"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "errors": list(self.errors),
            "flags": [flag.model_dump() for flag in self.flags],
        }


def validate_message(
    message: Any,
    allowed_roles: Any,
    *,
    mode: Mode = "blackboard",
    auth: PeerAuth | None = None,
) -> MessageVerdict:
    """Validate one inter-agent message. Never raises except for a bad ``mode``.

    ``mode`` is an explicit enum: ``p2p`` messages must carry an inline
    ``context``, ``blackboard`` messages must not (the blackboard is the data
    path). Unknown keys are rejected rather than ignored.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")

    verdict = MessageVerdict(ok=False, mode=mode)
    roles = {str(role).strip().lower() for role in (allowed_roles or ()) if str(role).strip()}

    if not isinstance(message, dict):
        verdict.errors.append("message must be a JSON object")
        return verdict

    allowed_keys = P2P_KEYS if mode == "p2p" else BLACKBOARD_KEYS
    unknown = sorted(set(message) - set(allowed_keys))
    if unknown:
        verdict.errors.append(f"unexpected field(s): {', '.join(unknown)}")

    run_id = message.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.match(run_id.strip()):
        verdict.errors.append("run_id must be a short token ([A-Za-z0-9_.:-], 1-128)")

    role = message.get("role")
    if not isinstance(role, str) or role.strip().lower() not in roles:
        verdict.errors.append(
            f"role must be one of {sorted(roles) or '(none configured)'}"
        )

    instruction = message.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        verdict.errors.append("instruction must be a non-empty string")
    elif len(instruction) > INSTRUCTION_LIMIT:
        verdict.errors.append(f"instruction exceeds {INSTRUCTION_LIMIT} characters")

    if mode == "p2p":
        context = message.get("context")
        if not isinstance(context, str) or not context.strip():
            verdict.errors.append("p2p message requires a non-empty context string")
        elif len(context) > P2P_CONTEXT_LIMIT:
            verdict.errors.append(f"context exceeds {P2P_CONTEXT_LIMIT} characters")
    elif message.get("context") is not None:
        verdict.errors.append("blackboard message must not carry context")

    signer = auth or PeerAuth()
    signature = message.get(SIG_FIELD)
    body = _unsigned(message)
    if signature:
        if not signer.verify(str(run_id or ""), body, signature if isinstance(signature, str) else ""):
            verdict.errors.append("signature mismatch")
            verdict.flags.append(
                _flag("interagent.signature", "blocked", "A2A message signature mismatch")
            )
    elif signer.require_signature:
        verdict.errors.append("signature required")
        verdict.flags.append(
            _flag("interagent.signature", "blocked", "A2A message carried no signature")
        )
    else:
        verdict.flags.append(
            _flag(
                "interagent.unsigned",
                "warned",
                f"A2A message carried no signature (set {REQUIRE_SIG_ENV}=1 to reject)",
            )
        )

    if isinstance(instruction, str) and instruction:
        for hit in scan_injection(instruction)[:5]:
            verdict.flags.append(
                _flag("interagent.instruction_text", "warned", f"{hit.category}: {hit.excerpt}")
            )
    if mode == "p2p" and isinstance(message.get("context"), str):
        for hit in scan_injection(message.get("context"))[:5]:
            verdict.flags.append(
                _flag(
                    "interagent.context_text",
                    "warned",
                    f"{hit.category}: {hit.excerpt} (data, not instructions)",
                )
            )

    verdict.ok = not verdict.errors
    return verdict


def sign_message(message: dict[str, Any], auth: PeerAuth | None = None) -> dict[str, Any]:
    """Convenience wrapper: sign one message with the process token."""
    return (auth or PeerAuth()).sign_message(message)


def verify_message(message: dict[str, Any], auth: PeerAuth | None = None) -> bool:
    """Convenience wrapper: verify one signed message."""
    return (auth or PeerAuth()).verify_message(message)
