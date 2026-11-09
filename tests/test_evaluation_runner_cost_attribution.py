"""Per-agent cost attribution and resumed-budget accounting.

Every test here is offline and fake-only: the event store is replaced below
``event_totals``, the batch executor is injected, and no provider is called.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from athena.observability import store as store_module  # noqa: E402
from athena.observability.events import Event  # noqa: E402
from evaluation import runner, schema  # noqa: E402
from tests.test_evaluation_runner import make_record  # noqa: E402

DATASET = schema.load_dataset(REPO_ROOT / "evaluation" / "dataset.json")
CONFIG = schema.load_config(REPO_ROOT / "evaluation" / "config.json")

RUN_ID = "run-cost-attribution"

ADK_MODEL = "gemini-2.5-flash"  # input 0.30, output 2.50 per Mtok
CREW_MODEL = "gpt-4o-mini"      # input 0.15, output 0.60 per Mtok
UNPRICED_MODEL = "totally-made-up/model-2099"


class FakeEventStore:
    def __init__(self, events: list[Event]) -> None:
        self._events = events
        self.requested: list[str] = []

    def events(self, run_id: str) -> list[Event]:
        self.requested.append(run_id)
        return list(self._events)


def use_events(monkeypatch, events: list[Event]) -> FakeEventStore:
    fake = FakeEventStore(events)

    def from_env(cls):
        return fake

    monkeypatch.setattr(store_module.SQLiteEventStore, "from_env", classmethod(from_env))
    return fake


def make_event(
    kind: str,
    agent: str,
    *,
    framework: str = "crewai",
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    model: str | None = None,
    reasoning_tokens: int | None = None,
) -> Event:
    extra = {}
    if model is not None:
        extra["model"] = model
    if reasoning_tokens is not None:
        extra["reasoning_tokens"] = reasoning_tokens
    return Event(
        run_id=RUN_ID,
        kind=kind,
        agent=agent,
        framework=framework,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        extra=extra,
    )


def provenance(*, adk: str = ADK_MODEL, crew: str = CREW_MODEL) -> schema.ModelProvenance:
    return schema.ModelProvenance(
        adk_model=adk,
        crew_model=crew,
        models_differ=bool(adk and crew and adk != crew),
    )


def merged_record(
    *, totals: dict | None, usage_by_agent: dict | None, models: schema.ModelProvenance,
) -> schema.RunRecord:
    entry = CONFIG.entries()[0]
    item = DATASET.by_id(entry.prompt_id)
    record = runner.base_record(
        entry, item, CONFIG, model=models.adk_model, timeout_s=10.0,
        backend="fake", provenance=models,
    )
    merged = runner.merge_run_facts(
        record,
        run_id="fake-run",
        summary={"failed": False},
        sections={"final_report": {"answer": "A recorded answer."}},
        totals=totals,
        config=CONFIG,
        model=models.adk_model,
        latency_s=1.0,
        errors=[],
        notes=[],
        provenance=models,
        usage_by_agent=usage_by_agent,
    )
    return schema.RunRecord.model_validate(merged)


# --- event detail ----------------------------------------------------------


def test_event_usage_carries_each_model_and_keeps_the_mirror_deduplicated(monkeypatch):
    use_events(
        monkeypatch,
        [
            make_event(
                "model_call", "planner", framework="adk",
                tokens_in=1000, tokens_out=100, model=ADK_MODEL,
            ),
            make_event("model_call", "writer", tokens_in=800, tokens_out=200),
            make_event(
                "a2a_call", "writer", framework="adk",
                tokens_in=800, tokens_out=200,
            ),
            make_event(
                "a2a_call", "analyst", framework="adk",
                tokens_in=400, tokens_out=50,
            ),
        ],
    )

    detail: dict = {}
    totals = runner.event_totals(RUN_ID, detail)

    assert totals["tokens_in"] == 2200
    assert totals["tokens_out"] == 350
    assert totals["model_calls"] == 2

    agents = detail["usage_by_agent"]
    assert agents["planner"]["source"] == "model_call"
    assert agents["planner"]["segments"][0]["model"] == ADK_MODEL
    # The server's own model_call is authoritative; the mirrored ack is absent.
    assert agents["writer"]["source"] == "model_call"
    assert len(agents["writer"]["segments"]) == 1
    assert agents["writer"]["segments"][0]["model"] is None
    # An ack-only role falls back to the acknowledgement, once.
    assert agents["analyst"]["source"] == "a2a_call"
    assert len(agents["analyst"]["segments"]) == 1


def test_event_totals_without_a_detail_dict_is_unchanged(monkeypatch):
    """The legacy call form and its exact totals keys remain supported."""
    use_events(
        monkeypatch,
        [make_event("model_call", "planner", framework="adk", tokens_in=5, tokens_out=2)],
    )

    totals = runner.event_totals(RUN_ID)

    assert totals == {
        "tokens_in": 5,
        "tokens_out": 2,
        "reasoning_tokens": 0,
        "model_calls": 1,
        "tool_calls": 0,
        "recovery_events": 0,
        "errors": 0,
    }


# --- per-agent pricing -----------------------------------------------------


def test_mixed_adk_and_crew_models_are_priced_separately():
    models = provenance()
    usage = {
        "planner": {
            "source": "model_call",
            "segments": [{
                "source": "model_call", "framework": "adk", "model": ADK_MODEL,
                "tokens_in": 1_000_000, "tokens_out": 0,
            }],
        },
        "writer": {
            "source": "model_call",
            "segments": [{
                "source": "model_call", "framework": "crewai", "model": None,
                "tokens_in": 0, "tokens_out": 1_000_000,
            }],
        },
    }

    record = merged_record(
        totals={"tokens_in": 1_000_000, "tokens_out": 1_000_000},
        usage_by_agent=usage,
        models=models,
    )

    # ADK input at 0.30 + crew output at 0.60; pricing everything against the
    # ADK model would have produced 0.30 + 2.50 = 2.80.
    assert record.est_cost_usd == pytest.approx(0.90)
    assert record.pricing.priced is True
    assert ADK_MODEL in record.cost_basis
    assert CREW_MODEL in record.cost_basis


def test_a2a_mirror_is_priced_once_at_the_crew_model(monkeypatch):
    use_events(
        monkeypatch,
        [
            make_event(
                "model_call", "planner", framework="adk",
                tokens_in=1000, tokens_out=100, model=ADK_MODEL,
            ),
            make_event("model_call", "writer", tokens_in=800, tokens_out=200),
            make_event(
                "a2a_call", "writer", framework="adk",
                tokens_in=800, tokens_out=200,
            ),
        ],
    )
    detail: dict = {}
    totals = runner.event_totals(RUN_ID, detail)

    record = merged_record(
        totals=totals, usage_by_agent=detail["usage_by_agent"], models=provenance(),
    )

    expected = (
        1000 / 1_000_000 * 0.30 + 100 / 1_000_000 * 2.50   # planner, ADK model
        + 800 / 1_000_000 * 0.15 + 200 / 1_000_000 * 0.60  # writer, crew model
    )
    assert record.est_cost_usd == pytest.approx(expected)
    # Counting the mirrored ack as well would double the writer's share.
    assert record.est_cost_usd != pytest.approx(expected + 0.00024)


def test_a_partly_unpriced_run_stays_unknown_and_names_the_model():
    models = provenance(crew=UNPRICED_MODEL)
    usage = {
        "planner": {
            "source": "model_call",
            "segments": [{
                "source": "model_call", "framework": "adk", "model": ADK_MODEL,
                "tokens_in": 1_000_000, "tokens_out": 0,
            }],
        },
        "writer": {
            "source": "model_call",
            "segments": [{
                "source": "model_call", "framework": "crewai", "model": None,
                "tokens_in": 0, "tokens_out": 1_000_000,
            }],
        },
    }

    record = merged_record(
        totals={"tokens_in": 1_000_000, "tokens_out": 1_000_000},
        usage_by_agent=usage,
        models=models,
    )

    assert record.est_cost_usd is None
    assert record.pricing.priced is False
    assert record.pricing.unpriced_models == [UNPRICED_MODEL]
    assert record.cost_basis.startswith("partly_unpriced:")
    assert "0.3" in record.cost_basis, "the known subtotal is reported, not spent"


def test_an_entirely_unpriced_run_has_no_numeric_cost():
    models = provenance(adk=UNPRICED_MODEL, crew=UNPRICED_MODEL)
    usage = {
        "planner": {
            "source": "model_call",
            "segments": [{
                "source": "model_call", "framework": "adk", "model": None,
                "tokens_in": 500, "tokens_out": 100,
            }],
        },
    }

    record = merged_record(
        totals={"tokens_in": 500, "tokens_out": 100},
        usage_by_agent=usage,
        models=models,
    )

    assert record.est_cost_usd is None
    assert record.pricing.priced is False
    assert record.pricing.unpriced_models == [UNPRICED_MODEL]
    assert record.cost_basis.startswith("unpriced:")


def test_a_segment_with_no_model_is_not_guessed():
    models = provenance(crew="")
    usage = {
        "writer": {
            "source": "model_call",
            "segments": [{
                "source": "model_call", "framework": "crewai", "model": None,
                "tokens_in": 100, "tokens_out": 20,
            }],
        },
    }

    record = merged_record(
        totals={"tokens_in": 100, "tokens_out": 20},
        usage_by_agent=usage,
        models=models,
    )

    assert record.est_cost_usd is None
    assert record.pricing.priced is False
    assert record.pricing.unpriced_models == ["writer (no model recorded)"]
    assert "no model recorded" in record.cost_basis


def test_legacy_totals_without_agent_detail_keep_the_single_model_estimate():
    models = provenance(crew=ADK_MODEL)

    record = merged_record(
        totals={"tokens_in": 1_000_000, "tokens_out": 0},
        usage_by_agent=None,
        models=models,
    )

    assert record.est_cost_usd == pytest.approx(0.30)
    assert record.cost_basis == "priced:" + ADK_MODEL


def test_mixed_model_aggregate_counts_do_not_guess_a_token_split():
    record = merged_record(
        totals={"tokens_in": 1_000_000, "tokens_out": 0},
        usage_by_agent=None, models=provenance(),
    )
    assert record.est_cost_usd is None
    assert record.pricing.priced is False
    assert record.cost_basis.startswith("unattributed_usage:")
    assert "no rate" not in record.pricing.basis


# --- resumed budget --------------------------------------------------------


class FakeExecutor:
    def __init__(self, **overrides) -> None:
        self.calls: list[str] = []
        self.overrides = overrides

    def __call__(self, entry, item):
        self.calls.append(entry.run_key)
        return make_record(entry, item, **self.overrides)


def test_resume_restores_known_spend_before_the_next_invocation(tmp_path):
    selected = CONFIG.entries()[:5]
    first = FakeExecutor(est_cost_usd=0.4)
    first_summary = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=selected, executor=first,
        max_usd=0.5, logger=lambda _m: None,
    ).run()
    assert first_summary["records"] == 2
    assert first_summary["budget"]["spent_usd_estimate"] == pytest.approx(0.8)

    second = FakeExecutor(est_cost_usd=0.4)
    second_summary = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=selected, executor=second,
        max_usd=0.5, logger=lambda _m: None,
    ).run()

    assert second.calls == []
    assert "max_total_usd" in (second_summary["stopped_reason"] or "")
    assert second_summary["budget"]["runs_started"] == 2
    assert second_summary["budget"]["spent_usd_estimate"] == pytest.approx(0.8)


def test_resume_counts_failed_and_retried_attempts_against_the_cap(tmp_path):
    entries = CONFIG.entries()[:3]
    retried_entry = entries[0]
    item = DATASET.by_id(retried_entry.prompt_id)
    first_attempt = make_record(
        retried_entry, item,
        terminal_state="infra_error", final_answer=None, latency_s=1.0,
        est_cost_usd=None, cost_basis="unknown_model",
        errors=["HTTP 503 Service Unavailable"],
    )
    second_attempt = make_record(retried_entry, item, attempt=2)
    (tmp_path / "raw.jsonl").write_text(
        "\n".join(json.dumps(record) for record in (first_attempt, second_attempt)) + "\n",
        encoding="utf-8",
    )

    executor = FakeExecutor()
    summary = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=entries, executor=executor,
        max_runs=3, logger=lambda _m: None,
    ).run()

    assert executor.calls == [entries[1].run_key]
    assert summary["budget"]["runs_started"] == 3
    assert "max_runs=3" in (summary["stopped_reason"] or "")
    assert len((tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_legacy_records_restore_attempts_and_unknown_costs(tmp_path):
    entries = CONFIG.entries()[:3]
    legacy = [
        {"run_key": "legacy-a", "terminal_state": "failed", "latency_s": 2.0},
        {"run_key": "legacy-b", "terminal_state": "error", "latency_s": 0.5},
    ]
    raw = tmp_path / "raw.jsonl"
    raw.write_text("\n".join(json.dumps(line) for line in legacy) + "\n", encoding="utf-8")

    executor = FakeExecutor()
    summary = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=entries, executor=executor,
        max_runs=2, logger=lambda _m: None,
    ).run()

    assert executor.calls == []
    assert summary["budget"]["runs_started"] == 2
    assert summary["budget"]["spent_usd_estimate"] == 0.0
    assert summary["budget"]["runs_with_unknown_cost"] == 2
    assert "not counted as zero" in summary["budget"]["usd_cap_note"]
    assert raw.read_text(encoding="utf-8").count("\n") == 2


def test_resume_keeps_known_and_unknown_costs_distinct(tmp_path):
    entries = CONFIG.entries()[:4]
    item = DATASET.by_id(entries[0].prompt_id)
    prior = [
        make_record(entries[0], item, est_cost_usd=0.2),
        make_record(
            entries[1], DATASET.by_id(entries[1].prompt_id),
            est_cost_usd=None, cost_basis="unknown_model",
        ),
    ]
    (tmp_path / "raw.jsonl").write_text(
        "\n".join(json.dumps(record) for record in prior) + "\n", encoding="utf-8"
    )

    executor = FakeExecutor(est_cost_usd=0.4)
    summary = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=entries, executor=executor,
        max_usd=0.5, logger=lambda _m: None,
    ).run()

    assert len(executor.calls) == 1
    assert summary["budget"]["runs_started"] == 3
    assert summary["budget"]["spent_usd_estimate"] == pytest.approx(0.6)
    assert summary["budget"]["runs_with_unknown_cost"] == 1
    assert "max_total_usd" in (summary["stopped_reason"] or "")


def test_resume_restores_wall_budget_and_preserves_metadata_overhead(tmp_path, monkeypatch):
    entries = CONFIG.entries()[:2]
    prior = make_record(entries[0], DATASET.by_id(entries[0].prompt_id), latency_s=4.0)
    (tmp_path / "raw.jsonl").write_text(json.dumps(prior) + "\n", encoding="utf-8")
    (tmp_path / "run_meta.json").write_text(
        json.dumps({"budget": {"elapsed_wall_s": 12.0, "runs_started": 1}}),
        encoding="utf-8")
    monkeypatch.setattr(runner.time, "monotonic", lambda: 100.0)
    config = CONFIG.model_copy(update={"budget": CONFIG.budget.model_copy(
        update={"max_total_wall_s": 10.0})})
    executor = FakeExecutor()
    batch = runner.Runner(DATASET, config, tmp_path, entries=entries, executor=executor,
                          logger=lambda _m: None)
    batch.pending(entries)
    summary = batch.run()
    assert executor.calls == []
    assert "max_total_wall_s" in summary["stopped_reason"]
    assert summary["budget"]["elapsed_wall_s"] == 12.0
    assert summary["budget"]["runs_started"] == 1, "restoration must occur once"


def test_legacy_wall_budget_uses_recorded_execution_time(monkeypatch):
    monkeypatch.setattr(runner.time, "monotonic", lambda: 100.0)
    budget = runner.BudgetTracker(CONFIG.budget.model_copy(update={"max_total_wall_s": 5.0}))
    budget.restore([{"latency_s": 3.0}, {"latency_s": 3.0}])
    assert "max_total_wall_s" in budget.exhausted()
    assert budget.as_dict()["usd_cap_enforceable"] is False


def test_resume_preserves_a_prior_per_run_budget_stop():
    budget = runner.BudgetTracker(CONFIG.budget.model_copy(
        update={"max_run_usd": 0.1, "max_total_usd": 10.0}))
    budget.restore([{"est_cost_usd": 0.2}])
    assert "max_run_usd" in budget.exhausted()


def test_wall_restore_counts_attempts_appended_after_the_last_metadata_write(tmp_path):
    entries = CONFIG.entries()[:3]
    records = [make_record(entry, DATASET.by_id(entry.prompt_id), latency_s=4.0)
               for entry in entries[:2]]
    (tmp_path / "raw.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (tmp_path / "run_meta.json").write_text(
        json.dumps({"budget": {"elapsed_wall_s": 12.0, "runs_started": 1}}),
        encoding="utf-8")
    config = CONFIG.model_copy(update={"budget": CONFIG.budget.model_copy(
        update={"max_total_wall_s": 15.0})})
    executor = FakeExecutor()
    summary = runner.Runner(DATASET, config, tmp_path, entries=entries, executor=executor,
                            logger=lambda _m: None).run()
    assert executor.calls == []
    assert summary["budget"]["elapsed_wall_s"] >= 16.0
    assert "max_total_wall_s" in summary["stopped_reason"]
