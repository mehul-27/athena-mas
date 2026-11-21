"""Standalone, print-ready research reports backed by the run blackboard."""

from __future__ import annotations

import html
from collections import Counter
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from athena.api import replay
from athena.api.guards import guard
from athena.blackboard import Blackboard
from athena.guardrails.secrets import redact_tree

router = APIRouter()


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _link(value: str) -> str:
    """Only turn ordinary HTTP(S) sources into clickable links."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return _escape(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return _escape(value)
    return f'<a href="{_escape(value)}" target="_blank" rel="noopener noreferrer">{_escape(value)}</a>'


def render_visual_report(
    *, run_id: str, question: str, report: dict, critique: dict | None,
    summary: dict, evidence: list[dict],
) -> str:
    """Render a self-contained editorial report with browser PDF printing."""
    citations = report.get("citations") or []
    points = report.get("key_points") or []
    sources = []
    for index, citation in enumerate(citations, 1):
        ref = str(citation.get("source_ref") or "")
        matching = next((item for item in evidence if item.get("source_ref") == ref), {})
        passage = citation.get("quote_or_summary") or matching.get("content") or ""
        sources.append(
            '<article class="source">'
            f'<span class="source-number">{index:02}</span>'
            f'<div><h3>{_link(ref or "Unspecified source")}</h3>'
            f'<p>{_escape(passage)}</p>'
            f'<small>{_escape(citation.get("source_type") or "source")}</small></div></article>'
        )

    point_markup = "".join(f"<li>{_escape(point)}</li>" for point in points)
    source_markup = "".join(sources) or '<p class="muted">No citations were recorded.</p>'
    review = critique.get("approved") if critique else None
    approval = {True: "Approved", False: "Review needed"}.get(review, "Review unavailable")
    confidence = report.get("confidence")
    confidence_text = f"{float(confidence):.0%}" if isinstance(confidence, (int, float)) else "Unavailable"
    outcome = str(summary.get("outcome") or "")
    outcome_markup = f'<span class="pill">{_escape(outcome.title())}</span>' if outcome else ""
    source_counts = Counter(str(citation.get("source_type") or "Other") for citation in citations)
    breakdown = "".join(
        f'<li><span>{_escape(kind.replace("_", " ").title())}</span>'
        f'<meter aria-label="{_escape(kind)} citations" min="0" max="{len(citations)}" value="{count}"></meter>'
        f'<span>{count}</span></li>'
        for kind, count in source_counts.most_common()
    )
    breakdown_markup = f'<div class="breakdown"><h3>Citations by source type</h3><ul>{breakdown}</ul></div>' if breakdown else ""
    guardrail_flags = report.get("guardrail_flags") or []
    flags_markup = "".join(
        f"<li>{_escape(flag.get('detail') or flag.get('rule') or 'Review recorded')}</li>"
        for flag in guardrail_flags
    )
    review_markup = (
        f'<section class="section"><p class="eyebrow">REVIEW NOTES</p><ul>{flags_markup}</ul></section>'
        if flags_markup else ""
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(question[:100] or 'Athena Research Report')}</title>
<style>
:root{{
  color-scheme:light;
  --ink:#20221f;--muted:#686b64;--paper:#f5f2ea;--card:#fffdf8;--rule:#d8d2c5;--accent:#a64e37;
  --toolbar:#f5f2eaef;--pill:#eeeadf;--pill-ink:#494a43;--shadow:#39301a0c;
  --serif:Georgia,'Iowan Old Style',serif;
}}
@media screen{{
  :root:not([data-theme="light"]){{
    color-scheme:dark;
    --ink:#e9e4db;--muted:#b1aba0;--paper:#121110;--card:#1c1a18;--rule:#403933;--accent:#e2a38b;
    --toolbar:#121110f2;--pill:#302823;--pill-ink:#d9c9ba;--shadow:#0005;
  }}
}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.72 system-ui,-apple-system,'Segoe UI',sans-serif}}
.toolbar{{position:sticky;top:0;display:flex;justify-content:flex-end;gap:8px;padding:14px max(calc((100vw - 940px)/2),24px);background:var(--toolbar);border-bottom:1px solid var(--rule);z-index:2}}
button{{border:1px solid transparent;background:var(--ink);color:var(--paper);border-radius:999px;padding:10px 16px;font:inherit;cursor:pointer}}
#report-theme{{background:var(--card);color:var(--ink);border-color:var(--rule)}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:3px}}
main{{max-width:940px;margin:5rem auto;padding:0 28px 5rem}} .hero{{padding:42px 48px;background:var(--card);border:1px solid var(--rule);border-top:5px solid var(--accent);box-shadow:0 18px 50px var(--shadow)}}
.eyebrow{{font-size:11px;letter-spacing:.16em;color:var(--accent);font-weight:750}} h1,h2{{font-family:var(--serif);font-weight:500;line-height:1.14}} h1{{font-size:clamp(34px,6vw,62px);margin:14px 0 24px;max-width:760px}} h2{{font-size:32px;margin:0 0 22px}} h3{{font-size:15px;margin:0 0 6px;overflow-wrap:anywhere}} .meta{{display:flex;gap:10px;flex-wrap:wrap}} .pill{{background:var(--pill);border-radius:999px;padding:5px 12px;font-size:12px;color:var(--pill-ink)}}
.section{{margin-top:28px;padding:36px 44px;background:var(--card);border:1px solid var(--rule)}} .answer{{font:21px/1.65 var(--serif);white-space:pre-wrap}} li{{margin:.65em 0}} .source{{display:grid;grid-template-columns:42px 1fr;gap:14px;padding:18px 0;border-top:1px solid var(--rule)}} .source-number{{font:20px var(--serif);color:var(--accent)}} .source p{{margin:6px 0;color:var(--muted);white-space:pre-wrap}} a{{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:3px;overflow-wrap:anywhere}} .muted,small,footer{{color:var(--muted)}} footer{{max-width:940px;margin:-3rem auto 3rem;padding:0 28px;font-size:12px}}
@media(max-width:600px){{main{{margin:2rem auto;padding:0 14px 3rem}}.hero,.section{{padding:26px 22px}}.toolbar{{padding:10px 14px}}}}
.breakdown{{max-width:470px;margin:0 0 24px}}.breakdown ul{{padding:0;list-style:none}}.breakdown li{{display:grid;grid-template-columns:minmax(6em,1fr) 2fr 2ch;gap:12px;align-items:center;font-size:14px}}meter{{width:100%;height:12px;accent-color:var(--accent)}}
@media print{{@page{{margin:18mm}}body{{background:white;font-size:11pt}}.toolbar{{display:none}}main{{max-width:none;margin:0;padding:0}}.hero,.section{{box-shadow:none;background:white;border-color:#bbb;break-inside:avoid}}.section{{break-inside:auto}}a{{color:inherit;text-decoration:none}}footer{{margin:1rem auto 0}}}}
</style></head><body>
<div class="toolbar"><button id="report-theme" type="button" hidden>Light mode</button><button type="button" onclick="window.print()">Save as PDF</button></div>
<script>
(() => {{
  const key = "athena.report.theme";
  const root = document.documentElement;
  const toggle = document.getElementById("report-theme");

  function setTheme(theme) {{
    root.dataset.theme = theme;
    toggle.textContent = theme === "light" ? "Dark mode" : "Light mode";
  }}

  try {{ setTheme(localStorage.getItem(key) === "light" ? "light" : "dark"); }} catch {{}}
  toggle.hidden = false;
  toggle.addEventListener("click", () => {{
    const next = root.dataset.theme === "light" ? "dark" : "light";
    setTheme(next);
    try {{ localStorage.setItem(key, next); }} catch {{}}
  }});
}})();
</script>
<main><header class="hero"><p class="eyebrow">ATHENA · RESEARCH BRIEF</p><h1>{_escape(question or 'Research report')}</h1>
<div class="meta">{outcome_markup}<span class="pill">{_escape(approval)}</span><span class="pill">Confidence {confidence_text}</span><span class="pill">{len(citations)} source{'s' if len(citations) != 1 else ''}</span></div></header>
<section class="section"><p class="eyebrow">SYNTHESIS</p><h2>What the research found</h2><div class="answer">{_escape(report.get('answer') or 'No answer was recorded.')}</div></section>
<section class="section"><p class="eyebrow">KEY POINTS</p><h2>At a glance</h2><ul>{point_markup or '<li>No key points were recorded.</li>'}</ul></section>
<section class="section"><p class="eyebrow">SOURCES</p><h2>Evidence trail</h2>{breakdown_markup}{source_markup}</section>{review_markup}
</main><footer>Run {_escape(run_id)} · Generated by Athena</footer></body></html>"""


