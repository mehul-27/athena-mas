"""Read-only evaluation results for the Evaluation view.

Everything here is derived from the harness output that already exists under
``evaluation/results/<batch>/``: ``raw.jsonl``, ``run_meta.json`` and the
``charts/`` directory written by ``python -m evaluation.analyze``.

Three rules:

* **Nothing is written.** The results tree belongs to the evaluation runner and
  may be being written to while this service is running, so every read here is
  read-only and a half-written line is reported as a problem instead of being
  guessed at.
* **Nothing is invented.** An empty results tree answers with an empty list, and
  a batch with no scorable record reports no numbers rather than zeros.
* **Everything is capped.** Batches, charts and the chart file path are bounded
  so a query cannot walk out of the results tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from athena.api.guards import guard
from evaluation import analyze, report, schema

router = APIRouter(prefix="/api/eval", tags=["eval"])

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "evaluation" / "results"
DATASET_PATH = REPO_ROOT / "evaluation" / "dataset.json"
CHART_SUFFIXES = {".png", ".svg"}
CHART_MEDIA = {".png": "image/png", ".svg": "image/svg+xml"}
MAX_BATCHES = 20
MAX_CHARTS = 12
MAX_PROBLEMS = 40


def results_root() -> Path:
    """The results tree this service reads. Overridable in tests."""
    return RESULTS_ROOT


def dataset_path() -> Path:
    """The committed dataset the records are scored against."""
    return DATASET_PATH


def _raw_records(directory: Path, problems: list[str]) -> list[dict[str, Any]]:
    """Read ``raw.jsonl`` without interpreting it. Never raises."""
    raw = directory / "raw.jsonl"
    records: list[dict[str, Any]] = []
    if not raw.is_file():
        problems.append(f"{directory.name}: raw.jsonl is missing")
        return records
    try:
        lines = raw.read_text(encoding="utf-8").splitlines()
    except OSError as exc:  # noqa: BLE001 - an unreadable batch is a problem
        problems.append(f"{directory.name}: raw.jsonl unreadable ({type(exc).__name__})")
        return records
    for number, line in enumerate(lines, start=1):
        body = line.strip()
        if not body:
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            problems.append(f"{directory.name}: raw.jsonl line {number} is not valid JSON")
            continue
        if isinstance(payload, dict):
            records.append(payload)
        else:
            problems.append(f"{directory.name}: raw.jsonl line {number} is not a record")
    return records


def _read_meta(directory: Path, problems: list[str]) -> dict[str, Any]:
    meta_path = directory / "run_meta.json"
    if not meta_path.is_file():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        problems.append(f"{directory.name}: run_meta.json is not valid JSON")
        return {}
    return payload if isinstance(payload, dict) else {}


def _analysis(directory: Path) -> tuple[analyze.Analysis, list[str]]:
    """Load one batch through the harness' own loader.

    Without a valid dataset the records can still be counted, they just cannot
    be scored: that is reported instead of silently scoring nothing.
    """
    problems: list[str] = []
    dataset = None
    try:
        dataset = schema.load_dataset(dataset_path())
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        problems.append(f"dataset.json is not valid: {type(exc).__name__}")
    if dataset is not None:
        analysis = analyze.load_analysis(directory, dataset)
        return analysis, problems + list(analysis.problems)

    analysis = analyze.Analysis(results_dir=directory)
    analysis.records = _raw_records(directory, problems)
    analysis.meta = _read_meta(directory, problems)
    return analysis, problems


def _batches() -> list[Path]:
    """Batches that actually recorded something, newest first."""
    found = report.find_results_dirs(results_root())
    return list(reversed(found))[:MAX_BATCHES]


def _batch(run: str) -> Path:
    """Resolve one batch name to its directory without leaving the results tree."""
    wanted = (run or "").strip()
    if not wanted or Path(wanted).name != wanted or wanted in {".", ".."}:
        raise HTTPException(status_code=404, detail="no such evaluation batch")
    for directory in _batches():
        if directory.name == wanted:
            return directory
    raise HTTPException(status_code=404, detail="no such evaluation batch")


def _number(value: Any, digits: int = 4) -> float | None:
    """A recorded number, or ``None``. A missing value is never a zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), digits)


