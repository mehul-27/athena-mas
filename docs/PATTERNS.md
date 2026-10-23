# Athena Patterns

Four orchestration patterns, one agent per role, chosen by `--pattern`. All four
produce the same sections on the blackboard, so their reports, histories and
summaries are directly comparable.

```
sequential
  planner ──► researcher ──► analyst ──► writer ──► critic
              (3 tools)      (A2A)       (A2A)      (A2A)

parallel
  planner ──┬─► rag_researcher  ─┐
            ├─► web_researcher  ─┼─► analyst ──► writer ──► critic
            └─► calc_researcher ─┘   (A2A)       (A2A)      (A2A)
            (only those the plan names, running concurrently)

hierarchical
  planner ──► [ manager dispatches each PlanStep by assigned_to ]
                 ├─► rag_researcher
                 ├─► web_researcher
                 └─► calc_researcher
              ──► analyst ──► writer ──► critic
                                  ▲            │
                                  └────────────┘  re-run if approved=false
                                                  and severity=high
                                                  (<= ATHENA_MAX_REVISIONS)

p2p  (message passing; no agent reads the blackboard)
  planner ──msg──► researcher ──msg──► analyst ──msg──► writer ──msg──► critic
                   (tools)              (CrewAI)        (CrewAI)       (CrewAI)
  every stage output is written to the blackboard afterwards as an AUDIT record
```

## What each uses

| Pattern | Messaging | Concurrency | Worker nodes | Revision loop | Blackboard |
|---|---|---|---|---|---|
| sequential | blackboard | none | one researcher, 3 tools | no | agents read/write |
| parallel | blackboard | 3 researchers at once | 3 role-specific researchers | no | agents read/write |
| hierarchical | blackboard | none | 3 role-specific researchers | yes (high severity) | agents read/write |
| p2p | message only | none | one researcher, 3 tools | no | supervisor audits only |

## Course rules

- **Rule 1 (blackboard)** — the three blackboard patterns share state through
  `run_id`; p2p deliberately does not, and exists so the two architectures can
  be compared on the same question.
- **Rule 4 (patterns)** — sequential, parallel and hierarchical are the three
  named patterns; the registry is the switch point.
- **Rules 5/9 (monitoring, tracing)** — not in this step. Each stage already
  emits `{"stage", "stage_ms"}` and the A2A client counts bytes, which is what
  Step 6 will consume.

## Where the switch lives

`athena/patterns/__init__.py` maps a name to a builder:

```python
build_supervisor(run_id, pattern="parallel")   # athena/agents/supervisor.py
```

`Task.pattern` on the blackboard keeps the name as free text for the record;
`athena.run` validates it against the registry and fails with the valid names.
