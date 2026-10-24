"""The analyst and critic CrewAI roles.

Each role reads the blackboard, runs a single-agent Crew, and writes exactly
one section. The writer lives in ``crew_writer`` because it owns the report.
"""

from __future__ import annotations

from typing import Callable

from athena.agents import bb_io
from athena.blackboard import (
    Analysis,
    Blackboard,
    BlackboardStore,
    Critique,
)

Generate = Callable[[str], object]


def _crew_call(role: str, goal: str, prompt: str, schema: type) -> tuple[object, dict]:
    """Run one single-agent Crew; return its structured output and usage.

    This is the single entry point every CrewAI role goes through, so it is also
    where monitoring hooks in: one model_call event with tokens, and an error
    event if the crew raises.
    """
    # athena.config.llm must load first: importing it points CrewAI's storage at
    # the repo before `import crewai` creates %LOCALAPPDATA%\CrewAI.
    import time

    from athena.config.llm import get_crewai_llm
    from crewai import Agent, Crew, Process, Task

    from athena.observability.recorder import get_recorder

    started = time.perf_counter()
    recorder = get_recorder()
    try:
        agent = Agent(
            role=role.title(),
            goal=goal,
            backstory=f"You are Athena's {role}. You work only from the material "
            f"given to you and never invent sources.",
            llm=get_crewai_llm(role),
            verbose=False,
            allow_delegation=False,
        )
        task = Task(
            description=prompt,
            expected_output=f"A JSON object matching the {schema.__name__} schema.",
            agent=agent,
            output_pydantic=schema,
        )
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
        output = crew.kickoff()
    except Exception as exc:
        if recorder is not None:
            recorder.emit(
                "error", role, "crewai", ok=False,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error_type=type(exc).__name__, error_message=str(exc),
                payload_summary=f"{role}: crew failed",
            )
        raise

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    usage = getattr(output, "token_usage", None)
    tokens_in = getattr(usage, "prompt_tokens", None) if usage else None
    tokens_out = getattr(usage, "completion_tokens", None) if usage else None
    reasoning = getattr(usage, "reasoning_tokens", None) if usage else None
    if recorder is not None:
        recorder.emit(
            "model_call", role, "crewai", duration_ms=duration_ms,
            tokens_in=tokens_in, tokens_out=tokens_out,
            payload_summary=f"{role}: {len(prompt)} prompt chars",
            extra={"reasoning_tokens": reasoning},
        )
    stats = {
        "tokens_in": tokens_in or 0,
        "tokens_out": tokens_out or 0,
        "reasoning_tokens": reasoning or 0,
        "model_ms": int(duration_ms),
    }
    return output.pydantic, stats


def _generate_analysis(prompt: str) -> tuple[object, dict]:
    return _crew_call("analyst", "Synthesise the evidence into findings and gaps.", prompt, Analysis)


def _generate_critique(prompt: str) -> tuple[object, dict]:
    return _crew_call("critic", "Review the report against the evidence.", prompt, Critique)


def analyse(
    run_id: str,
    instruction: str,
    store: BlackboardStore | None = None,
    generate: Generate | None = None,
) -> dict:
    """Read task + evidence, write section ``analysis`` as author ``analyst``."""
    blackboard = Blackboard.open(run_id, store)
    prompt = (
        f"{instruction}\n\nTASK:\n{bb_io.task_text(blackboard)}\n\n"
        f"EVIDENCE (cite ids exactly as shown):\n{bb_io.evidence_text(blackboard)}\n\n"
        "Return key_findings (2-4 short bullets) and gaps (what the evidence does "
        "not answer).\n"
        "When you name a source, cite it by its source_ref exactly as shown "
        "(document name plus page, e.g. nasa_Systems_handbook.pdf#p12-chunk-32). "
        "Never cite an internal id such as doc_1a2b3c."
    )
    generate = generate or _generate_analysis
    stats: dict = {}
    try:
        analysis, stats = _unpack(generate(prompt))
    except Exception as exc:  # noqa: BLE001 - one bad generation must not stop the run
        analysis = Analysis(
            key_findings=[], gaps=[f"analysis generation failed: {type(exc).__name__}"]
        )
    if not isinstance(analysis, Analysis):
        analysis = Analysis.model_validate(analysis)
    version = blackboard.write("analysis", analysis, author="analyst")
    return {"section": "analysis", "version": version, **stats}


def critique(
    run_id: str,
    instruction: str,
    store: BlackboardStore | None = None,
    generate: Generate | None = None,
) -> dict:
    """Review draft + report + evidence, write section ``critique`` as ``critic``."""
    blackboard = Blackboard.open(run_id, store)
    prompt = (
        f"{instruction}\n\nTASK:\n{bb_io.task_text(blackboard)}\n\n"
        f"EVIDENCE:\n{bb_io.evidence_text(blackboard)}\n\n"
        f"REPORT:\n{bb_io.report_text(blackboard)}\n\n"
        "Grade strictly but fairly:\n"
        "- approved=true when every claim in the answer is traceable to the "
        "recorded evidence and the answer is complete.\n"
        "- approved=false only for concrete issues, and list each one in issues.\n"
        "- severity is 'high' ONLY for unsupported claims, contradictions with "
        "the evidence, or a truncated answer. Style and brevity are not 'high'.\n"
        "- severity 'low' or 'medium' for everything else, with approved=true if "
        "the answer is still usable."
    )
    generate = generate or _generate_critique
    stats: dict = {}
    try:
        result, stats = _unpack(generate(prompt))
    except Exception as exc:  # noqa: BLE001 - one bad generation must not stop the run
        result = Critique(
            issues=[f"critique generation failed: {type(exc).__name__}"],
            severity="medium",
            approved=False,
        )
    if not isinstance(result, Critique):
        result = Critique.model_validate(result)
    version = blackboard.write("critique", result, author="critic")
    return {"section": "critique", "version": version, **stats}

def _unpack(value) -> tuple[object, dict]:
    """Accept both ``model`` and ``(model, usage)`` from a generator."""
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], dict):
        return value
    return value, {}