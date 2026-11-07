"""researcher: an ADK LlmAgent with MCP tool servers attached.

Two shapes, same code: ``build_researcher`` gets all three servers (the
sequential baseline), ``build_role_researcher`` gets exactly one (the parallel
and hierarchical workers).

The model decides which tools to call. What it cannot do is decide what the
evidence says: every tool result is written by the ``evidence_recorder`` hook,
which reads the actual response.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

from athena.agents import bb_io
from athena.agents.budget import Budget, make_budget_callbacks
from athena.agents.evidence_recorder import make_evidence_recorder
from athena.agents.requirements import finding_requirement
from athena.agents.routing import preferred_roles, source_guidance
from athena.agents.tool_args import MalformedCallGuard
from athena.agents.toolset_health import HealthCheckedToolset
from athena.agents.untrusted import UNTRUSTED_DATA_RULE
from athena.blackboard import Blackboard, BlackboardStore
from athena.config.llm import get_adk_model
from athena.observability.recorder import get_recorder
from athena.tools import mcp_stdio, registry
from athena.tools.settings import REPO_ROOT
from athena.tools.web.urls import plain_http_url

TOOL_DISCIPLINE = (
    "Call each search tool at most once per distinct query. Use `fetch_page` on "
    "relevant primary-source URLs; do not fetch mirrors or aggregators. Pass "
    "the URL as a plain HTTP(S) string, never a Markdown link. After a tool "
    "returns, its result is recorded automatically; do not repeat the call, and "
    "finish with a one-sentence summary. " + UNTRUSTED_DATA_RULE
)

INSTRUCTION = """You are the researcher. Gather evidence for the task below.

Rules:
- Choose sources to fit the question. For software, programming-language,
  library/API, release, standards, or current factual questions, start with
  `web_search` and fetch the relevant official/primary pages. Do not require a
  RAG call for every question.
- For questions about supplied local documents, use `rag_search` and base claims
  on the returned passages.
- Use `rag_search` as a supplementary source only when the corpus is plausibly
  relevant. If the corpus is unrelated, do not treat it as evidence.
- A search snippet is a discovery hint, not sufficient evidence for a specific
  claim. Fetch the page before relying on it.
- If the task involves numbers, arithmetic, totals or a trend, call `calculate`
  or `forecast` and put the exact figures in the expression/values.
- When a web result is a relevant primary source, call `fetch_page` on that URL
  before relying on it. Cite the fetched page text, not the search snippet.
- Do not fetch PDF URLs: the fetcher does not extract PDF content. Prefer HTML
  article pages with substantive body text over navigation-heavy landing pages.
- Never fetch a search-engine URL (DuckDuckGo, Bing, Mojeek, etc.). If a search
  is throttled, try one broader synonym query, not a search-results page.
- If a required finding still lacks a primary source, use the remaining fetch
  budget to find it; if it remains unavailable, record the gap instead of
  inventing evidence.
- Mirrors, reposts and aggregators are corroboration only; do not treat them as
  independent primary sources.
- Do not answer the question and do not summarise. The evidence is already being
  recorded. When you have enough, reply with the single word DONE.
