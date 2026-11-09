"""Offline tests for evaluation results-directory single-flight locking."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation import runner, schema  # noqa: E402
from evaluation.output_lock import (  # noqa: E402
    OutputDirectoryBusyError,
    OutputDirectoryLock,
)


def _wait_for(path: Path, process: subprocess.Popen, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            pytest.fail(f"lock holder exited early with {process.returncode}")
        time.sleep(0.02)
    pytest.fail("lock holder did not acquire the results-directory lock")


def _read_owner(path: Path) -> dict:
    with path.open("rb") as handle:
        handle.seek(1)
        return json.loads(handle.read())


def test_lock_is_cross_process_and_reports_owner_then_recovers_after_crash(tmp_path):
    out_dir = tmp_path / "results"
    ready = tmp_path / "ready"
    ready.unlink(missing_ok=True)
    code = (
        "import pathlib,sys,time; "
        "import os; "
        "from evaluation.output_lock import OutputDirectoryLock; "
        "lock=OutputDirectoryLock(sys.argv[1]); lock.__enter__(); "
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid())); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(out_dir), str(ready)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(ready, process)
        owner_pid = int(ready.read_text())
        with pytest.raises(OutputDirectoryBusyError) as caught:
            with OutputDirectoryLock(out_dir):
                pytest.fail("a second process acquired an active lock")
        message = str(caught.value)
        assert f"pid={owner_pid}" in message
        assert "host=" in message and "started=" in message
        assert str(out_dir.resolve()) in message
    finally:
        process.kill()
        process.wait(timeout=10)

    lock_file = out_dir / ".evaluation.lock"
    assert lock_file.exists()  # Keep the inode stable; the OS lock itself is released.
    stale = _read_owner(lock_file)
    assert stale["pid"] == owner_pid
    with OutputDirectoryLock(out_dir):
        current = _read_owner(lock_file)
        assert current["pid"] == os.getpid()


def test_two_runner_processes_only_execute_one_fake_attempt(tmp_path):
    out_dir = tmp_path / "results"
    started = tmp_path / "executor-started"
    release = tmp_path / "release-executor"
    second_called = tmp_path / "second-executor-called"
    busy = tmp_path / "busy-message"
    code = """
import pathlib, sys, time
from evaluation import runner, schema
from evaluation.output_lock import OutputDirectoryBusyError
root = pathlib.Path.cwd()
dataset = schema.load_dataset(root / 'evaluation/dataset.json')
config = schema.load_config(root / 'evaluation/config.json')
entry = config.entries()[0]
item = dataset.by_id(entry.prompt_id)
mode, out, started, release, second_called, busy = sys.argv[1:]
def execute(e, i, **_kwargs):
    if mode == 'first':
        pathlib.Path(started).write_text(str(__import__('os').getpid()))
        deadline = time.monotonic() + 20
        while not pathlib.Path(release).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not pathlib.Path(release).exists():
            raise RuntimeError('test release barrier timed out')
    else:
        pathlib.Path(second_called).write_text('called')
    now = runner.utc_now()
    return schema.RunRecord.model_validate({
        'run_key': e.run_key, 'prompt_id': i.id, 'category': i.category,
        'difficulty': i.difficulty, 'persona': i.persona,
        'pattern': e.pattern, 'context_sharing': e.context_sharing,
        'started_at': now, 'ended_at': now, 'latency_s': 0.1,
        'terminal_state': 'completed', 'final_answer': 'offline fake answer',
    }).model_dump(mode='json')
batch = runner.Runner(dataset, config, pathlib.Path(out), entries=[entry],
    executor=execute, provenance=schema.ModelProvenance(), logger=lambda _m: None)
try:
    batch.run()
except OutputDirectoryBusyError as exc:
    pathlib.Path(busy).write_text(str(exc))
"""
    first = subprocess.Popen(
        [sys.executable, "-c", code, "first", str(out_dir), str(started),
         str(release), str(second_called), str(busy)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(started, first)
        second = subprocess.run(
            [sys.executable, "-c", code, "second", str(out_dir), str(started),
             str(release), str(second_called), str(busy)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert second.returncode == 0, second.stderr
        assert busy.exists()
        message = busy.read_text()
        assert "owner pid=" in message and "host=" in message
        assert not second_called.exists()
        release.write_text("continue")
        assert first.wait(timeout=15) == 0
    finally:
        release.write_text("continue")
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10)
    third = subprocess.run(
        [sys.executable, "-c", code, "third", str(out_dir), str(started),
         str(release), str(second_called), str(busy)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert third.returncode == 0, third.stderr
    assert not second_called.exists()  # Default resume skipped the recorded key.
    records = [
        json.loads(line)
        for line in (out_dir / "raw.jsonl").read_text().splitlines()
        if line
    ]
    assert len(records) == 1
    assert records[0]["terminal_state"] == "completed"
    assert records[0]["final_answer"] == "offline fake answer"


def test_lock_metadata_survives_normal_and_exceptional_release(tmp_path):
    out_dir = tmp_path / "results"
    with OutputDirectoryLock(out_dir):
        assert (out_dir / ".evaluation.lock").exists()
    with pytest.raises(RuntimeError):
        with OutputDirectoryLock(out_dir):
            raise RuntimeError("offline check")
    with OutputDirectoryLock(out_dir):
        assert _read_owner(out_dir / ".evaluation.lock")["pid"] == os.getpid()


def test_metadata_write_failure_releases_and_closes_lock(tmp_path, monkeypatch):
    import evaluation.output_lock as output_lock

    def fail_fsync(_fd):
        raise OSError("offline fsync failure")

    monkeypatch.setattr(output_lock.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="offline fsync failure"):
        with OutputDirectoryLock(tmp_path / "results"):
            pytest.fail("context entered despite metadata write failure")
    monkeypatch.undo()
    with OutputDirectoryLock(tmp_path / "results"):
        pass


def test_stale_metadata_does_not_block_and_aliases_share_lock(tmp_path):
    out_dir = tmp_path / "results"
    out_dir.mkdir()
    lock_file = out_dir / ".evaluation.lock"
    lock_file.write_bytes(
        b"\0"
        + json.dumps(
            {"pid": -1, "hostname": "old", "started_at": "old", "out_dir": "old"}
        ).encode()
    )
    alias = out_dir / ".." / "results"
    with OutputDirectoryLock(out_dir):
        with pytest.raises(OutputDirectoryBusyError):
            with OutputDirectoryLock(alias):
                pytest.fail("a path alias bypassed the directory lock")


def test_locks_for_different_directories_are_independent(tmp_path):
    with OutputDirectoryLock(tmp_path / "one"):
        with OutputDirectoryLock(tmp_path / "two"):
            pass


def test_runner_refuses_before_resume_selection_when_directory_is_busy(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    dataset = schema.load_dataset(root / "evaluation" / "dataset.json")
    config = schema.load_config(root / "evaluation" / "config.json")
    batch = runner.Runner(
        dataset,
        config,
        tmp_path / "results",
        entries=config.entries()[:1],
        executor=lambda *_args: pytest.fail("busy runner must not execute work"),
    )
    monkeypatch.setattr(
        batch, "pending", lambda *_args: pytest.fail("resume selection ran before locking")
    )
    with OutputDirectoryLock(batch.out_dir):
        with pytest.raises(OutputDirectoryBusyError):
            batch.run()
    assert not batch.raw_path.exists()
