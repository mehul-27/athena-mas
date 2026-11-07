"""Attribute each benchmark attempt to one authorised provider account.

Why an allocation rather than rotation
--------------------------------------
Two authorised Groq accounts are used for this benchmark. The rule the operator
set is strict, and this module exists to make it impossible to break silently:

* the account for a run is decided **before the batch starts**, from a recorded
  plan, and never re-decided in response to a rate-limit or quota error. That
  would be rotation to evade enforcement, which is not permitted;
* the account is fixed for the whole attempt. A retry of the same run key gets
  the same account, so an attempt is never split across credentials;
* every attempt records which account ran it, by a **safe identifier** - a
  label plus a non-reversible fingerprint. No key material is stored.

The two accounts must be identical in every way that affects the science: same
provider, same endpoint, same model id. :func:`verify_same_conditions` refuses a
batch where that is not true, because results from different models are not
comparable and must not be pooled into one benchmark.

If an account is quota-limited the honest response is to stop and resume after
the provider's window resets - not to move the run to the other account.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


def account_fingerprint(key: str) -> str:
    """A stable, non-reversible identifier for a credential.

    Truncated SHA-256 only. It is enough to distinguish two accounts and to
    check that the same account was used for two runs, and cannot be turned
    back into the key.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


@dataclass(frozen=True)
class Account:
    """One authorised provider account, described without its secret."""

    label: str
    key: str
    api_base: str
    model: str

    @property
    def account_id(self) -> str:
        return f"{self.label}#{account_fingerprint(self.key)}"

    def public(self) -> dict[str, str]:
        """What may be written to a record or a report.

        Deliberately only the three attribution fields the record schema
        declares. ``api_base`` and ``model`` are recorded elsewhere (the batch
        provenance block) and must not be duplicated here: the record model
        forbids unknown fields, so an extra key is a validation failure that
        would lose the whole run.
        """
        return {
            "account_label": self.label,
            "account_fingerprint": account_fingerprint(self.key),
            "account_id": self.account_id,
        }


class AccountError(RuntimeError):
    """The account set cannot be used for a comparable benchmark."""


def verify_same_conditions(accounts: Sequence[Account]) -> None:
    """Refuse a batch whose accounts would run different science.

    Same provider endpoint, same model id. This is the guarantee that a pooled
    result is one experiment rather than two spliced together.
    """
    if len(accounts) < 2:
        return
    bases = {account.api_base.rstrip("/") for account in accounts}
    if len(bases) != 1:
        raise AccountError(
            "accounts use different endpoints "
            f"({sorted(bases)}); results would not be comparable"
        )
    models = {account.model for account in accounts}
    if len(models) != 1:
        raise AccountError(
            f"accounts use different models ({sorted(models)}); "
            "results would not be comparable"
        )
    fingerprints = {account_fingerprint(account.key) for account in accounts}
    if len(fingerprints) != len(accounts):
        raise AccountError(
            "two configured accounts hold the same credential; "
            "attribution would be meaningless"
        )


def allocation_for(
    run_keys: Iterable[str], accounts: Sequence[Account]
) -> dict[str, str]:
    """Deterministic account label per run key.

    Round-robin over a **sorted** key list, so the same plan always produces the
    same allocation no matter the iteration order and a resumed batch keeps the
    account each run was originally assigned. The allocation is a property of
    the plan, not of runtime conditions.
    """
    if not accounts:
        return {}
    keys = sorted(run_keys)
    return {
        key: accounts[index % len(accounts)].label
        for index, key in enumerate(keys)
    }


def header_value(account: Account, api_base: str) -> str:
    """The value for the child's ``LLM_API_BASE``/``LLM_MODEL`` pair.

    Kept here so the executor and the verifier agree on exactly two strings.
    """
    return api_base or account.api_base


def summarise_usage(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-account attempt and token totals, for the technical appendix.

    A record with no ``account_label`` (an older run, or a batch that used a
    single undeclared credential) is grouped under ``"(unattributed)"`` rather
    than guessed.
    """
    totals: dict[str, dict[str, Any]] = {}
    for record in records:
        label = str(record.get("account_label") or "(unattributed)")
        bucket = totals.setdefault(
            label,
            {
                "account_label": label,
                "account_fingerprint": record.get("account_fingerprint") or "",
                "attempts": 0,
                "completed": 0,
                "model_started": 0,
                "tokens_in": 0,
                "tokens_out": 0,
            },
        )
        bucket["attempts"] += 1
        if record.get("terminal_state") == "completed":
            bucket["completed"] += 1
        if (record.get("model_calls") or 0) >= 1 or (record.get("tokens_in") or 0) > 0:
            bucket["model_started"] += 1
        bucket["tokens_in"] += int(record.get("tokens_in") or 0)
        bucket["tokens_out"] += int(record.get("tokens_out") or 0)
    return dict(sorted(totals.items()))


__all__ = [
    "Account",
    "AccountError",
    "account_fingerprint",
    "allocation_for",
    "summarise_usage",
    "verify_same_conditions",
]
