"""The analyst and critic CrewAI roles.

Each role reads the blackboard, runs a single-agent Crew, and writes exactly
one section. The writer lives in ``crew_writer`` because it owns the report.
"""

from __future__ import annotations

from typing import Callable

from athena.agents import bb_io
from athena.agents.critique_rules import calibrate
from athena.blackboard import (
    Analysis,
    Blackboard,
    BlackboardStore,
    Critique,
)

Generate = Callable[[str], object]


class RunawayOutputError(RuntimeError):
    """A model call produced far more output than the role is allowed."""


def crew_call(role: str, goal: str, prompt: str, schema: type) -> tuple[object, dict]:
    """Run one single-agent Crew; return its structured output and usage.

    This is the single entry point every CrewAI role goes through, so it is also
    where monitoring hooks in: one model_call event with tokens, and an error
    event if the crew raises.
    """
    # athena.config.llm must load first: importing it points CrewAI's storage at
    # the repo before `import crewai` creates %LOCALAPPDATA%\CrewAI.
    import math
    import time

    from athena.config.call_limits import role_limits, take_runaway
    from athena.config.llm import get_crewai_llm, max_tokens_for
    from crewai import Agent, Crew, Process, Task

    from athena.observability.litellm_usage import usage_scope
    from athena.observability.usage_hook import install_usage_logger
    from athena.observability.redact import redact
    from athena.observability.recorder import get_recorder

    started = time.perf_counter()
    recorder = get_recorder()
    scope = None
    try:
        agent = Agent(
            role=role.title(),
            goal=goal,
            backstory=f"You are Athena's {role}. You work only on the material "
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
        # Building the LLM reset litellm.callbacks (crewai/llm.py:707), so the
        # hook is re-asserted here, after construction and before the call.
        install_usage_logger()
        with role_limits(role, max_tokens_for(role)), usage_scope(role) as scope:
            output = crew.kickoff()
        runaway = take_runaway()
        if runaway:
            raise RunawayOutputError(
                f"{runaway['role']} returned {runaway['tokens']} tokens "
                f"against a cap of {runaway['cap']}"
            )
    except Exception as exc:
        if recorder is not None:
            recorder.emit(
                "error", role, "crewai", ok=False,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error_type=type(exc).__name__, error_message=redact(str(exc))[:200],
                payload_summary=f"{role}: crew failed",
            )
        raise

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    # CrewOutput.token_usage is not populated on this path; the litellm scope is.
    # Keep whichever number is larger so neither source masks the other.
    usage = getattr(output, "token_usage", None)
    crew_in = getattr(usage, "prompt_tokens", None) if usage else None
    crew_out = getattr(usage, "completion_tokens", None) if usage else None
    crew_reasoning = getattr(usage, "reasoning_tokens", None) if usage else None

    tokens_in = max(crew_in or 0, scope.tokens_in if scope else 0) or None
    tokens_out = max(crew_out or 0, scope.tokens_out if scope else 0) or None
    reasoning = max(crew_reasoning or 0, scope.reasoning_tokens if scope else 0) or None
    calls = scope.calls if scope else 0

    # Last resort: the hook never fired but the role produced text. Mark the
    # numbers as estimates so nothing downstream mistakes them for measured.
    estimated = False
    reply_text = str(getattr(output, "raw", "") or "")
    if not calls and reply_text.strip():
        tokens_in = tokens_in or math.ceil(len(prompt) / 4)
        tokens_out = tokens_out or math.ceil(len(reply_text) / 4)
        reasoning = reasoning or 0
        estimated = True

    print(
        f"usage scope {role}: calls={calls} tokens={tokens_in or 0}/"
        f"{tokens_out or 0} reasoning={reasoning or 0}"
        + (" (estimated)" if estimated else "")
    )
    if recorder is not None:
        recorder.emit(
            "model_call", role, "crewai", duration_ms=duration_ms,
            tokens_in=tokens_in, tokens_out=tokens_out,
            payload_summary=f"{role}: {len(prompt)} prompt chars",
            extra={"reasoning_tokens": reasoning, "calls": calls},
        )
    stats = {
        "tokens_in": tokens_in or 0,
        "tokens_out": tokens_out or 0,
        "reasoning_tokens": reasoning or 0,
        "calls": calls,
        "model_ms": int(duration_ms),
        **({"estimated": True} if estimated else {}),
    }
    return output.pydantic, stats


def _generate_analysis(prompt: str) -> tuple[object, dict]:
    return crew_call("analyst", "Synthesise the evidence into findings and gaps.", prompt, Analysis)


def _generate_critique(prompt: str) -> tuple[object, dict]:
    return crew_call("critic", "Review the report against the evidence.", prompt, Critique)


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
    # No placeholder on failure: writing a section that looks like real
    # analysis is what made analyst failures silent. The exception propagates,
    # the server turns it into ok:false, and nothing is written.
    analysis, stats = unpack_result(generate(prompt))
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
        "the answer is still usable.\n"
        "- If the answer says the evidence does not cover the question, or its "
        "confidence is below 0.5, approved MUST be false with severity at least "
        "'medium'."
    )
    generate = generate or _generate_critique
    # Same rule as the analyst: a failed critic writes nothing and reports
    # ok:false. A fabricated "medium severity" critique would silently pass.
    result, stats = unpack_result(generate(prompt))
    if not isinstance(result, Critique):
        result = Critique.model_validate(result)
    # A report that admits it cannot answer must not be approved.
    report = blackboard.read("final_report")
    result = calibrate(result, report[0] if report else None)
    version = blackboard.write("critique", result, author="critic")
    return {"section": "critique", "version": version, **stats}

def unpack_result(value) -> tuple[object, dict]:
    """Accept both ``model`` and ``(model, usage)`` from a generator."""
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], dict):
        return value
    return value, {}