"""p2p role handlers: the CrewAI side of a message-passing call.

These never touch the blackboard. They receive the whole context in the message
and return their output inline as JSON, which is the point of the p2p baseline.
"""

from __future__ import annotations

from athena.agents.crew_roles import crew_call, unpack_result
from athena.agents.requirements import finding_requirement
from athena.agents.untrusted import UNTRUSTED_DATA_RULE
from athena.blackboard import Analysis, Critique, FinalReport

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
    "Grade the REPORT against the EVIDENCE. approved=true when every claim is "
    "traceable and the answer is complete. approved=false only for concrete "
    "issues, and list them. severity is 'high' ONLY for unsupported claims, "
    "contradictions, or a truncated answer."
)


def _content(model) -> str:
    return model.model_dump_json()


def p2p_analyse(run_id: str, instruction: str, store=None, context: str = "") -> dict:
    requirement = finding_requirement(f"{instruction}\n{context}")
    prompt = f"{instruction}\n\n{UNTRUSTED_DATA_RULE}\n\n{requirement}\n\n{context}\n\n{ANALYST_PROMPT}"
    analysis, stats = unpack_result(
        crew_call("analyst", "Synthesise the evidence into findings and gaps.",
                   prompt, Analysis)
    )
    return {"section": "analysis", "content": _content(analysis), **stats}


def p2p_write_report(run_id: str, instruction: str, store=None, context: str = "") -> dict:
    requirement = finding_requirement(f"{instruction}\n{context}")
    prompt = f"{instruction}\n\n{UNTRUSTED_DATA_RULE}\n\n{requirement}\n\n{context}\n\n{WRITER_PROMPT}"
    report, stats = unpack_result(
        crew_call("writer", "Write a cited research report.", prompt, FinalReport)
    )
    return {"section": "final_report", "content": _content(report), **stats}


def p2p_critique(run_id: str, instruction: str, store=None, context: str = "") -> dict:
    requirement = finding_requirement(f"{instruction}\n{context}")
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
