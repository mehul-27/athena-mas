"""Runner behaviour: dry run, resumability, budgets, and honest failure records.

No test here starts a live run. The executor is injected, and the in-process
executor is exercised against a stub ``athena.run`` module, so the tests never
touch an LLM, a provider key, or the A2A server.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation import runner, schema  # noqa: E402

DATASET = schema.load_dataset(REPO_ROOT / "evaluation" / "dataset.json")
CONFIG = schema.load_config(REPO_ROOT / "evaluation" / "config.json")


def record_payload(entry: schema.PlanEntry, item: schema.EvalItem, **overrides) -> dict:
    """The raw dict a record is built from, *before* validation."""
    payload = {
        "run_key": entry.run_key,
        "run_id": f"run-{entry.run_key}",
        "prompt_id": item.id,
        "category": item.category,
        "difficulty": item.difficulty,
        "persona": item.persona,
        "pattern": entry.pattern,
        "context_sharing": entry.context_sharing,
        "seed": entry.seed,
        "repeat": entry.repeat,
        "tier": entry.tier,
        "backend": "fake",
        "config": {"pattern": entry.pattern},
        "started_at": "2025-01-01T00:00:00+00:00",
        "ended_at": "2025-01-01T00:00:30+00:00",
        "latency_s": 30.0,
        "tokens_in": 1000,
        "tokens_out": 500,
        "model_calls": 4,
        "est_cost_usd": 0.004,
        "cost_basis": "priced:test",
        "tool_calls": 2,
        "recovery_rounds": 0,
        "revision_rounds": 0,
        "evidence_count": 0,
        "terminal_state": "completed",
        "final_answer": "A recorded answer.",
        "key_points": ["point"],
        "citations": [],
        "evidence": [],
        "errors": [],
    }
    payload.update(overrides)
    return payload


def make_record(entry: schema.PlanEntry, item: schema.EvalItem, **overrides) -> dict:
    """A complete, valid record for the given plan entry."""
    return schema.RunRecord.model_validate(
        record_payload(entry, item, **overrides)
    ).model_dump(mode="json")


class FakeExecutor:
    """Records calls and returns a valid record; never runs anything."""

    def __init__(self, **overrides):
        self.calls: list[str] = []
        self.overrides = overrides

    def __call__(self, entry, item):
        self.calls.append(entry.run_key)
        overrides = dict(self.overrides)
        if callable(overrides.get("factory")):
            return overrides["factory"](entry, item)
        return make_record(entry, item, **overrides)


# --- dry run ---------------------------------------------------------------


def test_dry_run_exits_zero_and_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(runner, "DEFAULT_RESULTS_ROOT", tmp_path / "results")
    code = runner.main(["--dry-run"])
    output = capsys.readouterr().out
    assert code == runner.EXIT_OK
    assert "DRY RUN" in output
    assert "no results were written" in output
    assert not (tmp_path / "results").exists()


def test_dry_run_json_describes_the_plan(capsys):
    code = runner.main(["--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == runner.EXIT_OK
    assert payload["dry_run"] is True
    assert payload["dataset"]["items"] == len(DATASET.items)
    assert payload["plan"]["committed_entries"] >= 48
    assert payload["plan"]["selected_entries"] == payload["plan"]["committed_entries"]
    assert set(payload["plan"]["by_pattern"]) == set(schema.PATTERNS)
    assert payload["run"]["backend"] in schema.BACKENDS
    assert "crew_a2a_server" in payload["preconditions"]


def test_dry_run_honours_filters(capsys):
    code = runner.main(["--dry-run", "--json", "--pattern", "p2p", "--tier", "core"])
    payload = json.loads(capsys.readouterr().out)
    assert code == runner.EXIT_OK
    assert set(payload["plan"]["by_pattern"]) == {"p2p"}
    assert set(payload["plan"]["by_tier"]) == {"core"}


def test_the_plan_flag_selects_the_config_file(tmp_path, capsys):
    """``--plan`` is the name the task's CLI uses for the committed matrix file."""
    plan_path = tmp_path / "my-plan.json"
    plan_path.write_text(
        (REPO_ROOT / "evaluation" / "config.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    code = runner.main(["--dry-run", "--json", "--plan", str(plan_path)])
    payload = json.loads(capsys.readouterr().out)

    assert code == runner.EXIT_OK
    assert payload["plan"]["id"] == CONFIG.plan.id
    assert payload["plan"]["committed_entries"] == len(CONFIG.plan.entries)


def test_the_plan_flag_rejects_a_missing_file(tmp_path, capsys):
    code = runner.main(["--dry-run", "--plan", str(tmp_path / "absent.json")])
    assert code == runner.EXIT_INVALID
    assert "error" in capsys.readouterr().err.lower()


def test_dry_run_rejects_an_impossible_selection(capsys):
    code = runner.main(["--dry-run", "--only", "not-a-prompt"])
    assert code == runner.EXIT_INVALID
    assert "select no plan entries" in capsys.readouterr().err


def test_dry_run_rejects_a_broken_config(tmp_path, capsys):
    bad = tmp_path / "config.json"
    bad.write_text('{"version": 1}', encoding="utf-8")
    code = runner.main(["--dry-run", "--config", str(bad)])
    assert code == runner.EXIT_INVALID
    assert "not valid" in capsys.readouterr().err


# --- selection, resume, budgets -------------------------------------------


def test_select_entries_filters_without_inventing():
    entries = CONFIG.entries()
    selected = runner.select_entries(entries, patterns=["p2p"], tiers=["core"])
    assert selected
    assert all(entry.pattern == "p2p" and entry.tier == "core" for entry in selected)
    assert len(selected) <= len(entries)

    by_key = runner.select_entries(entries, run_keys=[entries[0].run_key])
    assert [entry.run_key for entry in by_key] == [entries[0].run_key]

    assert runner.select_entries(entries, prompts=["nope"]) == []


def test_run_writes_one_record_per_entry(tmp_path):
    executor = FakeExecutor()
    selected = CONFIG.entries()[:3]
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=selected, executor=executor, logger=lambda _m: None,
    )
    summary = batch.run()

    assert summary["records"] == 3
    lines = (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["run_key"] == selected[0].run_key
    assert first["terminal_state"] == "completed"
    assert first["latency_s"] == 30.0
    assert (tmp_path / "run_meta.json").is_file()
    assert (tmp_path / "plan.json").is_file()
    meta = json.loads((tmp_path / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["dataset_sha256"]
    assert meta["records_written"] == 3


def test_resume_skips_already_recorded_runs(tmp_path):
    selected = CONFIG.entries()[:3]
    first = FakeExecutor()
    runner.Runner(DATASET, CONFIG, tmp_path, entries=selected, executor=first,
                  logger=lambda _m: None).run()
    assert len(first.calls) == 3

    second = FakeExecutor()
    summary = runner.Runner(DATASET, CONFIG, tmp_path, entries=selected, executor=second,
                            logger=lambda _m: None).run()
    assert second.calls == []
    assert summary["skipped"] == 3
    assert len((tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_no_resume_reruns_everything(tmp_path):
    selected = CONFIG.entries()[:2]
    runner.Runner(DATASET, CONFIG, tmp_path, entries=selected, executor=FakeExecutor(),
                  logger=lambda _m: None).run()
    again = FakeExecutor()
    runner.Runner(DATASET, CONFIG, tmp_path, entries=selected, executor=again,
                  resume=False, logger=lambda _m: None).run()
    assert len(again.calls) == 2


def test_budget_max_runs_stops_the_batch(tmp_path):
    selected = CONFIG.entries()[:5]
    executor = FakeExecutor()
    batch = runner.Runner(DATASET, CONFIG, tmp_path, entries=selected, executor=executor,
                          max_runs=2, logger=lambda _m: None)
    summary = batch.run()
    assert len(executor.calls) == 2
    assert summary["records"] == 2
    assert summary["stopped_reason"] and "max_runs" in summary["stopped_reason"]
    # Unrun entries are listed for a later resume, never written as records.
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert len(plan["not_run"]) == 3
    assert len((tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_budget_usd_stops_the_batch(tmp_path):
    selected = CONFIG.entries()[:4]
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=selected,
        executor=FakeExecutor(est_cost_usd=0.4), max_usd=0.5, logger=lambda _m: None,
    )
    summary = batch.run()
    assert summary["records"] == 2
    assert "max_total_usd" in (summary["stopped_reason"] or "")


def test_a_run_over_the_per_run_budget_stops_the_batch(tmp_path):
    selected = CONFIG.entries()[:3]
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=selected,
        executor=FakeExecutor(est_cost_usd=5.0), logger=lambda _m: None,
    )
    summary = batch.run()
    assert summary["records"] == 1
    assert "max_run_usd" in (summary["stopped_reason"] or "")


def test_executor_exception_becomes_an_error_record_and_the_batch_continues(tmp_path):
    selected = CONFIG.entries()[:3]

    def explode(entry, item):
        raise RuntimeError("boom")

    batch = runner.Runner(DATASET, CONFIG, tmp_path, entries=selected, executor=explode,
                          logger=lambda _m: None)
    summary = batch.run()
    assert summary["records"] == 3
    records = [json.loads(line) for line in
               (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(record["terminal_state"] == "error" for record in records)
    assert all("boom" in record["errors"][0] for record in records)
    # A failed harness run carries no fabricated answer or cost.
    assert all(record["final_answer"] is None for record in records)


def test_timeout_records_are_not_counted_as_results(tmp_path):
    selected = CONFIG.entries()[:1]
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=selected,
        executor=FakeExecutor(terminal_state="timeout", latency_s=900.0, final_answer=None),
        logger=lambda _m: None,
    )
    batch.run()
    record = json.loads((tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["terminal_state"] == "timeout"
    assert record["final_answer"] is None
    assert record["outcome"] is None


# --- honest records --------------------------------------------------------


def test_completed_record_without_an_answer_is_rejected():
    entry = CONFIG.entries()[0]
    item = DATASET.by_id(entry.prompt_id)
    payload = record_payload(entry, item, final_answer="")
    with pytest.raises(Exception):
        schema.RunRecord.model_validate(payload)


def test_cost_without_a_run_is_rejected():
    entry = CONFIG.entries()[0]
    item = DATASET.by_id(entry.prompt_id)
    payload = record_payload(entry, item, terminal_state="skipped", cost_basis="not_run",
                            est_cost_usd=0.5)
    with pytest.raises(Exception):
        schema.RunRecord.model_validate(payload)


def test_read_records_survives_a_torn_line(tmp_path):
    path = tmp_path / "raw.jsonl"
    path.write_text('{"run_key": "a"}\nnot json\n', encoding="utf-8")
    records, problems = runner.read_records(path)
    assert len(records) == 1
    assert problems and "not valid JSON" in problems[0]


def test_done_keys_and_retry_failed():
    records = [
        {"run_key": "a", "terminal_state": "completed"},
        {"run_key": "b", "terminal_state": "error"},
        {"run_key": "c", "terminal_state": "failed"},
    ]
    assert runner.done_keys(records) == {"a", "b", "c"}
    assert runner.done_keys(records, retry_failed=True) == {"a", "c"}


# --- pricing ---------------------------------------------------------------


def test_known_model_is_priced_and_unknown_model_is_not():
    cost, basis = runner.estimate_cost(CONFIG, "gemini-2.5-flash", 1_000_000, 0)
    assert cost == pytest.approx(0.30)
    assert basis == "priced:gemini-2.5-flash"

    cost, basis = runner.estimate_cost(CONFIG, "some/unlisted-model", 1_000_000, 0)
    assert cost is None
    assert basis == "unknown_model"


def test_missing_tokens_are_not_priced_as_zero():
    cost, basis = runner.estimate_cost(CONFIG, "gemini-2.5-flash", None, None)
    assert cost is None
    assert "no token" in basis


# --- the in-process executor, against a stub athena.run -------------------


def _stub_run_module(monkeypatch, run_one):
    module = types.ModuleType("athena.run")
    module.run_one = run_one
    monkeypatch.setitem(sys.modules, "athena.run", module)


def test_in_process_executor_records_a_timeout(tmp_path, monkeypatch):
    async def slow(question, pattern):
        await asyncio.sleep(5)

    _stub_run_module(monkeypatch, slow)
    spec = runner.ExecutorSpec(
        config=CONFIG, dataset=DATASET, timeout_s=0.05,
        log_dir=tmp_path / "logs", model="test-model",
    )
    (tmp_path / "logs").mkdir()
    executor = runner.InProcessExecutor(spec)
    entry = CONFIG.entries()[0]
    record = executor(entry, DATASET.by_id(entry.prompt_id))
    assert record["terminal_state"] == "timeout"
    assert record["final_answer"] is None
    assert record["latency_s"] is not None and record["latency_s"] < 5
    assert "timeout" in record["errors"][0]
    assert (tmp_path / "logs" / f"{entry.run_key}.log").exists()


def test_in_process_executor_records_an_exception(tmp_path, monkeypatch):
    async def broken(question, pattern):
        raise RuntimeError("provider exploded")

    _stub_run_module(monkeypatch, broken)
    (tmp_path / "logs").mkdir()
    spec = runner.ExecutorSpec(
        config=CONFIG, dataset=DATASET, timeout_s=5.0,
        log_dir=tmp_path / "logs", model="test-model",
    )
    entry = CONFIG.entries()[0]
    record = runner.InProcessExecutor(spec)(entry, DATASET.by_id(entry.prompt_id))
    assert record["terminal_state"] == "error"
    assert "provider exploded" in record["errors"][0]
    assert record["est_cost_usd"] is None


def test_in_process_executor_records_a_completed_run(tmp_path, monkeypatch):
    async def ok(question, pattern):
        return {
            "run_id": "run-xyz",
            "wall_s": 12.0,
            "adk_tokens": 812,
            "tool_calls": 3,
            "recovery_rounds": 1,
            "revision_rounds": 2,
            "outcome": "approved",
            "critic_status": "approved",
            "approved": True,
            "failed": False,
            "stage_errors": [],
        }

    _stub_run_module(monkeypatch, ok)
    monkeypatch.setattr(runner, "blackboard_sections", lambda run_id: {
        "final_report": {
            "answer": "Tungsten is W, atomic number 74.",
            "key_points": ["W", "74"],
            "citations": [
                {"source_type": "web", "source_ref": "https://www.rsc.org/x",
                 "quote_or_summary": "Tungsten W 74"}
            ],
            "confidence": 0.8,
            "tools_used": ["web_search"],
        },
        "evidence": [
            {"id": "ev-1", "source_type": "web", "source_ref": "https://www.rsc.org/x",
             "content": "Tungsten W atomic number 74", "score": 0.9,
             "retrieved_by": "web_researcher"}
        ],
        "critique": {"approved": True, "severity": "low", "issues": []},
    })
    monkeypatch.setattr(runner, "event_totals", lambda run_id: {
        "tokens_in": 900, "tokens_out": 120, "reasoning_tokens": 0,
        "model_calls": 6, "tool_calls": 3, "recovery_events": 1, "errors": 0,
    })
    (tmp_path / "logs").mkdir()
    spec = runner.ExecutorSpec(
        config=CONFIG, dataset=DATASET, timeout_s=5.0,
        log_dir=tmp_path / "logs", model="gemini-2.5-flash",
    )
    entry = CONFIG.entries()[0]
    record = runner.InProcessExecutor(spec)(entry, DATASET.by_id(entry.prompt_id))

    assert record["terminal_state"] == "completed"
    assert record["run_id"] == "run-xyz"
    assert record["tokens_in"] == 900
    assert record["tokens_out"] == 120
    assert record["model_calls"] == 6
    assert record["tool_calls"] == 3
    assert record["recovery_rounds"] == 1
    assert record["final_answer"].startswith("Tungsten")
    assert record["citations"][0]["source_ref"] == "https://www.rsc.org/x"
    assert record["cost_basis"].startswith("priced:")
    assert record["critic_approved"] is True
    # The record must round-trip through the schema the analyser will read.
    schema.RunRecord.model_validate(record)


def test_subprocess_backend_refuses_to_invent_a_summary(tmp_path, monkeypatch):
    class FakeProcess:
        returncode = 1

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(runner, "blackboard_sections", lambda run_id: {})
    monkeypatch.setattr(runner, "event_totals", lambda run_id: None)
    (tmp_path / "logs").mkdir()
    spec = runner.ExecutorSpec(
        config=CONFIG, dataset=DATASET, timeout_s=5.0,
        log_dir=tmp_path / "logs", model="test-model",
    )
    entry = CONFIG.entries()[0]
    record = runner.SubprocessExecutor(spec)(entry, DATASET.by_id(entry.prompt_id))
    assert record["terminal_state"] == "failed"
    assert record["final_answer"] is None
    assert any("without writing a summary" in error for error in record["errors"])


# --- pricing from the repo's bundled table ---------------------------------


def test_the_bundled_price_table_prices_a_model_the_config_does_not():
    """``data/pricing.json`` is the fallback when ``config.json`` has no entry."""
    from athena.observability import cost as catalog

    model = "llama-3.3-70b-versatile"
    entry = catalog.match_model(model)
    if entry is None:  # pragma: no cover - the bundled table ships with the repo
        pytest.skip("bundled price table has no entry for this model")
    assert model not in CONFIG.pricing.models, "the config must not carry this model"

    expected = float(entry["input_per_mtok"])
    cost, basis = runner.estimate_cost(CONFIG, model, 1_000_000, 0)
    assert cost == pytest.approx(expected, rel=1e-6)
    assert basis.startswith("priced:")
    assert "data/pricing.json" in basis


def test_a_model_in_neither_price_table_stays_unpriced():
    cost, basis = runner.estimate_cost(CONFIG, "totally-made-up/model-2099", 1_000, 1_000)
    assert cost is None
    assert basis == "unknown_model"


def test_pricing_summary_names_the_table_it_would_use():
    priced = runner.pricing_summary(CONFIG, "gemini-2.5-flash")
    assert priced["priced"] is True
    assert "config.pricing" in priced["basis"]

    unpriced = runner.pricing_summary(CONFIG, "totally-made-up/model-2099")
    assert unpriced["priced"] is False
    assert "null" in unpriced["basis"]


def test_pricing_status_reports_the_bundled_table():
    status = runner.pricing_status()
    assert status["source"] in {
        "athena.observability.cost",
        "evaluation/config.json pricing only",
    }
    if status["source"] == "athena.observability.cost":
        assert status["table"]
        assert isinstance(status["entries"], int)


def test_run_meta_records_the_pricing_source(tmp_path):
    out_dir = tmp_path / "results"
    batch = runner.Runner(
        DATASET, CONFIG, out_dir,
        entries=CONFIG.entries()[:1],
        executor=FakeExecutor(),
        logger=lambda _message: None,
    )
    batch.run()
    meta = json.loads((out_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert "pricing" in meta
    assert meta["pricing"]["source"]
    assert "dataset_sha256" in meta
    assert meta["records_written"] == 1


def test_dry_run_json_reports_the_pricing_basis(capsys):
    code = runner.main(["--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == runner.EXIT_OK
    assert "pricing" in payload["run"]
    assert {"model", "priced", "basis"} <= set(payload["run"]["pricing_summary"])


def test_subprocess_backend_reads_the_api_summary_as_a_fallback(tmp_path, monkeypatch):
    """The demo API writes ``.run/runs/<id>.summary.json``; the runner can read it."""

    class FakeProcess:
        returncode = 0

        def wait(self, timeout=None):
            return 0

    fixed = uuid.UUID("00000000-0000-0000-0000-000000000123")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(runner, "blackboard_sections", lambda run_id: {})
    monkeypatch.setattr(runner, "event_totals", lambda run_id: None)
    monkeypatch.setattr(runner.uuid, "uuid4", lambda: fixed)
    monkeypatch.setattr(runner, "RUN_SUMMARY_DIR", tmp_path / "api-runs")
    (tmp_path / "api-runs").mkdir()
    (tmp_path / "api-runs" / f"{fixed}.summary.json").write_text(
        json.dumps({"failed": False, "tool_calls": 4, "recovery_rounds": 2}),
        encoding="utf-8",
    )
    (tmp_path / "logs").mkdir()

    spec = runner.ExecutorSpec(
        config=CONFIG, dataset=DATASET, timeout_s=5.0,
        log_dir=tmp_path / "logs", model="test-model",
    )
    entry = CONFIG.entries()[0]
    record = runner.SubprocessExecutor(spec)(entry, DATASET.by_id(entry.prompt_id))

    assert record["run_id"] == str(fixed)
    assert record["tool_calls"] == 4
    assert record["recovery_rounds"] == 2
    assert not any("without writing a summary" in error for error in record["errors"])
