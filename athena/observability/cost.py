"""Price a model call against the bundled, cited list-price table.

The table lives in ``data/pricing.json`` (override with ``ATHENA_PRICING``) and
records *when* it was read and *where* each number came from. Two rules keep this
honest:

- a model that does not match an entry is **unpriced**: ``cost_usd`` is ``None``
  and ``cost_basis`` is ``unpriced``. Nothing is interpolated, averaged or
  guessed, and no default rate is invented;
- the money is a list-price *estimate*: batch discounts, cached-input rates and
  long-context tiers are not modelled, so it is never presented as a bill.

``estimate_cost()`` is pure (no network, no clock) and never raises for a bad
model string; callers get an estimate with a reason instead.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PRICING_ENV = "ATHENA_PRICING"
DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[2] / "data" / "pricing.json"

LIST_PRICE = "list_price"
UNPRICED = "unpriced"

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class CostEstimate:
    """One call's price, or the reason there is no price."""

    model: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    cost_basis: str
    currency: str = "USD"
    unit: str = "usd_per_million_tokens"
    matched_pattern: str | None = None
    provider: str | None = None
    priced_as_of: str = ""
    source: str = ""
    reason: str = ""

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def pricing_path(path: str | Path | None = None) -> Path:
    """Where the table is read from: argument, then env, then the repo copy."""
    if path is not None:
        return Path(path)
    return Path(os.getenv(PRICING_ENV) or DEFAULT_PRICING_PATH)


def load_pricing(path: str | Path | None = None) -> dict[str, Any]:
    """The parsed table. Cached by path and mtime, so an edit is picked up."""
    target = pricing_path(path)
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return {"models": [], "as_of": "", "currency": "USD",
                "unit": "usd_per_million_tokens",
                "note": f"pricing table not found at {target}",
                "source": "", "entries": 0}
    cached = _CACHE.get(str(target))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # a broken table must not break a request
        return {"models": [], "as_of": "", "currency": "USD",
                "unit": "usd_per_million_tokens",
                "note": f"pricing table unreadable: {type(exc).__name__}",
                "source": "", "entries": 0}
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        data = {"models": []}
    data.setdefault("as_of", "")
    data.setdefault("currency", "USD")
    data.setdefault("unit", "usd_per_million_tokens")
    data.setdefault("note", "")
    data.setdefault("source", "")
    data["entries"] = len(data["models"])
    _CACHE[str(target)] = (mtime, data)
    return data


def pricing_meta(path: str | Path | None = None) -> dict[str, Any]:
    """What the UI/API states about the price basis."""
    table = load_pricing(path)
    return {
        "as_of": table.get("as_of") or "",
        "currency": table.get("currency") or "USD",
        "unit": table.get("unit") or "usd_per_million_tokens",
        "note": table.get("note") or "",
        "source": table.get("source") or "",
        "sources": list(table.get("sources") or []),
        "entries": int(table.get("entries") or 0),
        # Ids that were checked and left unpriced on purpose, with the reason.
        # They are never matched: only ``models`` entries price a call.
        "unlisted": list(table.get("unlisted") or []),
        "unlisted_note": table.get("unlisted_note") or "",
        "table": str(pricing_path(path)),
        "unknown_model_basis": UNPRICED,
    }


def _candidates(model: str) -> list[str]:
    """Whole id first, then with leading route segments stripped."""
    text = str(model or "").strip().lower()
    if not text:
        return []
    parts = [part for part in text.replace(":", "/").split("/") if part]
    candidates = [text]
    for start in range(1, len(parts)):
        candidate = "/".join(parts[start:])
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def match_model(model: str | None, path: str | Path | None = None) -> dict[str, Any] | None:
    """The table entry for ``model``, or None when it is not in the table."""
    table = load_pricing(path)
    entries = [entry for entry in table.get("models") or [] if isinstance(entry, dict)]
    candidates = _candidates(model or "")
    if not candidates:
        return None
    # Exact ids win over globs, whatever the file order is.
    for entry in entries:
        if str(entry.get("match") or "").strip().lower() in candidates:
            return entry
    for entry in entries:
        pattern = str(entry.get("match") or "").strip().lower()
        if not pattern or not any(char in pattern for char in "*?["):
            continue
        if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
            return entry
    return None


def _as_tokens(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def estimate_cost(
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    path: str | Path | None = None,
) -> CostEstimate:
    """Price one call. Unknown model => ``cost_usd=None``, basis ``unpriced``."""
    table = load_pricing(path)
    meta = {
        "currency": table.get("currency") or "USD",
        "unit": table.get("unit") or "usd_per_million_tokens",
        "priced_as_of": table.get("as_of") or "",
    }
    prompt = _as_tokens(tokens_in)
    completion = _as_tokens(tokens_out)

    if not str(model or "").strip():
        return CostEstimate(
            model=None, tokens_in=prompt, tokens_out=completion, cost_usd=None,
            cost_basis=UNPRICED, matched_pattern=None, reason="no model recorded",
            **meta,
        )

    entry = match_model(model, path)
    if entry is None:
        return CostEstimate(
            model=str(model), tokens_in=prompt, tokens_out=completion, cost_usd=None,
            cost_basis=UNPRICED, matched_pattern=None,
            reason=f"model {model!r} is not in the bundled price table",
            **meta,
        )

    try:
        rate_in = float(entry.get("input_per_mtok"))
        rate_out = float(entry.get("output_per_mtok"))
    except (TypeError, ValueError):
        return CostEstimate(
            model=str(model), tokens_in=prompt, tokens_out=completion, cost_usd=None,
            cost_basis=UNPRICED, matched_pattern=str(entry.get("match") or "") or None,
            provider=str(entry.get("provider") or "") or None,
            reason="price table entry has no usable rates",
            **meta,
        )

    cost = (prompt / 1_000_000.0) * rate_in + (completion / 1_000_000.0) * rate_out
    return CostEstimate(
        model=str(model),
        tokens_in=prompt,
        tokens_out=completion,
        cost_usd=round(cost, 9),
        cost_basis=LIST_PRICE,
        matched_pattern=str(entry.get("match") or "") or None,
        provider=str(entry.get("provider") or "") or None,
        source=str(entry.get("source") or table.get("source") or ""),
        reason="",
        **meta,
    )


def sum_costs(estimates: list[CostEstimate]) -> dict[str, Any]:
    """Add up estimates, keeping "unpriced" visible instead of treating it as 0."""
    priced = [item.cost_usd for item in estimates if item.cost_usd is not None]
    unpriced = [item for item in estimates if item.cost_usd is None]
    return {
        "cost_usd": round(sum(priced), 9) if priced else None,
        "cost_basis": LIST_PRICE if priced and not unpriced else (
            UNPRICED if not priced else "partly_unpriced"
        ),
        "priced_calls": len(priced),
        "unpriced_calls": len(unpriced),
        "unpriced_models": sorted({item.model for item in unpriced if item.model}),
    }
