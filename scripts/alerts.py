"""Persistent and visible alerts for memory engine failures."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from path_safety import append_text, managed_dir, validate_layout


def notify(project: Path, engine: str, error: str) -> None:
    project = validate_layout(project, create_state=True)
    state_dir = managed_dir(project, ".cmc")
    record = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "engine": engine,
        "project": str(project),
        "error": error[-500:],
    }
    append_text(state_dir / "alerts.jsonl", json.dumps(record, ensure_ascii=False) + "\n")

    message = f"{engine} недоступен для {project.name}. Детали: .cmc/alerts.jsonl"
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(message)} with title "Project memory"'],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass
