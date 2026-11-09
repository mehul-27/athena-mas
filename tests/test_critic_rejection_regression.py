from __future__ import annotations

from athena.agents import crew_p2p, crew_roles
from athena.agents.critique_rules import ISSUE, calibrate
from athena.agents.sufficiency import assess
from athena.blackboard import Blackboard, Critique, EvidenceItem, FinalReport, SQLiteStore, Task


APPROVING = Critique(issues=[], severity="low", approved=True)


def report(answer: str, confidence: float = 0.8) -> FinalReport:
    return FinalReport(answer=answer, confidence=confidence, run_id="r")


def _definition_evidence() -> list[EvidenceItem]:
    return [EvidenceItem(
        id="doc-1", source_type="rag",
        source_ref="supplied_handbook.pdf#p1",
        content=("Configuration management covers identification, baseline control, "
                 "change control, status accounting, and configuration audits. " * 4),
        retrieved_by="researcher",
    )]


def _python_release_evidence() -> list[EvidenceItem]:
    return [EvidenceItem(
        id=f"fetch-{index}", source_type="web",
        source_ref=f"https://docs.python.org/3.13/whatsnew/{index}.html",
        content=f"Python 3.13 change {index}: supported explanation and practical impact. " * 12,
        retrieved_by="researcher",
    ) for index in range(1, 4)]


def test_incidental_publication_date_caveat_keeps_supported_answer() -> None:
    evidence = _python_release_evidence()
    question = ("Explain three key changes introduced in Python 3.13, with one official "
                "source URL for each and a brief practical impact.")
    assessment = assess(question, evidence)
    assert assessment.answer_as_asked is True
    result = calibrate(
        APPROVING,
        report("Python 3.13 change 1 improves the workflow. Python 3.13 change 2 improves another workflow. Python 3.13 change 3 adds a further practical improvement. The evidence does not state the publication date.", confidence=assessment.confidence_ceiling),
        assessment,
    )
    assert result.approved is True
    assert ISSUE not in result.issues


def test_missing_requested_date_still_rejects() -> None:
    assessment = assess("What is the publication date of the configuration management handbook?", _definition_evidence())
    assert assessment.answer_as_asked is False
    result = calibrate(
        APPROVING,
        report("The evidence does not state the requested publication date.", confidence=assessment.confidence_ceiling),
        assessment,
    )
    assert result.approved is False
    assert result.severity == "medium"
    assert ISSUE in result.issues


def test_qualified_partial_gap_is_allowed_but_direct_refusal_is_not() -> None:
    assessment = assess(
        "How does NASA's nitrogen thruster work in space?",
        [EvidenceItem(
            id="fetch-1", source_type="web", source_ref="https://example.org/cold-gas",
            content="A cold gas thruster expands pressurized nitrogen through a nozzle to produce thrust without combustion. " * 5,
            retrieved_by="researcher",
        )],
        enabled_tools=["web_search", "fetch_page"],
    )
    assert assessment.answer_as_asked is False
    assert assessment.qualified_possible is True
    partial = calibrate(
        APPROVING,
        report("A cold gas thruster expands pressurized nitrogen through a nozzle to produce thrust without combustion; organization-specific details cannot be determined from the evidence.", confidence=assessment.confidence_ceiling),
        assessment,
    )
    refusal = calibrate(
        APPROVING,
        report("The evidence cannot answer the question.", confidence=assessment.confidence_ceiling),
        assessment,
    )
    assert partial.approved is True
    assert refusal.approved is False
    assert ISSUE in refusal.issues


def test_main_critic_prompt_preserves_qualified_partial_exception(tmp_path) -> None:
    blackboard = Blackboard.create(Task(question="q"), store=SQLiteStore(tmp_path / "bb.db"))
    captured = {}

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return Critique(issues=[], severity="low", approved=True)

    crew_roles.critique(blackboard.run_id, "go", blackboard.store, fake_generate)
    prompt = captured["prompt"]
    assert "unless this is a qualified partial answer" in prompt
    assert "confidence is below 0.5, approved MUST be false" not in prompt
    assert "orchestration pattern/persona metadata as execution context" in prompt


def test_p2p_critic_prompt_has_matching_criteria(monkeypatch) -> None:
    captured = {}

    def fake_crew_call(_role, _description, prompt, _schema):
        captured["prompt"] = prompt
        return Critique(issues=[], severity="low", approved=True), {
            "tokens_in": 1, "tokens_out": 1
        }

    monkeypatch.setattr(crew_p2p, "crew_call", fake_crew_call)
    crew_p2p.p2p_critique("r", "review", context="REPORT: supported answer")
    prompt = captured["prompt"]
    assert "qualified partial answer" in prompt
    assert "confidence below 0.5" in prompt
    assert "missing requested findings" in prompt
    assert "REPORT: supported answer" in prompt


def test_p2p_writer_count_does_not_conflict_with_requested_findings(monkeypatch) -> None:
    captured = {}

    def fake_crew_call(_role, _description, prompt, _schema):
        captured["prompt"] = prompt
        return FinalReport(answer="x", confidence=0.8, run_id="r"), {
            "tokens_in": 1, "tokens_out": 1
        }

    monkeypatch.setattr(crew_p2p, "crew_call", fake_crew_call)
    crew_p2p.p2p_write_report("r", "Give five findings", context="evidence")
    prompt = captured["prompt"]
    assert "exactly 5 key points" in prompt
    assert "at least 5 citations from distinct sources" in prompt