"""

ROLE_INSTRUCTIONS = {
    "rag_researcher": "Search the NASA document corpus. Call `rag_search` once "
    "with a short focused query (top_k=3). Ignore the other tools.",
    "web_researcher": "Your FIRST `web_search` query must be a general technical "
    "query with no company, vehicle, or product name (for example, 'cold gas "
    "thruster nitrogen working principle'), max_results=3. Search for "
    "organization/product-specific terms only in a later query if budget remains. "
    "If a search is throttled, try one broader synonym query; if two distinct "
    "queries are throttled and no fallback is configured, stop searching and "
    "record the gap instead of exhausting the budget. Never fetch a "
    "search-engine result page. Then call `fetch_page` on the most relevant "
    "HTML article result(s); if one returns 403, 404, empty, or unsupported "
    "content, try the next distinct HTML result instead of repeating it. Do not "
    "fetch URLs ending in .pdf; fetch_page cannot extract PDFs. Prefer article "
    "pages with substantive body text over navigation-heavy landing pages; use "
    "max_chars=20000 for long technical pages. Ignore the NASA corpus and "
    "calculator tools.",
    "calc_researcher": "Work the numbers. Call `calculate` or `forecast` with "
    "the exact figures the task mentions. Ignore the other tools.",
}

SERVERS = {
    "rag": "athena.tools.rag_server",
    "web": "athena.tools.web_server",
    "calc": "athena.tools.calc_server",
}

ROLE_SERVER = {
    "rag_researcher": "rag",
    "web_researcher": "web",
    "calc_researcher": "calc",
}


_MCP_ENV_PREFIXES = (
    "ATHENA_", "SEARCH_", "SEARXNG_", "WEB_FETCH_", "RAG_",
    "EMBEDDING_", "FASTEMBED_",
)
_MCP_ENV_EXACT = {
    "PATH", "PYTHONPATH", "PYTHONIOENCODING", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "SYSTEMROOT",
    "WINDIR", "COMSPEC", "CHROMA_PATH", "CORPUS_DIR",
}


def mcp_environment() -> dict[str, str]:
    """Environment for local MCP tool servers: settings, never model secrets."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _MCP_ENV_EXACT or key.startswith(_MCP_ENV_PREFIXES)
    }


def build_toolset(
    module: str,
    timeout: float = 120.0,
    tools: list[str] | None = None,
    server: str = "",
    agent: str = "researcher",
    run_id: str = "",
    store: Any = None,
) -> McpToolset:
    """One stdio MCP server as an ADK toolset, filtered to ``tools``.

    Filtering happens before the model sees a schema, so a disabled tool is not
    merely hidden in the UI: it is never offered for a call. MCP's stdio client
    uses a restricted default environment, so search/fetch/RAG settings must be
    passed explicitly or the tool process silently falls back to defaults.

    Two properties keep a lost toolset from becoming a silent zero-evidence run:

    * ``errlog`` is our own never-closed file handle, not ``sys.stderr``. ADK
      binds ``errlog=sys.stderr`` as a default *at import time*; under a
      process-wide ``redirect_stderr`` to a log file that was later closed,
      ``subprocess`` then failed in ``msvcrt.get_osfhandle`` before the child
      existed, every server vanished, and the run reported "completed".
    * The toolset records its own load outcome, so a failure is an observable
      error event and the run is marked degraded rather than looking clean.
    """
    return HealthCheckedToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", module],
                cwd=str(REPO_ROOT),
                env=mcp_environment(),
            ),
            timeout=timeout,
        ),
        tool_filter=list(tools) if tools is not None else None,
        **mcp_stdio.toolset_kwargs(),
    ).use_health(
        server=server or module,
        agent=agent,
        required=list(tools or ()),
        run_id=run_id,
        store=store,
    )


def plain_fetch_urls(callback):
    """Normalize fetch_page URLs before the MCP tool receives them."""
    def before_tool(tool, args, tool_context):
        if getattr(tool, "name", "") == "fetch_page" and isinstance(args, dict):
            args["url"] = plain_http_url(args.get("url"))
        return callback(tool, args, tool_context)

    return before_tool


def selection_guard(
    allowed: set[str],
    agent: str,
    run_enabled: set[str] | None = None,
):
    """Refuse a tool outside this worker's assignment.

    The toolset filter is the primary boundary. This guard is defence in depth:
    a server that ignores filtering, or a model that hallucinates a tool name,
    still cannot run it.

    A worker is assigned only the tools its role owns. A name that is enabled
    for the run but absent from this worker is therefore a worker-assignment
    restriction, not a user-level selection restriction.
    """
    enabled = set(allowed if run_enabled is None else run_enabled)

    def before_tool(tool, args, tool_context):
        name = getattr(tool, "name", "") or "tool"
        if name in allowed:
            return None
        if name in enabled:
            blocked_by = "worker_assignment"
            error_type = "ToolNotAssigned"
            message = f"Blocked: {name} is not assigned to {agent}."
        else:
            blocked_by = "user_selection"
            error_type = "ToolDisabled"
            message = f"Blocked: {name} is not enabled for this run."
        return {
            "isError": True,
            "error": message,
            "error_type": error_type,
            "blocked_by": blocked_by,
            "tool": name,
        }

    return before_tool


