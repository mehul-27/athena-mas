"""Internal launcher: restart the CrewAI server on amd with a 300s call timeout."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
PROFILE_ID = "367f57c4-d107-447c-a857-632fa8e6a454"

sys.path.insert(0, str(REPO))
os.chdir(REPO)
os.environ["ATHENA_LLM_TIMEOUT_S"] = "300"

from athena.api import activation  # noqa: E402

result = activation.activate(PROFILE_ID)
print(result)
raise SystemExit(0 if result.get("ok") else 1)
