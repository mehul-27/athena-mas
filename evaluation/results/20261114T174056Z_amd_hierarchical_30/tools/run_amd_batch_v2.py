"""Internal launcher v2: corrected caps/timeouts and absolute shared DB paths.

Reads the key from the existing encrypted vault (never prints it), points both
ADK and CrewAI provider variables at the amd endpoint, and forwards every CLI
argument to ``python -m evaluation.runner``.

Corrections recorded in the batch notes:
* ATHENA_MAX_LLM_CALLS=40 (documented hierarchical policy) overrides the stale
  Groq diagnostic 12 in .env.
* ATHENA_LLM_TIMEOUT_S=900 and ATHENA_A2A_TIMEOUT_S=1200 for slow AMD calls;
  the per-run timeout remains 3600 s.
* BLACKBOARD_DB/EVENTS_DB are absolute shared paths for parent and children.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
PROFILE_NAME = "amd"
PROFILE_ID = "367f57c4-d107-447c-a857-632fa8e6a454"

sys.path.insert(0, str(REPO))
os.chdir(REPO)

from athena.settings import profiles as profile_store  # noqa: E402
from athena.settings.vault import default_vault  # noqa: E402

profile = next((p for p in profile_store.load_profiles() if p.name == PROFILE_NAME), None)
if profile is None or profile.id != PROFILE_ID:
    raise SystemExit("amd profile missing or id changed; refusing to launch")
key = default_vault().get(profile_store.key_name(profile.id, "llm"))
if not key:
    raise SystemExit("amd profile has no stored key; refusing to launch")

env = dict(os.environ)
env.update(
    {
        "LLM_API_KEY": key,
        "LLM_MODEL": profile.model,
        "LLM_API_BASE": profile.api_base,
        "OPENAI_API_KEY": key,
        "OPENAI_API_BASE": profile.api_base,
        "OPENAI_BASE_URL": profile.api_base,
        "ATHENA_LLM_TIMEOUT_S": "900",
        "ATHENA_A2A_TIMEOUT_S": "1200",
        "ATHENA_MAX_LLM_CALLS": "40",
        "BLACKBOARD_DB": str(REPO / "data" / "blackboard.db"),
        "EVENTS_DB": str(REPO / "data" / "events.db"),
        "PYTHONUNBUFFERED": "1",
    }
)

argv = [sys.executable, "-m", "evaluation.runner", *sys.argv[1:]]
result = subprocess.run(argv, cwd=str(REPO), env=env)
raise SystemExit(result.returncode)