def _toolsets(
    selection: tuple[str, ...] | None,
    agent: str = "researcher",
    run_id: str = "",
    store: Any = None,
) -> list[McpToolset]:
    return [
        build_toolset(
            SERVERS[server],
            tools=names,
            server=server,
            agent=agent,
            run_id=run_id,
            store=store,
        )
        for server, names in registry.server_filters(selection).items()
    ]


def _selection_instruction(selection: tuple[str, ...] | None) -> str:
    return (
        f"{registry.capability_block(selection)}\n"
        f"{registry.limit_note(selection)}"
    )


def _compose_guard(
    allowed: set[str],
    agent: str,
    budget_guard,
    run_enabled: set[str] | None = None,
):
    guard = selection_guard(allowed, agent, run_enabled)
    fetch_and_budget = plain_fetch_urls(budget_guard)
    # One allowance per worker: a second worker's first mistake is still
    # correctable, and no worker can loop on an incomplete call.
    malformed = MalformedCallGuard(agent)

    def before_tool(tool, args, tool_context):
        blocked = guard(tool, args, tool_context)
        if blocked is not None:
            return blocked
        # Refuse an incomplete call before it reaches the MCP server, so the
        # model gets a readable validation error instead of a transport failure.
        invalid = malformed.check(tool, args)
        if invalid is not None:
            return invalid
        return fetch_and_budget(tool, args, tool_context)

    return before_tool


def build_researcher(
    run_id: str,
    store: BlackboardStore | None = None,
    task_text: str | None = None,
    sink: Any = None,
) -> LlmAgent:
    """The sequential researcher: the enabled tools across all MCP servers."""
    text = task_text if task_text is not None else bb_io.task_text(
        Blackboard.open(run_id, store)
    )
    selection = registry.task_selection(run_id, store)
    run_enabled = set(registry.enabled_ids(selection))
    allowed = set(run_enabled)
    budget = Budget.from_env("researcher")
    before_model, budget_guard = make_budget_callbacks(
        budget, get_recorder(), "researcher"
    )
    return LlmAgent(
        name="researcher",
        model=get_adk_model("researcher"),
        description="Gathers evidence with the enabled research tools.",
        instruction=(
            f"{INSTRUCTION}\n{TOOL_DISCIPLINE}\n{source_guidance(text)}\n"
            f"{_selection_instruction(selection)}\n"
            f"{finding_requirement(text)}\nThe task:\n{text}\n"
        ),
        tools=_toolsets(selection, "researcher", run_id, store),
        before_model_callback=before_model,
        before_tool_callback=_compose_guard(
            allowed, "researcher", budget_guard, run_enabled
        ),
        after_tool_callback=make_evidence_recorder(run_id, store, sink=sink),
    )


