"""
Decide whether to trigger compile.py in the background and do so if needed.

Called from:
  * flush.py at the end of a SessionEnd/PreCompact flush
  * hooks/session-start.py at the start of every session

Why both: the user wants compilation to run at 04:00 local. If the computer
is OFF at 04:00, we still want it to run on the next session — so SessionStart
provides the "wake on resume" fallback.

This script:
  1. Determines the project from $CMC_PROJECT_DIR / $CLAUDE_PROJECT_DIR / cwd.
  2. Checks COMPILE_AFTER_HOUR (default 4) — must be >= that hour locally.
  3. Walks every daily/*.md, compares hashes against .cmc/state.json.
  4. If anything is missing or stale, spawns compile.py as a detached process.

It is safe to call repeatedly — compile.py itself is idempotent on hashes.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess as sp
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import time

TOOL_ROOT = Path(__file__).resolve().parent.parent
if (TOOL_ROOT / ".disabled-by-codex").exists() or os.environ.get("CMC_DISABLED") == "1":
    print("disabled by Codex kill-switch")
    sys.exit(0)

from config import (
    COMPILE_AFTER_HOUR,
    COMPILE_LOG_FILE,
    DAILY_DIR,
    FLUSH_LOG_FILE,
    PROJECT_DIR,
    STATE_FILE,
    TOOL_ROOT,
    ensure_state_dir,
    memory_enabled,
)
from utils import list_raw_files, list_source_files

LOCK_STALE_AFTER_SECONDS = 6 * 60 * 60
RETRY_FAILED_AFTER_SECONDS = 6 * 60 * 60
MIN_SPAWN_INTERVAL_SECONDS = 30 * 60


def _file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()[:16]


def _has_pending_work() -> bool:
    """True if any daily log is uncompiled or has changed since last compile."""
    if not DAILY_DIR.exists():
        return False

    ingested: dict = {}
    if STATE_FILE.exists():
        try:
            ingested = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("ingested", {})
        except (json.JSONDecodeError, OSError):
            ingested = {}

    for log_path in list_raw_files() + list_source_files():
        name = log_path.name if log_path.parent == DAILY_DIR else f"sources/{log_path.name}"
        if name not in ingested:
            return True
        entry = ingested[name]
        if entry.get("hash") != _file_hash(log_path):
            return True
        # A failed compile is retried for the same file version at most
        # once every RETRY_FAILED_AFTER_SECONDS — never in a tight loop.
        failed_at = entry.get("failed_at")
        if failed_at:
            try:
                failed_ts = datetime.fromisoformat(failed_at).timestamp()
            except ValueError:
                continue
            if time() - failed_ts > RETRY_FAILED_AFTER_SECONDS:
                return True
    return False


def _setup_logging() -> None:
    ensure_state_dir()
    if FLUSH_LOG_FILE.is_symlink():
        logging.basicConfig(level=logging.WARNING)
        return
    logging.basicConfig(
        filename=str(FLUSH_LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [maybe-compile] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def trigger_if_due() -> str:
    """Return a short status string explaining the decision."""
    if not memory_enabled():
        return "skip: no wiki/"

    now = datetime.now(timezone.utc).astimezone()
    if now.hour < COMPILE_AFTER_HOUR:
        return f"skip: hour {now.hour:02d} < {COMPILE_AFTER_HOUR:02d}"

    if not _has_pending_work():
        return "skip: no pending daily logs"

    compile_script = TOOL_ROOT / "scripts" / "compile.py"
    if not compile_script.exists():
        return "skip: compile.py not found"

    # Rate limit: never spawn compile more often than MIN_SPAWN_INTERVAL,
    # even if daily logs keep changing (every spawn costs Claude usage).
    spawn_marker = PROJECT_DIR / ".cmc" / "last-compile-spawn"
    if spawn_marker.exists():
        if time() - spawn_marker.stat().st_mtime < MIN_SPAWN_INTERVAL_SECONDS:
            return "skip: compile spawned recently"

    lock_path = PROJECT_DIR / ".cmc" / "compile.lock"
    try:
        if lock_path.exists():
            age = time() - lock_path.stat().st_mtime
            if age < LOCK_STALE_AFTER_SECONDS:
                return "skip: compile already running"
            lock_path.unlink(missing_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as lock:
            lock.write(f"pid={os.getpid()}\nstarted={now.isoformat()}\n")
    except OSError as e:
        return f"skip: compile lock unavailable ({e})"

    cmd = [
        "uv", "run",
        "--directory", str(TOOL_ROOT),
        "python", str(compile_script),
    ]

    env = os.environ.copy()
    env["CMC_PROJECT_DIR"] = str(PROJECT_DIR)
    env["CMC_COMPILE_LOCK"] = str(lock_path)

    kwargs: dict = {"env": env, "cwd": str(TOOL_ROOT)}
    if sys.platform == "win32":
        kwargs["creationflags"] = sp.CREATE_NEW_PROCESS_GROUP | sp.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    try:
        if COMPILE_LOG_FILE.is_symlink():
            raise ValueError("compile log is a symlink")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        log_handle = os.fdopen(os.open(COMPILE_LOG_FILE, flags, 0o600), "a")
        sp.Popen(cmd, stdout=log_handle, stderr=sp.STDOUT, **kwargs)
        spawn_marker.touch()
    except Exception as e:
        lock_path.unlink(missing_ok=True)
        return f"error: {e}"

    return f"spawned: compile.py for {PROJECT_DIR}"


def main() -> int:
    _setup_logging()
    status = trigger_if_due()
    logging.info("maybe_compile: %s", status)
    print(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
