# Athena — Project Rules Checklist

Mandatory rules for the whole rebuild. Status values: `todo` / `in progress` / `done`.
This repository currently contains only the Stage 0 skeleton plus the Stage 1–3
interop spike; the rules below are tracked, not yet implemented.

| # | Rule | Status | Notes |
|---|------|--------|-------|
| 1 | Blackboard (shared state) for agent context sharing | todo | Package `athena/blackboard/` reserved. |
| 2 | At least two tools exposed via MCP | todo | Stage 3 proves one MCP tool (`add_numbers`); a second is still needed. |
| 3 | Pydantic structured final output | todo | — |
| 4 | Configurable agentic patterns (sequential / parallel / hierarchical) | todo | Package `athena/config/` reserved. |
| 5 | Callback-based monitoring and logging | todo | Package `athena/observability/` reserved. |
| 6 | A2A between agents, one in ADK and one in CrewAI | in progress | Proven in the Stage 1–3 spike (`spike/`). |
| 7 | Only Google ADK and CrewAI as agent frameworks | done | No LangChain/LangGraph/AutoGen installed or used. CrewAI does vendor LangChain internally; we add no agent framework of our own. |
| 8 | Guardrails | todo | Package `athena/guardrails/` reserved. |
| 9 | Observability / tracing | todo | — |
| 10 | Evaluation with many runs and boxplots | todo | Package `athena/evaluation/` reserved. |
| 11 | Cloud deployment | todo | — |

## Architectural constraints

- Single virtual environment (`.venv`), single `requirements.txt`, exact pins.
- Every new dependency must come only from the two allowed agent frameworks or
  the protocols in use (`a2a-sdk`, `mcp`).
