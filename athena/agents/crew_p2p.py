"""p2p role handlers: the CrewAI side of a message-passing call.

These never touch the blackboard. They receive the whole context in the message
and return their output inline as JSON, which is the point of the p2p baseline.
"""

from __future__ import annotations

from athena.agents.crew_roles import crew_call, unpack_result
from athena.agents.requirements import finding_requirement, required_findings
from athena.agents.untrusted import UNTRUSTED_DATA_RULE
from athena.blackboard import Analysis, Critique, FinalReport
from athena.patterns.context import task_question

ANALYST_PROMPT = (
    "Read the context and return key_findings (2-4 short bullets) and gaps. "
    "Cite sources by source_ref exactly as shown, never by internal ids."
)
WRITER_PROMPT = (
    "Using the context, write an answer of at most 150 words ending with a full "
    "stop, 3-5 key points, and 1-3 citations whose source_ref is copied verbatim "
    "from the evidence. confidence is 0 to 1."
)
CRITIC_PROMPT = (
    "Grade the REPORT against the EVIDENCE and coverage assessment. Treat task "
    "orchestration pattern/persona metadata as execution context, not requested "
    "answer format unless the question explicitly asks for it. approved=true "
    "when every claim is traceable and the answer is complete for what the "
    "evidence supports. A qualified partial answer is complete when it answers "
    "the supported part and names the unverified remainder, if coverage says "
    "such an answer is possible. Reject unsupported claims, contradictions, "
    "missing requested findings, truncated answers, or a direct admission that "
    "the evidence cannot answer the core question. Reject confidence below 0.5 "
    "unless the report is a qualified partial and coverage allows it. For a "
    "fixed finding count, each finding needs a distinct primary-source URL, a "
    "grounded explanation, and practical impact; do not invent missing support. "
    "Use high severity ONLY for unsupported claims, contradictions, missing "
    "required findings, or truncation. Style/brevity are not high severity; "
    "low/medium issues may be approved when the answer remains usable. Use the "
    "citation reference audit to check whether each report source_ref was "
    "recorded: do not call a recorded reference missing because its excerpt is "
    "not shown or the answer does not repeat the URL; recorded does not prove "
    "support."
)


def _content(model) -> str:
    return model.model_dump_json()


def p2p_analyse(run_id: str, instruction: str, store=None, context: str = "") -> dict:
    requirement = finding_requirement(task_question(instruction, context))
    prompt = f"{instruction}\n\n{UNTRUSTED_DATA_RULE}\n\n{requirement}\n\n{context}\n\n{ANALYST_PROMPT}"
    analysis, stats = unpack_result(
        crew_call("analyst", "Synthesise the evidence into findings and gaps.",
                   prompt, Analysis)
    )
    return {"section": "analysis", "content": _content(analysis), **stats}


def p2p_write_report(run_id: str, instruction: str, store=None, context: str = "") -> dict:
    question = task_question(instruction, context)
    requirement = finding_requirement(question)
    count = required_findings(question)
    writer_prompt = (
        WRITER_PROMPT.replace("3-5 key points", f"exactly {count} key points")
        .replace("1-3 citations", f"at least {count} citations from distinct sources")
        if count else WRITER_PROMPT
    )
    prompt = f"{instruction}\n\n{UNTRUSTED_DATA_RULE}\n\n{requirement}\n\n{context}\n\n{writer_prompt}"
    report, stats = unpack_result(
        crew_call("writer", "Write a cited research report.", prompt, FinalReport)
    )
    return {"section": "final_report", "content": _content(report), **stats}


def p2p_critique(run_id: str, instruction: str, store=None, context: str = "") -> dict:
    requirement = finding_requirement(task_question(instruction, context))
    prompt = f"{instruction}\n\n{UNTRUSTED_DATA_RULE}\n\n{requirement}\n\n{context}\n\n{CRITIC_PROMPT}"
    critique, stats = unpack_result(
        crew_call("critic", "Review the report against the evidence.", prompt, Critique)
    )
    return {"section": "critique", "content": _content(critique), **stats}


P2P_ROLES = {
    "analyst": p2p_analyse,
    "writer": p2p_write_report,
    "critic": p2p_critique,
}
