"""Persistent and visible alerts for memory engine failures."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def notify(project: Path, engine: str, error: str) -> None:
    state_dir = project / ".cmc"
    state_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "engine": engine,
        "project": str(project),
        "error": error[-500:],
    }
    with (state_dir / "alerts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    message = f"{engine} недоступен для {project.name}. Детали: .cmc/alerts.jsonl"
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(message)} with title "Project memory"'],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass
