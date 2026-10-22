"""Concurrency tests: many threads and two separate processes, one DB file.

These are the tests that prove ``append`` is safe for parallel researchers.
No sleeps and no retries in the assertions: the store is expected to serialize
writers on its own, and a failure here is a real bug.
"""

from __future__ import annotations

import multiprocessing
import threading
from uuid import uuid4

import pytest

from athena.blackboard import Blackboard, EvidenceItem, SQLiteStore, Task


@pytest.fixture()
def store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "blackboard.db")


@pytest.fixture()
def bb(store: SQLiteStore) -> Blackboard:
    return Blackboard.create(Task(question="Concurrent?"), store=store)


def _evidence(author: str) -> EvidenceItem:
    return EvidenceItem(
        id=str(uuid4()),
        source_type="web",
        source_ref="https://example.com/42",
        content="42 is the answer to everything.",
        retrieved_by=author,
    )


def _append_evidence(store: SQLiteStore, run_id: str, count: int, author: str) -> None:
    blackboard = Blackboard.open(run_id, store)
    for _ in range(count):
        blackboard.append("evidence", _evidence(author), author=author)


def _append_in_new_process(db_path: str, run_id: str, count: int) -> None:
    _append_evidence(SQLiteStore(db_path), run_id, count, author="process-worker")


def test_threads_append_without_loss(store: SQLiteStore, bb: Blackboard) -> None:
    thread_count, per_thread = 8, 25
    threads = [
        threading.Thread(
            target=_append_evidence,
            args=(store, bb.run_id, per_thread, f"researcher-{index}"),
        )
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    items, version = bb.read("evidence")
    assert len(items) == thread_count * per_thread
    assert len({item.id for item in items}) == thread_count * per_thread
    assert version == thread_count * per_thread


def test_processes_append_without_loss(tmp_path) -> None:
    db_path = str(tmp_path / "shared.db")
    blackboard = Blackboard.create(Task(question="Concurrent?"), store=SQLiteStore(db_path))

    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_append_in_new_process, args=(db_path, blackboard.run_id, 10))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=180)

    assert [process.exitcode for process in processes] == [0, 0]
    items, version = blackboard.read("evidence")
    assert len(items) == 20
    assert len({item.id for item in items}) == 20
    assert version == 20