def build_role_researcher(
    role: str,
    run_id: str,
    store: BlackboardStore | None = None,
    task_text: str | None = None,
    sink: Any = None,
) -> LlmAgent:
    """A worker researcher with only its selected MCP tools attached."""
    text = task_text if task_text is not None else bb_io.task_text(
        Blackboard.open(run_id, store)
    )
    selection = registry.task_selection(run_id, store)
    run_enabled = set(registry.enabled_ids(selection))
    allowed_list = registry.role_tools(role, selection)
    allowed = set(allowed_list)
    budget = Budget.from_env(role)
    before_model, budget_guard = make_budget_callbacks(budget, get_recorder(), role)
    tools = (
        [
            build_toolset(
                SERVERS[ROLE_SERVER[role]],
                tools=list(allowed_list),
                server=ROLE_SERVER[role],
                agent=role,
                run_id=run_id,
                store=store,
            )
        ]
        if allowed
        else []
    )
    role_instruction = ROLE_INSTRUCTIONS[role]
    if role == "web_researcher" and allowed_list == ("fetch_page",):
        role_instruction = (
            "Fetch the exact URL(s) named in the task with `fetch_page`. Do not "
            "search the web; web search is disabled for this run."
        )
    elif role == "web_researcher" and allowed_list == ("web_search",):
        role_instruction = (
            "Search the web with `web_search` once (max_results=3). Page fetching "
            "is disabled for this run, so treat snippets as discovery hints, not "
            "as sufficient evidence."
        )
    elif role == "calc_researcher":
        usable = " or ".join(f"`{name}`" for name in allowed_list)
        role_instruction = (
            f"Work only the numbers explicitly requested using {usable}. Each "
            "calculation needs a stated purpose and expected output; use one call "
            "per distinct quantity and stop when that result is obtained. Do not "
            "invent inputs. If the task does not state numeric inputs and does "
            "not ask for a numeric result, call no tool and reply DONE. Do not "
            "perform web research."
        ) if allowed_list else role_instruction
    disabled_note = (
        ""
        if allowed
        else f"\nNo tools are enabled for this role. State that the task cannot be "
        f"researched with the selected tools and reply DONE."
    )
    return LlmAgent(
        name=role,
        model=get_adk_model("researcher"),
        description=f"The {role} worker.",
        instruction=(
            f"You are the {role}. {role_instruction}\n{TOOL_DISCIPLINE}\n"
            f"{source_guidance(text)}\n{_selection_instruction(selection)}\n"
            f"{finding_requirement(text)}\n"
            f"The task:\n{text}\nReply DONE when finished.{disabled_note}"
        ),
        tools=tools,
        before_model_callback=before_model,
        before_tool_callback=_compose_guard(
            allowed, role, budget_guard, run_enabled
        ),
        after_tool_callback=make_evidence_recorder(run_id, store, sink=sink),
    )


class AssignedWorker(BaseAgent):
    """Runs its worker only when the plan gives it a step.

    The plan does not exist when the graph is built, so the decision is deferred
    to run time and stays deterministic: a worker runs iff some step names it.
    """

    role: str
    worker: Any
    run_id: str
    store: Any = None

    async def _run_async_impl(self, ctx: InvocationContext):
        blackboard = Blackboard.open(self.run_id, self.store)
        plan = bb_io.section(blackboard, "plan")
        assigned = {step.assigned_to for step in plan.steps} if plan else set()
        selection = registry.task_selection(self.run_id, self.store)
        available = set(registry.available_roles(selection))

        # A planner that writes no executable step must not leave the research
        # pool empty. Fall back to the source class the question needs, but only
        # among workers the user actually enabled.
        if not (assigned & available):
            task = bb_io.section(blackboard, "task")
            chosen = preferred_roles(task.question if task else "", available)
            if not chosen and available:
                chosen = sorted(
                    available, key=lambda role: registry.ROLE_ORDER.index(role)
                )[:1]
            assigned = set(chosen)

        if not registry.role_enabled(self.role, selection):
            reason = "tools disabled by the user"
            self._note_skip(reason)
            yield Event(
                author=self.name,
                custom_metadata={
                    "skipped": True,
                    "reason": reason,
                    "role": self.role,
                },
            )
            return
        if self.role not in assigned:
            self._note_skip("no plan step")
            yield Event(
                author=self.name,
                custom_metadata={"skipped": True, "reason": "no plan step"},
            )
            return
        async for event in self.worker.run_async(ctx):
            yield event

    def _note_skip(self, reason: str) -> None:
        recorder = get_recorder()
        if recorder is not None:
            recorder.emit(
                "skip",
                self.role,
                "adk",
                payload_summary=f"{self.role} skipped: {reason}",
                extra={"reason": reason},
            )
