"""Tokenization and stop words shared by hybrid search and hashing embeddings.

Ported from the old Athena `rag/text.py`. Filtering stop words is what lets the
keyword half of hybrid search and the offline `hashing` embeddings agree on
which tokens actually carry signal.
"""

from __future__ import annotations

import re

STOP_WORDS = set(
    """
    the a an is are was were be been being to of in for on at by with from
    and or if then else when while as it this that those these i you he she
    we they my your our their me him her us them
    """.split()
)

_WORD = re.compile(r"[A-Za-z0-9_\-]+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, minus stop words and single characters."""
    if not isinstance(text, str):
        return []
    return [
        token
        for token in _WORD.findall(text.lower())
        if len(token) > 1 and token not in STOP_WORDS
    ]


def token_set(text: str) -> set[str]:
    return set(tokenize(text))
