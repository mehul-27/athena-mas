"""The focused 36-run hierarchical plan, provenance, attempts and pricing honesty.

Everything here is offline: no LLM, no provider key, no A2A server. The plan file
and the dataset are the real deliverables, so the validation tests parse the real
files rather than a fixture that could drift from them.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation import runner, schema  # noqa: E402

DATASET_PATH = REPO_ROOT / "evaluation" / "dataset.json"
CONFIG_PATH = REPO_ROOT / "evaluation" / "config.json"
HIER_PATH = REPO_ROOT / "evaluation" / "hierarchical_36.json"

DATASET = schema.load_dataset(DATASET_PATH)
CONFIG = schema.load_config(CONFIG_PATH)

CORE = ["gf-01", "te-02", "ne-02", "cm-02", "cv-01", "ms-02",
        "cs-01", "dr-01", "nc-01", "ie-01", "pi-01", "tp-01"]
BREADTH = ["gf-02", "te-01", "ne-01", "cm-03", "cv-02", "ms-04",
           "cs-04", "dr-04", "nc-03", "ie-03", "pi-03", "tp-04"]


@pytest.fixture(scope="module")
def hier_payload() -> dict:
    return json.loads(HIER_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def hier() -> schema.ExperimentConfig:
    return schema.load_config(HIER_PATH)


def entry_payload(prompt_id: str, tier: str = "core", repeat: int = 0,
                  pattern: str = "hierarchical", context_sharing: str = "blackboard",
                  run_key: str | None = None) -> dict:
    return {
        "run_key": run_key or f"{prompt_id}__{pattern}__r{repeat}",
        "prompt_id": prompt_id,
        "pattern": pattern,
        "context_sharing": context_sharing,
        "seed": None,
        "repeat": repeat,
        "tier": tier,
        "note": "",
    }


def plan_payload(entries: list[dict], **overrides) -> dict:
    payload = {
        "id": "test-plan",
        "scope": "focused",
        "target_runs": len(entries),
        "entries": entries,
    }
    payload.update(overrides)
    return payload


def plan_with(entries: list[dict], **overrides) -> dict:
    """``plan_payload`` that also allows overriding ``entries`` itself."""
    payload = plan_payload(entries)
    payload.update(overrides)
    return payload


def plan_declaring(target_runs: int, entries: list[dict]) -> dict:
    return {
        "id": "test-plan",
        "scope": "focused",
        "target_runs": target_runs,
        "entries": entries,
    }


def thirty_six_hierarchical_entries() -> list[dict]:
    """A valid 36-entry hierarchical shape, built independently of the file."""
    entries = [entry_payload(prompt_id, "core") for prompt_id in CORE]
    entries += [entry_payload(prompt_id, "breadth") for prompt_id in BREADTH]
    entries += [entry_payload(prompt_id, "variance", repeat=1, run_key=f"{prompt_id}__hierarchical__r1")
                for prompt_id in CORE]
    return entries


# --- the committed 36-run plan --------------------------------------------


def test_hierarchical_plan_file_loads(hier):
    assert hier.plan.id == "hierarchical-36"
    assert hier.plan.scope == "focused"
    assert hier.plan.target == 36
    assert len(hier.plan.entries) == 36
    assert hier.plan.minimum_runs == schema.MIN_PLAN_RUNS_FOCUSED


def test_every_entry_is_hierarchical_on_the_blackboard(hier):
    assert {entry.pattern for entry in hier.plan.entries} == {"hierarchical"}
    assert {entry.context_sharing for entry in hier.plan.entries} == {"blackboard"}
    assert hier.matrix.backend == "subprocess"


def test_tiers_are_twelve_each(hier):
    counts: dict[str, int] = {}
    for entry in hier.plan.entries:
        counts[entry.tier] = counts.get(entry.tier, 0) + 1
    assert counts == {"core": 12, "breadth": 12, "variance": 12}


def test_prompt_ids_are_exactly_the_specified_ones(hier):
    by_tier: dict[str, set[str]] = {"core": set(), "breadth": set(), "variance": set()}
    for entry in hier.plan.entries:
        by_tier[entry.tier].add(entry.prompt_id)
    assert sorted(by_tier["core"]) == sorted(CORE)
    assert sorted(by_tier["breadth"]) == sorted(BREADTH)
    assert sorted(by_tier["variance"]) == sorted(CORE)


def test_twenty_four_distinct_prompts_each_in_the_dataset(hier):
    ids = {entry.prompt_id for entry in hier.plan.entries}
    assert len(ids) == 24
    unknown = sorted(ids - {item.id for item in DATASET.items})
    assert not unknown, f"plan names prompt ids that are not in dataset.json: {unknown}"


def test_every_category_appears_exactly_three_times(hier):
    counts: dict[str, int] = {}
    for entry in hier.plan.entries:
        category = DATASET.by_id(entry.prompt_id).category
        counts[category] = counts.get(category, 0) + 1
    assert len(counts) == 12
    assert set(counts) == set(schema.CATEGORIES)
    assert all(value == 3 for value in counts.values()), counts


def test_variance_entries_repeat_a_core_prompt_with_a_distinct_key(hier):
    core_keys = {entry.prompt_id for entry in hier.plan.entries if entry.tier == "core"}
    variance = [entry for entry in hier.plan.entries if entry.tier == "variance"]
    assert len(variance) == 12
    for entry in variance:
        assert entry.repeat == 1
        assert entry.prompt_id in core_keys
        assert entry.run_key.endswith("__hierarchical__r1"), entry.run_key
        assert "variance" == entry.tier


def test_hierarchical_budget_and_timeout(hier):
    assert hier.budget.max_runs == 40, "40 is the hard attempt cap"
    assert hier.budget.max_total_usd == pytest.approx(8.0)
    assert hier.budget.max_run_usd == pytest.approx(0.6)
    assert hier.matrix.per_run_timeout_s == pytest.approx(1200.0)
    assert hier.budget.max_runs > len(hier.plan.entries), "one retry fits inside the cap"


def test_hierarchical_plan_does_not_claim_seeded_reproduction(hier):
    assert hier.claims_seeded_reproduction is False
    assert hier.matrix.seeds == []
    assert all(entry.seed is None for entry in hier.plan.entries)
    assert "stochastic" in hier.plan.note.lower()
    assert "stochastic" in hier.note.lower()


def test_hierarchical_pricing_table_is_empty_on_purpose(hier):
    """No invented rate for the active model: it stays unpriced, honestly."""
    assert hier.pricing.models == {}
    assert hier.pricing.note
    assert runner.pricing_summary(hier, "groq/qwen/qwen3.8-27b")["priced"] is False


def test_the_old_incomplete_batch_is_preserved_and_not_the_new_benchmark():
    old = REPO_ROOT / "evaluation" / "results" / "20261106T194245Z"
    assert old.is_dir(), "the old 6-attempt batch must be preserved"
    assert (old / "raw.jsonl").is_file()
    records = [
        json.loads(line)
        for line in (old / "raw.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 6
    # It is not the 36-run plan, so nothing may count it as the new benchmark.
    assert len(records) != 36
    assert all(record.get("backend") == "in_process" for record in records)


# --- schema: dataset minimum vs plan minimum ------------------------------


def test_the_two_minimums_are_independent_constants():
    assert schema.MIN_ITEMS == 44
    assert schema.MIN_PLAN_RUNS == 48
    assert schema.MIN_PLAN_RUNS_FOCUSED == 30
    assert schema.PLAN_MINIMUM_RUNS == {"full": 48, "focused": 30}


def test_a_valid_36_run_hierarchical_plan_loads():
    plan = schema.Plan.model_validate(plan_payload(thirty_six_hierarchical_entries()))
    assert len(plan.entries) == 36
    assert plan.target == 36


def test_a_29_entry_focused_plan_is_rejected():
    entries = thirty_six_hierarchical_entries()[:29]
    with pytest.raises(Exception) as excinfo:
        schema.Plan.model_validate(plan_payload(entries))
    assert ">= 30" in str(excinfo.value)


def test_a_30_entry_focused_plan_is_accepted_but_48_is_still_needed_for_full():
    entries = thirty_six_hierarchical_entries()[:30]
    schema.Plan.model_validate(plan_payload(entries))
    with pytest.raises(Exception) as excinfo:
        schema.Plan.model_validate(plan_payload(entries, scope="full"))
    assert ">= 48" in str(excinfo.value)


def test_a_36_entry_multi_pattern_plan_is_rejected():
    """A focused plan is focused: 36 entries cannot span four patterns."""
    entries = thirty_six_hierarchical_entries()
    entries.append(entry_payload("gf-03", "breadth", pattern="sequential"))
    payload = {
        "version": 1,
        "matrix": {"patterns": list(schema.PATTERNS), "backend": "in_process"},
        "plan": plan_payload(entries),
    }
    with pytest.raises(Exception) as excinfo:
        schema.ExperimentConfig.model_validate(payload)
    assert "plan patterns" in str(excinfo.value)


def test_a_hierarchical_plan_with_the_wrong_context_sharing_is_rejected():
    entries = thirty_six_hierarchical_entries()
    entries[0] = entry_payload(CORE[0], "core", context_sharing="message_passing")
    with pytest.raises(Exception) as excinfo:
        schema.Plan.model_validate(plan_payload(entries))
    assert "shares context via" in str(excinfo.value)


def test_duplicate_run_keys_are_rejected():
    entries = thirty_six_hierarchical_entries()
    entries[-1] = dict(entries[0])
    with pytest.raises(Exception) as excinfo:
        schema.Plan.model_validate(plan_payload(entries))
    assert "duplicate run_keys" in str(excinfo.value)


def test_a_plan_without_a_variance_tier_is_rejected():
    entries = [entry for entry in thirty_six_hierarchical_entries()
               if entry["tier"] != "variance"]
    with pytest.raises(Exception):
        schema.Plan.model_validate(plan_payload(entries))


def test_target_runs_must_equal_the_real_entry_count():
    entries = thirty_six_hierarchical_entries()
    with pytest.raises(Exception) as excinfo:
        schema.Plan.model_validate(plan_declaring(40, entries))
    assert "target_runs" in str(excinfo.value)
    with pytest.raises(Exception) as excinfo:
        schema.Plan.model_validate(plan_declaring(36, entries[:-1]))
    assert "target_runs" in str(excinfo.value)
    # Declaring exactly the entry count is accepted.
    assert len(schema.Plan.model_validate(plan_declaring(36, entries)).entries) == 36


def test_the_existing_72_run_config_still_validates():
    assert len(CONFIG.plan.entries) == 72
    assert CONFIG.plan.scope == "full"
    assert CONFIG.plan.target == 72
    assert CONFIG.plan.minimum_runs == schema.MIN_PLAN_RUNS
    assert set(CONFIG.plan.patterns()) == set(schema.PATTERNS)
    assert CONFIG.budget.max_runs == 72
    # Re-read from disk: the file itself is preserved, not just the model.
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert len(payload["plan"]["entries"]) == 72
    assert payload["plan"]["target_runs"] == 72


def test_a_config_claiming_seeded_reproduction_is_rejected():
    payload = json.loads(HIER_PATH.read_text(encoding="utf-8"))
    payload["claims_seeded_reproduction"] = True
    with pytest.raises(Exception) as excinfo:
        schema.ExperimentConfig.model_validate(payload)
    assert "seed" in str(excinfo.value).lower()


def test_seed_propagation_is_declared_false():
    """Nothing in the pipeline forwards a seed; the schema must not imply it does."""
    assert schema.SEED_PROPAGATED_TO_PROVIDER is False


# --- provenance (B1) ------------------------------------------------------


@pytest.fixture(scope="module")
def hier_provenance() -> schema.ModelProvenance:
    return schema.load_config(HIER_PATH), runner.resolve_provenance()


def test_run_meta_records_both_models_and_a_redacted_host(tmp_path, monkeypatch):
    unavailable = {"LLM_MODEL": "adk/model-a", "LLM_API_BASE": "https://gw.example/v1"}
    profile = {"LLM_MODEL": "crew/model-b", "LLM_API_BASE": "https://crew.example/v1"}
    provenance = runner.resolve_provenance(env=unavailable, profile=profile)
    assert provenance.adk_model == "adk/model-a"
    assert provenance.crew_model == "crew/model-b"
    assert provenance.models_differ is True
    assert provenance.provider == "adk"
    assert provenance.crew_provider == "crew"
    assert provenance.api_host == "https://gw.example/v1"
    assert provenance.crew_api_host == "https://crew.example/v1"
    assert provenance.profile_applied is True
    assert provenance.env_source == "profile"

    batch = runner.Runner(
        DATASET, CONFIG, tmp_path,
        entries=CONFIG.entries()[:1],
        executor=_GoodExecutor(),
        provenance=provenance,
        logger=lambda _message: None,
    )
    batch.run()
    meta = json.loads((tmp_path / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["adk_model"] == "adk/model-a"
    assert meta["crew_model"] == "crew/model-b"
    assert meta["models_differ"] is True
    assert meta["api_host"] == "https://gw.example/v1"
    assert meta["profile_applied"] is True
    assert meta["env_source"] == "profile"
    assert meta["provenance"]["crew_model"] == "crew/model-b"

    record = json.loads(
        (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert record["provenance"]["adk_model"] == "adk/model-a"
    assert record["provenance"]["crew_model"] == "crew/model-b"
    assert record["config"]["crew_model"] == "crew/model-b"
    assert record["config"]["seed_propagated_to_provider"] is False


def test_provenance_never_carries_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret-value")
    provenance = runner.resolve_provenance(
        env={
            "LLM_MODEL": "groq/x",
            "LLM_API_KEY": "sk-super-secret-value",
            "LLM_API_BASE": "https://user:pw@gw.example/v1?token=abc",
        },
        profile={},
    )
    blob = json.dumps(provenance.model_dump(mode="json"))
    assert "sk-super-secret-value" not in blob
    assert "pw@" not in blob
    assert "token=abc" not in blob
    assert provenance.api_host == "https://gw.example/v1"


def test_provenance_falls_back_to_one_model_when_no_profile_is_active():
    provenance = runner.resolve_provenance(
        env={"LLM_MODEL": "only/one"}, profile={}
    )
    assert provenance.adk_model == "only/one"
    assert provenance.crew_model == "only/one"
    assert provenance.models_differ is False
    assert provenance.profile_applied is False
    assert provenance.env_source == "env"


def test_the_redacting_helper_is_the_repository_one():
    from athena.tools.diagnostics import redact_url

    raw = "https://user:secret@gw.example:8443/v1?key=abc#frag"
    assert runner.redact_host(raw) == redact_url(raw)


# --- output directory (B4) ------------------------------------------------


def test_an_unwritable_out_directory_returns_exit_invalid(tmp_path, capsys, monkeypatch):
    # A *file* where the results directory should be: mkdir must fail with an
    # OSError that the runner turns into a message plus EXIT_INVALID (B4).
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")

    class NoPreflight:
        def __call__(self, **_kwargs):
            return {
                "crew_a2a_server": "up",
                "llm_api_key": "present",
                "model": "gemini-2.5-flash",
                "crew_model": "gemini-2.5-flash",
            }

    monkeypatch.setattr(runner, "preflight", NoPreflight())
    code = runner.main([
        "--out", str(blocker / "results"), "--limit", "1", "--model", "gemini-2.5-flash",
    ])
    captured = capsys.readouterr()
    assert code == runner.EXIT_INVALID
    assert "error" in captured.err.lower()
    assert "results directory" in captured.err
    # No partial run_meta.json was written anywhere.
    assert not (blocker / "results" / "run_meta.json").exists()
    assert not Path(str(blocker) + "/results").exists()


def test_prepare_out_dir_reports_a_bad_path_without_a_traceback(tmp_path):
    # A file blocks the directory, which is the portable way to make mkdir fail.
    blocker = tmp_path / "out"
    blocker.write_text("occupied", encoding="utf-8")
    with pytest.raises(runner.OutputDirectoryError) as excinfo:
        runner.prepare_out_dir(blocker)
    assert "results directory" in str(excinfo.value)


def test_prepare_out_dir_accepts_a_good_path(tmp_path):
    out = runner.prepare_out_dir(tmp_path / "ok")
    assert out.is_dir()
    assert (out / "logs").is_dir()


# --- stream flushing (B2) -------------------------------------------------


class RecordingStream(io.StringIO):
    """A stream that counts reconfigure/flush calls, like a redirected file."""

    def __init__(self) -> None:
        super().__init__()
        self.configures: list[dict] = []
        self.flushes = 0

    def reconfigure(self, **kwargs):  # noqa: D401 - mirrors TextIOWrapper
        self.configures.append(kwargs)

    def flush(self):
        self.flushes += 1
        super().flush()


def test_configure_streams_asks_for_write_through_and_flushes(monkeypatch):
    out, err = RecordingStream(), RecordingStream()
    monkeypatch.setattr(runner.sys, "stdout", out)
    monkeypatch.setattr(runner.sys, "stderr", err)
    runner.configure_streams()

    for stream in (out, err):
        assert stream.configures, "reconfigure must be called"
        assert stream.configures[0].get("write_through") is True, (
            "without write_through a redirected log stays empty until exit (B2)"
        )
        assert stream.configures[0].get("encoding") == "utf-8"
        assert stream.flushes >= 1


def test_configure_streams_survives_a_stream_that_rejects_write_through(monkeypatch):
    class Picky(RecordingStream):
        def reconfigure(self, **kwargs):
            if "write_through" in kwargs:
                raise TypeError("unexpected keyword argument 'write_through'")
            super().reconfigure(**kwargs)

    out = Picky()
    monkeypatch.setattr(runner.sys, "stdout", out)
    monkeypatch.setattr(runner.sys, "stderr", Picky())
    runner.configure_streams()
    assert out.configures and "encoding" in out.configures[0]


def test_progress_is_flushed_before_and_after_every_attempt(tmp_path):
    """Every attempt logs a line before and after, and each line is flushed."""
    stream = RecordingStream()

    class FlushSpy:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def __call__(self, message: str) -> None:
            self.lines.append(message)
            runner.flushed_logger(message, stream=stream)

        def index_of(self, needle: str, *, nth: int = 0) -> int:
            hits = [i for i, line in enumerate(self.lines) if needle in line]
            return hits[nth]

    spy = FlushSpy()
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path,
        entries=CONFIG.entries()[:2],
        executor=_GoodExecutor(),
        logger=spy,
    )
    batch.run()

    starting = [line for line in spy.lines if "starting -" in line]
    finished = [line for line in spy.lines if "    -> attempt" in line]
    assert len(starting) == 2
    assert len(finished) == 2
    assert stream.flushes >= len(spy.lines), "every logged line must be flushed"
    # Each attempt's "starting" line comes before its own "-> attempt" line.
    assert spy.index_of("starting -", nth=0) < spy.index_of("    -> attempt", nth=0)
    assert spy.index_of("starting -", nth=1) < spy.index_of("    -> attempt", nth=1)


def test_flushed_logger_tolerates_a_broken_stream():
    class Broken(RecordingStream):
        def flush(self):
            raise ValueError("I/O operation on closed file")

    stream = Broken()
    runner.flushed_logger("hello", stream=stream)  # must not raise
    assert "hello" in stream.getvalue()


# --- attempts: uniqueness, accounting, cap, retries ----------------------


class _GoodExecutor:
    """A complete, valid record; never runs anything."""

    def __init__(self, overrides: dict | None = None) -> None:
        self.calls: list[str] = []
        self.overrides = dict(overrides or {})

    def __call__(self, entry, item, *, attempt: int = 1, attempt_id: str = ""):
        from tests.test_evaluation_runner import make_record

        self.calls.append(entry.run_key)
        record = make_record(entry, item, **self.overrides)
        record["attempt"] = attempt
        record["attempt_id"] = attempt_id or runner.new_attempt_id(entry.run_key, attempt)
        return record


class _FlakyExecutor:
    """Fails the first attempt with an infra fault, succeeds on the retry."""

    def __init__(self, state: str = "infra_error") -> None:
        self.calls: list[tuple[str, int]] = []
        self.state = state

    def __call__(self, entry, item, *, attempt: int = 1, attempt_id: str = ""):
        from tests.test_evaluation_runner import make_record

        self.calls.append((entry.run_key, attempt))
        if attempt == 1:
            record = make_record(
                entry, item,
                terminal_state=self.state,
                final_answer=None,
                latency_s=0.5,
                est_cost_usd=None,
                cost_basis="not_run" if self.state == "infra_error" else "unknown_model",
                errors=["HTTP 503 Service Unavailable"],
            )
        else:
            record = make_record(entry, item)
        record["attempt"] = attempt
        record["attempt_id"] = attempt_id or runner.new_attempt_id(entry.run_key, attempt)
        return record


def test_attempt_ids_are_unique_per_execution(tmp_path):
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path,
        entries=CONFIG.entries()[:3],
        executor=_GoodExecutor(),
        logger=lambda _m: None,
    )
    batch.run()
    records = [
        json.loads(line)
        for line in (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    ids = [record["attempt_id"] for record in records]
    assert len(ids) == 3
    assert len(set(ids)) == 3, "an attempt id must identify one execution"
    assert all(record["attempt"] == 1 for record in records)
    assert batch.accounting.attempted == 3


def test_a_retry_appends_a_second_record_and_never_rewrites_the_first(tmp_path):
    executor = _FlakyExecutor()
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path,
        entries=CONFIG.entries()[:1],
        executor=executor,
        retry_failed=True,
        logger=lambda _m: None,
    )
    summary = batch.run()

    records = [
        json.loads(line)
        for line in (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2, "the failed attempt is kept, the retry is a new record"
    assert records[0]["terminal_state"] == "infra_error"
    assert records[0]["attempt"] == 1
    assert records[1]["attempt"] == 2
    assert records[1]["terminal_state"] == "completed"
    assert records[0]["attempt_id"] != records[1]["attempt_id"]
    assert len({record["attempt_id"] for record in records}) == 2
    assert summary["attempts"]["attempted"] == 2
    assert summary["attempts"]["retries"] == 1
    assert summary["attempts"]["completed"] == 1
    assert summary["attempts"]["infra_errors"] == 1


def test_a_completed_run_is_never_retried(tmp_path):
    executor = _GoodExecutor()
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path,
        entries=CONFIG.entries()[:2],
        executor=executor,
        retry_failed=True,
        logger=lambda _m: None,
    )
    batch.run()
    assert len(executor.calls) == 2
    assert batch.accounting.retries == 0


def test_a_critic_rejection_is_a_result_and_is_not_retried(tmp_path):
    executor = _GoodExecutor(
        {
            "terminal_state": "completed",
            "critic_status": "rejected",
            "critic_approved": False,
        }
    )
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path,
        entries=CONFIG.entries()[:1],
        executor=executor,
        retry_failed=True,
        logger=lambda _m: None,
    )
    batch.run()
    assert len(executor.calls) == 1, "a critic verdict is data, not an infra fault"
    assert batch.accounting.retries == 0


def test_a_pipeline_failure_is_not_retried(tmp_path):
    executor = _GoodExecutor(
        {"terminal_state": "failed", "final_answer": None, "latency_s": 4.0,
         "est_cost_usd": None, "cost_basis": "unknown_model"}
    )
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path,
        entries=CONFIG.entries()[:1],
        executor=executor,
        retry_failed=True,
        logger=lambda _m: None,
    )
    batch.run()
    assert len(executor.calls) == 1
    assert batch.accounting.retries == 0


def test_should_retry_is_the_only_place_that_decides():
    assert runner.should_retry({"terminal_state": "infra_error"}) is False
    assert runner.should_retry({"terminal_state": "error"}) is False
    assert runner.should_retry({
        "terminal_state": "error", "errors": ["ValueError: synthetic bug"]
    }) is False
    assert runner.should_retry({
        "terminal_state": "infra_error",
        "errors": ["HTTP 503 Service Unavailable"],
    }) is True
    assert runner.should_retry({"terminal_state": "timeout"}) is True
    assert runner.should_retry({"terminal_state": "completed"}) is False
    assert runner.should_retry({"terminal_state": "failed"}) is False
    assert runner.should_retry(
        {"terminal_state": "infra_error", "critic_status": "rejected"}
    ) is False


def test_the_batch_hard_attempt_cap_is_enforced(tmp_path):
    """max_runs counts attempts, so a retry consumes the hard cap (40 here)."""
    executor = _FlakyExecutor()
    entries = CONFIG.entries()[:5]
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path,
        entries=entries,
        executor=executor,
        retry_failed=True,
        max_runs=6,   # 5 entries; the retries push past it
        logger=lambda _m: None,
    )
    summary = batch.run()
    records = [
        json.loads(line)
        for line in (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 6, "six attempts, not five entries"
    assert summary["attempts"]["attempted"] == 6
    assert summary["stopped_reason"] and "max_runs=6" in summary["stopped_reason"]
    assert summary["attempts"]["attempted"] <= 6


def test_the_focused_plan_cannot_exceed_40_attempts(tmp_path):
    """With 36 entries and one retry each the cap would be 72; 40 stops it."""
    hier = schema.load_config(HIER_PATH)
    executor = _FlakyExecutor()
    batch = runner.Runner(
        DATASET, hier, tmp_path,
        entries=hier.entries(),
        executor=executor,
        retry_failed=True,
        logger=lambda _m: None,
    )
    summary = batch.run()
    assert summary["attempts"]["attempted"] == 40
    assert summary["attempts"]["hard_max_attempts"] == 40
    assert summary["stopped_reason"] and "max_runs=40" in summary["stopped_reason"]
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan["not_run"], "the unrun tail is listed for a later resume"
    assert len(plan["entries"]) == 36
    records = [
        json.loads(line)
        for line in (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 40
    assert len({record["attempt_id"] for record in records}) == 40


def test_attempt_accounting_counts_every_terminal_class():
    accounting = schema.AttemptAccounting(
        planned=10, attempted=4, completed=1, failed=1, timeout=1, infra_errors=1,
        hard_max_attempts=10,
    )
    assert accounting.hard_max_attempts == 10
    with pytest.raises(Exception):
        schema.AttemptAccounting(attempted=5, hard_max_attempts=4)
    with pytest.raises(Exception):
        schema.AttemptAccounting(attempted=1, completed=2, hard_max_attempts=4)


def test_resume_skips_completed_keys_and_does_not_double_count(tmp_path):
    executor = _GoodExecutor()
    selected = CONFIG.entries()[:3]
    runner.Runner(DATASET, CONFIG, tmp_path, entries=selected, executor=executor,
                  logger=lambda _m: None).run()

    again = _GoodExecutor()
    summary = runner.Runner(DATASET, CONFIG, tmp_path, entries=selected,
                            executor=again, logger=lambda _m: None).run()
    assert again.calls == []
    assert summary["skipped"] == 3
    assert summary["attempts"]["attempted"] == 0
    assert summary["attempts"]["skipped"] == 3
    assert len((tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_resume_continues_attempt_numbering_for_a_retried_key(tmp_path):
    executor = _FlakyExecutor()
    selected = CONFIG.entries()[:1]
    runner.Runner(DATASET, CONFIG, tmp_path, entries=selected, executor=executor,
                  retry_failed=False, logger=lambda _m: None).run()
    # The first pass left one infra_error with attempt == 1.
    first = json.loads(
        (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert first["attempt"] == 1

    second = runner.Runner(
        DATASET, CONFIG, tmp_path, entries=selected, executor=_GoodExecutor(),
        retry_failed=True, logger=lambda _m: None,
    )
    second.run()
    records = [
        json.loads(line)
        for line in (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    assert records[1]["attempt"] == 2, "the resumed attempt continues the numbering"
    assert records[1]["attempt_id"] != records[0]["attempt_id"]


# --- pricing honesty ------------------------------------------------------


def test_an_unpriced_run_records_a_null_cost_and_says_so(tmp_path):
    batch = runner.Runner(
        DATASET, CONFIG, tmp_path,
        entries=CONFIG.entries()[:1],
        executor=_GoodExecutor({"est_cost_usd": None, "cost_basis": "unknown_model"}),
        model="groq/qwen/qwen3.8-27b",
        logger=lambda _m: None,
    )
    batch.run()
    record = json.loads(
        (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert record["est_cost_usd"] is None
    schema.RunRecord.model_validate(record)


def test_pricing_record_distinguishes_priced_from_unpriced():
    priced = runner.pricing_record(CONFIG, "gemini-2.5-flash", 0.004, "priced:gemini-2.5-flash")
    assert priced.priced is True
    assert priced.unpriced_models == []

    unpriced = runner.pricing_record(CONFIG, "groq/qwen/qwen3.8-27b", None, "unknown_model")
    assert unpriced.priced is False
    assert unpriced.unpriced_models == ["groq/qwen/qwen3.8-27b"]
    assert "null" in unpriced.basis


def test_the_active_model_gets_no_invented_rate():
    for name in ("groq/qwen/qwen3.8-27b", "qwen/qwen3.8-27b"):
        cost, basis = runner.estimate_cost(CONFIG, name, 1_000_000, 1_000_000)
        assert cost is None, f"{name} must stay unpriced"
        assert basis == "unknown_model"
        assert runner.pricing_summary(CONFIG, name)["priced"] is False


def test_usd_caps_are_reported_as_not_enforceable_when_unpriced():
    hier = schema.load_config(HIER_PATH)
    assert runner.usd_cap_is_enforceable(hier, "groq/qwen/qwen3.8-27b") is False
    status = runner.pricing_status_for_meta(hier, "groq/qwen/qwen3.8-27b")
    assert status["priced"] is False
    assert status["usd_cap_enforceable"] is False
    assert status["unpriced_models"] == ["groq/qwen/qwen3.8-27b"]
    assert "cannot bound real spend" in status["note"]
    assert "max_runs=40" in status["note"]


def test_run_meta_names_the_unpriced_model(tmp_path):
    hier = schema.load_config(HIER_PATH)
    provenance = schema.ModelProvenance(
        adk_model="groq/qwen/qwen3.8-27b", crew_model="openai/deepseek/deepseek-v4.1-flash"
    )
    batch = runner.Runner(
        DATASET, hier, tmp_path,
        entries=hier.entries()[:1],
        executor=_GoodExecutor(),
        provenance=provenance,
        acknowledge_unpriced=True,
        logger=lambda _m: None,
    )
    batch.run()
    meta = json.loads((tmp_path / "run_meta.json").read_text(encoding="utf-8"))
    assert "groq/qwen/qwen3.8-27b" in meta["pricing_status"]["unpriced_models"]
    assert meta["pricing_status"]["priced"] is False
    assert meta["pricing_status"]["usd_cap_enforceable"] is False
    assert meta["budget"]["usd_cap_enforceable"] is False
    assert "cannot bound real spend" in meta["budget"]["usd_cap_note"]
    assert meta["unpriced_acknowledged"]["acknowledged"] is True
    assert "groq/qwen/qwen3.8-27b" in meta["unpriced_acknowledged"]["unpriced_models"]


def test_a_live_batch_over_an_unpriced_model_needs_acknowledgement(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        runner, "preflight",
        lambda **_kwargs: {
            "crew_a2a_server": "up",
            "llm_api_key": "present",
            "model": "groq/qwen/qwen3.8-27b",
            "crew_model": "groq/qwen/qwen3.8-27b",
        },
    )
    monkeypatch.setenv("LLM_MODEL", "groq/qwen/qwen3.8-27b")
    code = runner.main(["--out", str(tmp_path / "out"), "--limit", "1"])
    captured = capsys.readouterr()
    assert code == runner.EXIT_PRECONDITION
    assert "--acknowledge-unpriced" in captured.err
    assert "cannot" in captured.err and "bound real spend" in captured.err
    assert not (tmp_path / "out" / "raw.jsonl").exists(), "nothing may be executed"


def test_the_acknowledgement_is_recorded_in_run_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "preflight",
        lambda **_kwargs: {
            "crew_a2a_server": "up",
            "llm_api_key": "present",
            "model": "groq/qwen/qwen3.8-27b",
            "crew_model": "groq/qwen/qwen3.8-27b",
        },
    )
    monkeypatch.setenv("LLM_MODEL", "groq/qwen/qwen3.8-27b")
    captured: dict = {}
    real_runner = runner.Runner

    def capturing_runner(*args, **kwargs):
        captured.update(kwargs)
        # Keep the acknowledgement the CLI passed; use a stub executor so the
        # batch itself is the only thing exercised.
        kwargs["executor"] = _GoodExecutor()
        kwargs["logger"] = lambda _message: None
        kwargs["resume"] = False
        return real_runner(*args, **kwargs)

    monkeypatch.setattr(runner, "Runner", capturing_runner)
    code = runner.main([
        "--out", str(tmp_path / "out"), "--limit", "1", "--acknowledge-unpriced",
    ])
    assert code == runner.EXIT_OK
    assert captured["acknowledge_unpriced"] is True
    meta = json.loads((tmp_path / "out" / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["unpriced_acknowledged"]["acknowledged"] is True
    assert "groq/qwen/qwen3.8-27b" in meta["unpriced_acknowledged"]["unpriced_models"]
    assert "chose to proceed" in meta["unpriced_acknowledged"]["note"]


def test_the_dry_run_reports_the_honesty_block(capsys):
    code = runner.main([
        "--dry-run", "--json", "--config", str(HIER_PATH),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == runner.EXIT_OK
    assert payload["plan"]["scope"] == "focused"
    assert payload["plan"]["target_runs"] == 36
    assert payload["plan"]["minimum_runs"] == schema.MIN_PLAN_RUNS_FOCUSED
    assert payload["honesty"]["usd_cap_enforceable"] is False
    assert "cannot bound real spend" in payload["honesty"]["usd_cap_note"]
    assert "No seed is forwarded" in payload["honesty"]["seed_note"]
    assert payload["run"]["seed_propagated_to_provider"] is False
    assert payload["run"]["backend"] == "subprocess"
    assert payload["plan"]["by_tier"] == {"core": 12, "breadth": 12, "variance": 12}
    assert payload["plan"]["distinct_prompts"] == 24


def test_the_dry_run_writes_nothing_for_the_focused_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DEFAULT_RESULTS_ROOT", tmp_path / "results")
    code = runner.main(["--dry-run", "--config", str(HIER_PATH)])
    assert code == runner.EXIT_OK
    assert not (tmp_path / "results").exists()


def test_the_focused_plan_validates_the_72_run_config_is_untouched():
    """Both plans load side by side; the matrix file is preserved."""
    hier = schema.load_config(HIER_PATH)
    config = schema.load_config(CONFIG_PATH)
    assert hier.plan.id != config.plan.id
    assert len(hier.plan.entries) == 36
    assert len(config.plan.entries) == 72
    assert config.matrix.backend == "in_process"  # unchanged on purpose
    assert config.budget.max_runs == 72
