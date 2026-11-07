"""Library status, a safe single-flight RAG rebuild, and a read-only document
preview for the Documents view."""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from athena.api.guards import guard
from athena.tools import settings as tool_settings
from athena.tools.rag.status import chunk_count, document_preview, document_rows, index_exists

router = APIRouter(prefix="/api/settings", tags=["settings"])

# The preview lives under /api/library so its URL names what it returns; it is
# read-only and capped, and shares this module's guarded, model-free reads.
documents_router = APIRouter(prefix="/api/library", tags=["library"])

REPO_ROOT = Path(__file__).resolve().parents[2]
INGEST_SCRIPT = "scripts/ingest_nasa.py"
JOB_TIMEOUT_S = 1800
PREVIEW_MAX_CHUNKS = 12
PREVIEW_MAX_CHARS = 12000
PREVIEW_MAX_ID_LENGTH = 300

_LOCK = threading.Lock()
_JOB: dict[str, Any] = {
    "state": "idle",
    "started_at": "",
    "finished_at": "",
    "exit_code": None,
    "tail": "",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _corpus_files() -> list[dict[str, Any]]:
    corpus = tool_settings.corpus_dir()
    if not corpus.exists():
        return []
    rows = []
    for path in sorted(corpus.glob("*.pdf")):
        try:
            rows.append({"name": path.name, "bytes": path.stat().st_size})
        except OSError:
            continue
    return rows


def _status() -> dict[str, Any]:
    with _LOCK:
        job = dict(_JOB)
    return {
        "rag": {
            "corpus_dir": str(tool_settings.corpus_dir()),
            "index_dir": str(tool_settings.chroma_path()),
            "collection": tool_settings.collection_name(),
            "embedding_provider": tool_settings.embedding_provider(),
            "chunks": chunk_count(),
            "index_exists": index_exists(),
            "corpus_files": _corpus_files(),
            "documents": document_rows(),
        },
        "search": {
            "provider": tool_settings.search_provider(),
            "endpoint": tool_settings.searxng_instance(),
            "note": "Chat uses the selected profile's search provider; research uses the active profile.",
        },
        "reindex": job,
    }


def _run_reindex() -> None:
    global _JOB
    try:
        result = subprocess.run(
            [sys.executable, INGEST_SCRIPT],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=JOB_TIMEOUT_S,
            check=False,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        tail = "\n".join(output.splitlines()[-40:])[-4000:]
        if result.returncode == 0:
            from athena.tools.rag.search import clear_index_cache

            clear_index_cache()
        with _LOCK:
            _JOB = {
                "state": "done" if result.returncode == 0 else "failed",
                "started_at": _JOB.get("started_at", ""),
                "finished_at": _now(),
                "exit_code": result.returncode,
                "tail": tail,
            }
    except subprocess.TimeoutExpired:
        with _LOCK:
            _JOB = {
                "state": "failed",
                "started_at": _JOB.get("started_at", ""),
                "finished_at": _now(),
                "exit_code": None,
                "tail": f"index rebuild timed out after {JOB_TIMEOUT_S}s",
            }
    except Exception as exc:  # noqa: BLE001 - the job must end with a status
        with _LOCK:
            _JOB = {
                "state": "failed",
                "started_at": _JOB.get("started_at", ""),
                "finished_at": _now(),
                "exit_code": None,
                "tail": f"{type(exc).__name__}: {exc}",
            }


@router.get("/library")
def library(request: Request) -> dict:
    guard(request)
    return _status()


@router.post("/library/reindex")
def reindex(request: Request) -> dict:
    guard(request)
    global _JOB
    with _LOCK:
        if _JOB.get("state") == "running":
            raise HTTPException(status_code=409, detail="an index rebuild is already running")
        _JOB = {
            "state": "running",
            "started_at": _now(),
            "finished_at": "",
            "exit_code": None,
            "tail": "",
        }
    thread = threading.Thread(target=_run_reindex, daemon=True)
    thread.start()
    return {"started": True, "state": "running"}


@documents_router.get("/documents/{doc_id}")
def document(doc_id: str, request: Request) -> dict:
    """A capped, read-only preview of one indexed document's chunk text.

    It reads the chunk text that is already stored in the local index: nothing
    is re-rendered, re-ingested or written, and the response is capped by both
    chunk count and characters. An unknown or missing document is a 404 rather
    than an empty preview, so the drawer can say why there is nothing to show.
    """
    guard(request)
    wanted = (doc_id or "").strip()
    if not wanted or len(wanted) > PREVIEW_MAX_ID_LENGTH:
        raise HTTPException(status_code=404, detail="no such document")
    preview = document_preview(
        wanted, max_chunks=PREVIEW_MAX_CHUNKS, max_chars=PREVIEW_MAX_CHARS
    )
    if preview is None:
        raise HTTPException(
            status_code=404,
            detail=f"no indexed text for {wanted!r}",
        )
    return preview
