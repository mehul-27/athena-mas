"""Offline dataset profile for the 30 selected entries. No model calls."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
DATASET = REPO / "evaluation" / "dataset.json"
PLAN = REPO / "evaluation" / "hierarchical_30.json"

data = json.loads(DATASET.read_text(encoding="utf-8"))
items = {item["id"]: item for item in data["items"]}
cfg = json.loads(PLAN.read_text(encoding="utf-8"))
entries = cfg["plan"]["entries"]

print("selected entries:", len(entries))
print("distinct prompts:", len({e["prompt_id"] for e in entries}))
print("tiers:", dict(Counter(e["tier"] for e in entries)))
print("categories:", dict(sorted(Counter(items[e["prompt_id"]]["category"] for e in entries).items())))
print("personas:", dict(sorted(Counter(items[e["prompt_id"]]["persona"] for e in entries).items())))
print("difficulty:", dict(sorted(Counter(items[e["prompt_id"]]["difficulty"] for e in entries).items())))

print("\ncategory run counts:")
for category, count in sorted(Counter(items[e["prompt_id"]]["category"] for e in entries).items()):
    print(f"  {category}: {count}")
print("categories with 3:", sum(1 for c in Counter(items[e["prompt_id"]]["category"] for e in entries).values() if c == 3))
print("categories with 2:", sum(1 for c in Counter(items[e["prompt_id"]]["category"] for e in entries).values() if c == 2))

print("\nvolatile prompt keys:")
for prompt_id in ("cv-01", "cv-02"):
    item = items.get(prompt_id)
    if not item:
        print(prompt_id, "MISSING")
        continue
    print(prompt_id, "category", item["category"], "persona", item["persona"])
    print("  answer_key:", repr(item.get("answer_key")))
    print("  keys:", sorted(item.keys()))
    print("  rubric criteria:", [r.get("id") for r in item.get("rubric", [])])

print("\nprompt ids:", sorted({e["prompt_id"] for e in entries}))
