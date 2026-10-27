"""One run at a time: spawn the CLI, keep its log, remember the state.

The API never runs the pipeline in-process; it starts the existing CLI as a
subprocess so the demo shows exactly what a terminal run does, and so a crash
cannot take the web server with it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from athena.observability.redact import redact

RUN_DIR = Path(".run/runs")
MAX_QUESTION = 500


class BusyError(RuntimeError):
    """A run is already in progress."""


@dataclass
class ActiveRun:
    run_id: str
    pattern: str
    question: str
    process: subprocess.Popen
    log_path: Path
    summary_path: Path
    finished: bool = False
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)


class RunRegistry:
    """Holds the single active run and tails its log."""

    def __init__(self, command: list[str] | None = None) -> None:
        self._active: ActiveRun | None = None
        self._lock = threading.Lock()
        self._command = command  # tests replace this with a stub

    @property
    def active(self) -> ActiveRun | None:
        return self._active

    def _argv(self, question: str, pattern: str, run_id: str) -> list[str]:
        summary = RUN_DIR / f"{run_id}.summary.json"
        if self._command is not None:
            return [*self._command, run_id, question, pattern, str(summary)]
        return [
            sys.executable, "-m", "athena.run", question,
            "--pattern", pattern, "--run-id", run_id, "--json-summary", str(summary),
        ]

    def start(self, question: str, pattern: str) -> str:
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"question must be at most {MAX_QUESTION} characters")

        with self._lock:
            if self._active is not None and not self._active.finished:
                raise BusyError(f"run {self._active.run_id} is still going")

            run_id = str(uuid.uuid4())
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            log_path = RUN_DIR / f"{run_id}.log"
            summary_path = RUN_DIR / f"{run_id}.summary.json"
            log = log_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                self._argv(question, pattern, run_id),
                cwd=os.getcwd(),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=dict(os.environ),
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            self._active = ActiveRun(run_id, pattern, question, process, log_path,
                                     summary_path)
            return run_id

    def refresh(self) -> ActiveRun | None:
        """Update the cached exit code and read any new log lines."""
        run = self._active
        if run is None:
            return None
        run.exit_code = run.process.poll()
        if run.exit_code is not None:
            run.finished = True
        if run.log_path.exists():
            run.lines = [
                redact(line.rstrip("\n"))
                for line in run.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            ]
        return run

    def summary(self, run_id: str) -> dict | None:
        import json

        path = RUN_DIR / f"{run_id}.summary.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
