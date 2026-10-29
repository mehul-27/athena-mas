"""Deterministic finding-count requirements and prompt wiring."""

from __future__ import annotations

from athena.agents.crew_writer import writer_prompt
from athena.agents.requirements import finding_requirement, required_findings
from athena.blackboard import Blackboard, EvidenceItem, SQLiteStore, Task


def test_required_findings_parses_words_and_digits() -> None:
    assert required_findings("Explain three key changes with sources.") == 3
    assert required_findings("List 5 findings.") == 5
    assert required_findings("Give two practical differences.") == 2
    assert required_findings("Explain the release.") == 0
    assert required_findings("Explain twenty changes.") == 0  # unsupported count word


def test_finding_requirement_requires_sources_and_impact() -> None:
    text = finding_requirement("Explain three key changes.")

    assert "exactly 3 findings" in text
    assert "distinct primary-source URL" in text
    assert "practical impact" in text
    assert "Do not reuse the same primary source" in text
    assert "never invent" in text


def test_writer_prompt_carries_the_finding_requirement(tmp_path) -> None:
    blackboard = Blackboard.create(
        Task(question="Explain three key changes introduced in Python 3.13."),
        store=SQLiteStore(tmp_path / "bb.db"),
    )
    blackboard.append(
        "evidence",
        EvidenceItem(
            id="fetch-one", source_type="web",
            source_ref="https://docs.python.org/3.13/whatsnew/3.13.html",
            content="A primary-source excerpt.", retrieved_by="researcher",
        ),
        author="researcher",
    )

    prompt = writer_prompt(blackboard, "write")

    assert "exactly 3 findings" in prompt
    assert "distinct primary-source URL" in prompt
    assert "Do not reuse the same primary source" in prompt
