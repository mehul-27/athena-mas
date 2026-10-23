"""PDF extraction and sentence-aware chunking.

Ported from the old Athena `rag/ingestion.py`: pypdf for text, chunking on
sentence boundaries with a hard split for runaway sentences and an overlap
tail, plus a verified page map so a hit can cite a page number instead of
guessing one.
"""

from __future__ import annotations

import re
from pathlib import Path

from athena.tools import settings

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")


def extract_pdf_pages(path: str | Path) -> list[str]:
    """Text of each page, in order. Empty list when the file cannot be read."""
    try:
        from pypdf import PdfReader

        return [(page.extract_text() or "") for page in PdfReader(str(path)).pages]
    except Exception:
        return []


def page_map(pages: list[str]) -> tuple[str, list[tuple[int, int, int]]]:
    """Document text plus ``(page_number, start, end)`` ranges into that text.

    The ranges are built from the same join that produced the text, so a page
    number is looked up rather than inferred from chunk order.
    """
    if not pages:
        return "", []
    raw = "\n".join(pages)
    lead = len(raw) - len(raw.lstrip())
    text = raw.strip()

    ranges: list[tuple[int, int, int]] = []
    cursor = 0
    for number, page in enumerate(pages, start=1):
        start = max(0, cursor - lead)
        end = min(len(text), max(0, cursor + len(page) - lead))
        if end > start:
            ranges.append((number, start, end))
        cursor += len(page) + 1
    return text, ranges


def page_for_offset(ranges: list[tuple[int, int, int]], offset: int) -> int | None:
    """The page an offset falls on, or None when there is no page map."""
    if not ranges:
        return None
    for number, start, end in ranges:
        if start <= offset < end:
            return number
    preceding = [number for number, start, _ in ranges if start <= offset]
    return preceding[-1] if preceding else ranges[0][0]


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_SPLIT.finditer(text):
        spans.append((cursor, match.start()))
        cursor = match.end()
    spans.append((cursor, len(text)))

    out: list[tuple[int, int]] = []
    for start, end in spans:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            out.append((start, end))
    return out


def chunk_spans(
    text: str, size: int = 1000, overlap: int = 200
) -> list[tuple[str, int, int]]:
    """Chunks as ``(text, start, end)`` spans of the source text."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [(text, 0, len(text))]

    spans = _sentence_spans(text)
    chunks: list[tuple[str, int, int]] = []
    current: list[tuple[int, int]] = []
    current_len = 0
    step = max(1, size - overlap)

    def flush() -> None:
        pieces = [text[span[0] : span[1]] for span in current]
        chunks.append((" ".join(pieces), current[0][0], current[-1][1]))

    for span in spans:
        length = span[1] - span[0]

        if length > size:
            # A single sentence longer than a chunk: hard split it, keep the
            # overlap so a phrase crossing the cut is still findable.
            if current:
                flush()
                current, current_len = [], 0
            start = span[0]
            for offset in range(0, length, step):
                piece = text[start + offset : start + offset + size]
                if piece:
                    chunks.append((piece, start + offset, start + offset + len(piece)))
            continue

        if current_len + length + 1 > size and current:
            flush()
            tail: list[tuple[int, int]] = []
            tail_len = 0
            for previous in reversed(current):
                piece_len = previous[1] - previous[0]
                if tail_len + piece_len + 1 > overlap:
                    break
                tail.insert(0, previous)
                tail_len += piece_len + 1
            current, current_len = tail, tail_len

        current.append(span)
        current_len += length + 1

    if current:
        flush()
    return chunks or [(text, 0, len(text))]


def document_chunks(
    path: str | Path, size: int | None = None, overlap: int | None = None
) -> list[tuple[str, int | None]]:
    """``(chunk_text, page)`` pairs for one PDF. Empty when unreadable."""
    size = size or settings.chunk_size()
    overlap = overlap or settings.chunk_overlap()
    text, ranges = page_map(extract_pdf_pages(path))
    if not text.strip():
        return []
    return [
        (chunk, page_for_offset(ranges, start))
        for chunk, start, _ in chunk_spans(text, size, overlap)
    ]


def text_chunks(
    text: str, size: int | None = None, overlap: int | None = None
) -> list[tuple[str, int | None]]:
    """``(chunk_text, None)`` pairs for plain text. No pages to verify."""
    size = size or settings.chunk_size()
    overlap = overlap or settings.chunk_overlap()
    return [(chunk, None) for chunk, _, _ in chunk_spans(text, size, overlap)]
