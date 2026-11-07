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
from athena.tools import registry

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
    tools: list[str] | None = None
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

    def _argv(
        self,
        question: str,
        pattern: str,
        run_id: str,
        tools: list[str] | None = None,
    ) -> list[str]:
        summary = RUN_DIR / f"{run_id}.summary.json"
        if self._command is not None:
            argv = [*self._command, run_id, question, pattern, str(summary)]
        else:
            argv = [
                sys.executable, "-m", "athena.run", question,
                "--pattern", pattern, "--run-id", run_id, "--json-summary", str(summary),
            ]
        if tools is not None:
            # An empty value is a real selection ("no tools"), not All tools.
            argv += ["--tools", ",".join(tools)]
        return argv

    def start(
        self,
        question: str,
        pattern: str,
        tools: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"question must be at most {MAX_QUESTION} characters")
        selection = registry.normalise_selection(tools)

        with self._lock:
            if self._active is not None and not self._active.finished:
                raise BusyError(f"run {self._active.run_id} is still going")

            run_id = str(uuid.uuid4())
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            log_path = RUN_DIR / f"{run_id}.log"
            summary_path = RUN_DIR / f"{run_id}.summary.json"
            log = log_path.open("w", encoding="utf-8")
            selected = list(selection) if selection is not None else None
            process = subprocess.Popen(
                self._argv(question, pattern, run_id, selected),
                cwd=os.getcwd(),
                stdout=log,
                stderr=subprocess.STDOUT,
                # The active profile wins over stale shell/.env values.
                env={**os.environ, **self._env_overrides()},
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            self._active = ActiveRun(
                run_id, pattern, question, process, log_path, summary_path,
                tools=selected,
            )
            return run_id

    @staticmethod
    def _env_overrides() -> dict[str, str]:
        try:
            from athena.api.activation import active_env

            return active_env()
        except Exception:  # noqa: BLE001 - never block a run on settings
            return {}

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

    def terminate_all(self, timeout: float = 5.0) -> bool:
        """Stop the active run subprocess, gracefully first and then hard.

        The API's shutdown hook calls this so a container stop (SIGTERM) does
        not strand a run that is mid-token-spend. ``terminate`` is SIGTERM on
        POSIX; on Windows it is TerminateProcess for the direct child only (the
        run's own children are left to the OS). Returns True when nothing of the
        run is left running.
        """
        run = self._active
        if run is None:
            return True
        process = run.process
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=max(0.0, timeout))
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    return False
        run.exit_code = process.poll()
        run.finished = run.exit_code is not None
        return run.finished

    def summary(self, run_id: str) -> dict | None:
        import json

        path = RUN_DIR / f"{run_id}.summary.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
