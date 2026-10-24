"""p2p: the same stages, message-passing instead of the blackboard.

No agent reads the blackboard in this mode. Each stage's output travels inline
in the A2A message (``context``), and the supervisor writes the stage outputs to
the blackboard afterwards as audit records so the report, history and summary
stay comparable with the blackboard patterns.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from athena.agents.a2a_client import build_payload, send_role_request
from athena.agents.a2a_stage import DEFAULT_CARD_URL, INSTRUCTIONS
from athena.agents.planner import build_planner, parse_plan_text
from athena.agents.report_sanitize import evidence_ids_for, sanitize_report
from athena.agents.researcher import build_researcher
from athena.agents import a2a_client
from athena.blackboard import Analysis, Blackboard, Critique, Draft, FinalReport, Plan
from athena.observability.recorder import get_recorder
from athena.patterns.context import P2PContext, render_plan



def _text_of(event) -> str:
    content = getattr(event, "content", None)
    if not content or not content.parts:
        return ""
    return "".join(part.text for part in content.parts if part.text and not part.thought)


def _timing(name: str, started: float) -> Event:
    return Event(
        author="supervisor",
        custom_metadata={"stage": name, "stage_ms": round((time.perf_counter() - started) * 1000, 1)},
    )


class P2PAgent(BaseAgent):
    """Runs the pipeline carrying context in messages, auditing afterwards."""

    run_id: str
    question: str
    card_url: str = DEFAULT_CARD_URL
    store: Any = None

    def _blackboard(self) -> Blackboard:
        return Blackboard.open(self.run_id, self.store)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        context = P2PContext(question=self.question)

        # --- planner: message mode, no blackboard tool -------------------
        started = time.perf_counter()
        planner = build_planner(self.run_id, None, task_text=self.question, message_mode=True)
        raw = ""
        async for event in planner.run_async(ctx):
            yield event
            raw = _text_of(event) or raw
        plan = Plan(steps=parse_plan_text(raw))
        context.plan = render_plan(plan)
        yield _timing("planner", started)
        self._blackboard().write("plan", plan, author="planner")  # audit

        # --- researcher: tools, evidence collected in memory -------------
        started = time.perf_counter()
        collected: list = []
        researcher = build_researcher(
            self.run_id, None, task_text=self.question, sink=collected.extend
        )
        async for event in researcher.run_async(ctx):
            yield event
        context.evidence = collected
        yield _timing("researcher", started)
        for item in collected:  # audit
            self._blackboard().append("evidence", item, author="researcher")

        # --- analyst / writer / critic over A2A --------------------------
        for role in ("analyst", "writer", "critic"):
            started = time.perf_counter()
            content = await self._call_role(role, context)
            yield Event(author=role, content=types.Content(parts=[types.Part(text=content[:200])]))
            yield _timing(role, started)

        report = self._blackboard().read("final_report")
        text = report[0].to_markdown() if report else "(no final report was written)"
        yield Event(
            author=self.name,
            content=types.Content(parts=[types.Part(text=text)]),
            custom_metadata={"final": True},
        )

    async def _call_role(self, role: str, context: P2PContext) -> str:
        """Send one stage's context, parse the reply, audit it."""
        payload = build_payload(self.run_id, role, INSTRUCTIONS[role], context.for_role(role))
        started = time.perf_counter()
        reply = await send_role_request(self.card_url, payload)
        self._record_call(role, reply, started)
        try:
            body = json.loads(reply)
        except ValueError:
            body = {"ok": False, "error": "server returned a non-JSON reply"}
        if not body.get("ok"):
            return self._audit_failure(role, str(body.get("error", "unknown error")))

        content = body.get("content", "")
        if role == "analyst":
            return self._audit_analysis(content)
        if role == "writer":
            return self._audit_report(content, context.evidence)
        return self._audit_critique(content)

    def _audit_analysis(self, content: str) -> str:
        try:
            analysis = Analysis.model_validate(json.loads(content))
        except Exception:  # noqa: BLE001 - a bad stage must not stop the run
            analysis = Analysis(key_findings=[], gaps=["analyst returned invalid JSON"])
        self._blackboard().write("analysis", analysis, author="analyst")
        return content

    def _audit_report(self, content: str, items: list) -> str:
        try:
            report = FinalReport.model_validate(json.loads(content))
        except Exception as exc:  # noqa: BLE001
            report = FinalReport(
                answer=f"The report could not be generated ({type(exc).__name__}).",
                confidence=0.0, run_id=self.run_id,
            )
        report = sanitize_report(report, self._blackboard(), self.run_id, items=items)
        self._blackboard().write(
            "draft",
            Draft(text=report.answer, based_on_evidence_ids=evidence_ids_for(report, items)),
            author="writer",
        )
        self._blackboard().write("final_report", report, author="writer")
        return content

    def _audit_critique(self, content: str) -> str:
        try:
            critique = Critique.model_validate(json.loads(content))
        except Exception:  # noqa: BLE001
            critique = Critique(issues=["critic returned invalid JSON"], severity="medium", approved=False)
        self._blackboard().write("critique", critique, author="critic")
        return content

    def _audit_failure(self, role: str, error: str) -> str:
        blackboard = self._blackboard()
        if role == "analyst":
            blackboard.write("analysis", Analysis(key_findings=[], gaps=[error]), author="analyst")
        elif role == "writer":
            blackboard.write(
                "final_report",
                FinalReport(answer=f"The writer failed: {error}", confidence=0.0, run_id=self.run_id),
                author="writer",
            )
        else:
            blackboard.write(
                "critique",
                Critique(issues=[error], severity="medium", approved=False),
                author="critic",
            )
        return ""


    def _record_call(self, role: str, reply: str, started: float) -> None:
        """Emit the p2p A2A exchange, including the context bytes it carried."""
        recorder = get_recorder()
        if recorder is None:
            return
        traffic = a2a_client.last_call()
        try:
            body = json.loads(reply)
        except ValueError:
            body = {}
        if not body.get("ok", True):
            print(
                f"WARNING {role} failed: {body.get('error_type', 'A2AError')}: "
                f"{str(body.get('error', 'role failed'))[:200]}"
            )

        recorder.emit(
            "a2a_call", role, "adk",
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            ok=bool(body.get("ok", True)),
            tokens_in=body.get("tokens_in"),
            tokens_out=body.get("tokens_out"),
            payload_summary=f"p2p {role}: {traffic['out']}B out / {traffic['in']}B in",
            extra={
                "request_bytes": traffic["out"],
                "response_bytes": traffic["in"],
                "reasoning_tokens": body.get("reasoning_tokens"),
                "calls": body.get("calls"),
            },
        )


def build(run_id: str, store: Any = None, card_url: str = "") -> P2PAgent:
    blackboard = Blackboard.open(run_id, store)
    question = ""
    task = blackboard.read("task")
    if task:
        question = task[0].question
    return P2PAgent(
        name="supervisor",
        description="Message-passing pipeline with blackboard audit records.",
        run_id=run_id,
        question=question,
        card_url=card_url or DEFAULT_CARD_URL,
        store=store,
    )
