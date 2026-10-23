"""Stop the background CrewAI A2A server started by scripts/dev_up.py.

    python scripts/dev_down.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PID_FILE = REPO_ROOT / ".run" / "crew_a2a.pid"


def main() -> int:
    if not PID_FILE.exists():
        print("nothing to stop (.run/crew_a2a.pid is missing)")
        return 0

    pid = PID_FILE.read_text(encoding="utf-8").strip()
    if not pid.isdigit():
        PID_FILE.unlink(missing_ok=True)
        print("pid file was unreadable; removed it")
        return 1

    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/PID", pid, "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        stopped = result.returncode == 0
        detail = (result.stdout or result.stderr).strip().splitlines()
    else:  # pragma: no cover - this project targets Windows
        result = subprocess.run(["kill", "-9", pid], check=False)
        stopped = result.returncode == 0
        detail = []

    PID_FILE.unlink(missing_ok=True)
    print(f"stopped pid {pid}" if stopped else f"could not stop pid {pid}")
    for line in detail[:2]:
        print(f"  {line}")
    return 0 if stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