@router.get("/api/runs/{run_id}/report", response_class=HTMLResponse)
def visual_report(run_id: str, request: Request) -> HTMLResponse:
    guard(request)
    blackboard = Blackboard.open(run_id)
    task = blackboard.read("task")
    report = blackboard.read("final_report")
    if not task or not report:
        raise HTTPException(status_code=404, detail="No completed research report available")
    critique = blackboard.read("critique")
    evidence = blackboard.read("evidence")
    # Run summaries live in the event store; report content and citations live
    # in the blackboard.
    from athena.observability.store import SQLiteEventStore

    latest = SQLiteEventStore().latest_summary(run_id)
    summary_data = dict(latest.extra) if latest else {}
    return _response(
        run_id=run_id,
        question=task[0].question,
        report=report[0].model_dump(mode="json"),
        critique=critique[0].model_dump(mode="json") if critique else None,
        summary=summary_data,
        evidence=[item.model_dump(mode="json") for item in (evidence[0] if evidence else [])],
    )


@router.get("/api/replay/{name}/report", response_class=HTMLResponse)
def replay_visual_report(name: str, request: Request) -> HTMLResponse:
    guard(request)
    try:
        bundle = replay.load(name)
    except replay.ReplayError as exc:
        raise HTTPException(404, str(exc)) from exc
    blackboard = bundle.get("blackboard") or {}
    report = (blackboard.get("final_report") or {}).get("value")
    if not report:
        raise HTTPException(404, "No completed research report available")
    return _response(
        run_id=bundle["run_id"],
        question=((blackboard.get("task") or {}).get("value") or {}).get("question", ""),
        report=report,
        critique=(blackboard.get("critique") or {}).get("value"),
        summary=bundle.get("summary") or {},
        evidence=bundle.get("evidence") or [],
    )


def _response(**report_fields) -> HTMLResponse:
    return HTMLResponse(
        render_visual_report(**redact_tree(report_fields)),
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
            "Cache-Control": "no-store",
        },
    )
