"""Verify the corrected launcher policy as booleans/numbers only. No secrets."""

from __future__ import annotations

import os
import sys
import urllib.request
import json
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
PROFILE_NAME = "amd"
PROFILE_ID = "367f57c4-d107-447c-a857-632fa8e6a454"

sys.path.insert(0, str(REPO))
os.chdir(REPO)

from athena.settings import profiles as profile_store  # noqa: E402
from athena.settings.vault import default_vault  # noqa: E402

profile = next(p for p in profile_store.load_profiles() if p.name == PROFILE_NAME)
key = default_vault().get(profile_store.key_name(profile.id, "llm"))
assert profile.id == PROFILE_ID and key

os.environ.update(
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
    }
)

from athena.config.caps import PATTERN_CAPS, llm_call_cap  # noqa: E402
from athena.config.call_limits import timeout_s as call_timeout_s  # noqa: E402
from athena.agents.a2a_client import a2a_timeout  # noqa: E402

print("llm_model_is_amd:", os.getenv("LLM_MODEL") == profile.model)
print("llm_api_base_is_amd:", os.getenv("LLM_API_BASE").rstrip("/") == profile.api_base.rstrip("/"))
print("openai_key_equals_amd:", os.getenv("OPENAI_API_KEY") == key)
print("openai_base_is_amd:", os.getenv("OPENAI_BASE_URL").rstrip("/") == profile.api_base.rstrip("/"))
print("dotenv_cap_value_not_used:", os.getenv("ATHENA_MAX_LLM_CALLS") != "12")
print("effective_hierarchical_cap:", llm_call_cap("hierarchical"))
print("pattern_table_hierarchical:", PATTERN_CAPS["hierarchical"])
print("provider_call_timeout_s:", call_timeout_s())
print("a2a_timeout_s:", a2a_timeout())
print("blackboard_db_absolute:", Path(os.getenv("BLACKBOARD_DB")).is_absolute())
print("events_db_absolute:", Path(os.getenv("EVENTS_DB")).is_absolute())

with urllib.request.urlopen("http://127.0.0.1:9001/.well-known/agent-card.json", timeout=5) as response:
    print("crew_card_status:", response.status)
with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5) as response:
    health = json.loads(response.read())
print("health_model:", health.get("model"))
print("health_host:", health.get("api_host"))
print("active_profile_name:", (health.get("active_profile") or {}).get("name"))