def _pattern_rows(analysis: analyze.Analysis) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, group in analyze.group_by_pattern(analysis).items():
        stats = analyze.summarise_group(group)
        hits, _total, share = stats["success"]
        rows.append(
            {
                "pattern": name,
                "runs": stats["runs"],
                "scored": stats["scored"],
                "successes": hits,
                "success_rate": _number(share),
                "latency_median_s": _number(stats["latency"].median),
                "tokens_median": _number(stats["tokens"].median, 1),
                "cost_median_usd": _number(stats["cost"].median, 6),
                "citation_precision_median": _number(stats["citation_precision"].median),
            }
        )
    return rows


def _charts(directory: Path) -> list[dict[str, str]]:
    """The charts this batch actually has, as guarded hrefs."""
    charts_dir = directory / "charts"
    if not charts_dir.is_dir():
        return []
    rows: list[dict[str, str]] = []
    for path in sorted(charts_dir.iterdir()):
        if len(rows) >= MAX_CHARTS:
            break
        if not path.is_file() or path.suffix.lower() not in CHART_SUFFIXES:
            continue
        rows.append(
            {
                "name": path.stem.replace("_", " "),
                "file": path.name,
                "href": "/api/eval/chart?run=" + quote(directory.name)
                        + "&name=" + quote(path.name),
            }
        )
    return rows


def _chart_path(directory: Path, name: str) -> Path:
    """One chart file inside ``<batch>/charts``; anything else is a 404."""
    candidate = (name or "").strip()
    if not candidate or Path(candidate).name != candidate:
        raise HTTPException(status_code=404, detail="no such chart")
    if Path(candidate).suffix.lower() not in CHART_SUFFIXES:
        raise HTTPException(status_code=404, detail="no such chart")
    charts_dir = (directory / "charts").resolve()
    target = (charts_dir / candidate).resolve()
    try:
        target.relative_to(charts_dir)
    except ValueError:  # pragma: no cover - the name check already refuses
        raise HTTPException(status_code=404, detail="no such chart") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="no such chart")
    return target


@router.get("/runs")
def runs(request: Request) -> dict:
    """Every batch that recorded a run, with its real record count."""
    guard(request)
    batches: list[dict[str, Any]] = []
    for directory in _batches():
        problems: list[str] = []
        meta = _read_meta(directory, problems)
        records = _raw_records(directory, problems)
        batches.append(
            {
                "id": directory.name,
                "runs": len(records),
                "started_at": str(meta.get("started_at") or ""),
                "ended_at": str(meta.get("ended_at") or ""),
                "model": str(meta.get("model") or ""),
                "backend": str(meta.get("backend") or ""),
                "plan_id": str(meta.get("plan_id") or ""),
                "problems": problems[:MAX_PROBLEMS],
            }
        )
    return {"root": str(results_root()), "runs": batches}


@router.get("/summary")
def summary(request: Request, run: str = "") -> dict:
    """One batch, summarised from the records it holds."""
    guard(request)
    if run:
        directory = _batch(run)
    else:
        batches = _batches()
        if not batches:
            raise HTTPException(status_code=404, detail="no evaluation results are present")
        directory = batches[0]

    analysis, problems = _analysis(directory)
    meta = analysis.meta or {}
    return {
        "run": directory.name,
        "records": len(analysis.records),
        "scored": len(analysis.scores),
        "patterns": _pattern_rows(analysis),
        "charts": _charts(directory),
        "problems": problems[:MAX_PROBLEMS],
        "note": analyze.NO_SIGNIFICANCE_NOTE,
        "meta": {
            "plan_id": str(meta.get("plan_id") or ""),
            "backend": str(meta.get("backend") or ""),
            "model": str(meta.get("model") or ""),
            "started_at": str(meta.get("started_at") or ""),
            "ended_at": str(meta.get("ended_at") or ""),
            "stopped_reason": str(meta.get("stopped_reason") or ""),
        },
    }


@router.get("/chart")
def chart(request: Request, run: str = "", name: str = "") -> FileResponse:
    """Serve one chart that ``evaluation.analyze`` wrote for one batch."""
    guard(request)
    directory = _batch(run)
    target = _chart_path(directory, name)
    return FileResponse(target, media_type=CHART_MEDIA[target.suffix.lower()])
