"""Small URL cleanup used before handing a URL to ``fetch_page``.

The model sometimes copies a Markdown link instead of the plain URL. The fetch
caller normalizes that before the MCP tool sees the argument; the tool still
performs all scheme/destination validation independently.
"""

from __future__ import annotations

import re

_MARKDOWN_LINK = re.compile(
    r"^\s*\[[^\]]*\]\(\s*<?(https?://[^)\s>]+)>?"
    r"(?:\s+[\"'][^\"']*[\"'])?\s*\)\s*$",
    re.IGNORECASE,
)


def plain_http_url(value: object) -> str:
    """Return a plain HTTP(S) URL; unwrap Markdown links and angle brackets."""
    text = str(value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    match = _MARKDOWN_LINK.match(text)
    if match:
        return match.group(1).strip()
    return text
