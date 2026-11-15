"""Internal launcher: restart CrewAI on amd with corrected caps and timeouts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
PROFILE_ID = "367f57c4-d107-447c-a857-632fa8e6a454"

sys.path.insert(0, str(REPO))
os.chdir(REPO)
os.environ.update(
    {
        "ATHENA_LLM_TIMEOUT_S": "900",
        "ATHENA_A2A_TIMEOUT_S": "1200",
        "ATHENA_MAX_LLM_CALLS": "40",
        "BLACKBOARD_DB": str(REPO / "data" / "blackboard.db"),
        "EVENTS_DB": str(REPO / "data" / "events.db"),
        "PYTHONUNBUFFERED": "1",
    }
)

from athena.api import activation  # noqa: E402

result = activation.activate(PROFILE_ID)
print(result)
raise SystemExit(0 if result.get("ok") else 1)
