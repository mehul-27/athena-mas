"""Demo: one run, two named authors, then snapshot and history as JSON."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena.blackboard import Blackboard, Draft, EvidenceItem, Plan, PlanStep, Task

bb = Blackboard.create(Task(question="Why is the number 42 famous?"))
bb.write("plan", Plan(steps=[PlanStep(id="s1", description="Find sources", assigned_to="researcher")]), author="planner")
bb.append("evidence", EvidenceItem(id="e1", source_type="web", source_ref="https://example.com/42", content="42 is the answer to everything.", retrieved_by="researcher"), author="researcher")
bb.write("draft", Draft(text="42 is famous because of Douglas Adams.", based_on_evidence_ids=["e1"]), author="writer")

print("SNAPSHOT:", json.dumps(bb.snapshot(), default=lambda m: m.model_dump(mode="json"), indent=2))
print("HISTORY:", json.dumps(bb.history(), indent=2))
