"""Deterministic finding-count requirements for research prompts.

The UI accepts free text, so the prompt layer extracts an explicit count when
the user asks for "three key changes", "five findings", and similar phrasing.
It does not enforce a source count by fabricating evidence; it tells the writer
and critic what a complete answer must contain.
"""

from __future__ import annotations

import re

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_COUNT = re.compile(
    r"\b(" + "|".join(_NUMBER_WORDS) + r"|\d{1,2})\s+"
    r"(?:(?:key|major|important|main|practical|notable|concrete|specific|critical|significant)\s+)?"
    r"(?:changes?|findings?|items?|reasons?|differences?)\b",
    re.IGNORECASE,
)


def required_findings(question: str) -> int:
    """Return the explicit finding count, or 0 when no count was requested."""
    match = _COUNT.search(question or "")
    if not match:
        return 0
    token = match.group(1).lower()
    count = _NUMBER_WORDS.get(token)
    if count is None:
        try:
            count = int(token)
        except ValueError:
            return 0
    return count if 1 <= count <= 10 else 0


def finding_requirement(question: str) -> str:
    """Prompt text requiring one primary source and impact per finding."""
    count = required_findings(question)
    if not count:
        return ""
    return (
        f"The question requires exactly {count} findings. For each finding, provide "
        "a distinct primary-source URL, a specific explanation grounded in that "
        "source, and a practical impact. Do not reuse the same primary source for "
        "two findings. If the evidence cannot support a finding, preserve the "
        "requested count and state the evidence gap explicitly; never invent a "
        "source, URL, explanation, or impact to fill the count."
    )
