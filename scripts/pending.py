"""Per-turn raw capture buffers + batched hand-off to flush.py (the Grok step).

Design: the Stop hook captures raw turn-deltas into .cmc/pending/<session>.md with
NO LLM call (loop-free hot path). Grok summarization runs batched —
at SessionEnd / PreCompact (flush this session's buffer) and at SessionStart
(recover buffers orphaned by an abrupt close). Every flush.py spawn carries
CLAUDE_INVOKED_BY so Claude hooks no-op for detached memory jobs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import time

TOOL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = TOOL_ROOT / "scripts"
FLUSH_SCRIPT = SCRIPTS_DIR / "flush.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from transcript import load_offset, read_new_turns, save_offset  # noqa: E402


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S-%f")


def _pending_dir(project_dir: Path) -> Path:
    return project_dir / ".cmc" / "pending"


def capture_to_buffer(project_dir: Path, session_id: str, transcript_path: str | Path) -> int:
    """Append the new turns (since the shared cursor) to this session's raw buffer.

    No LLM. Returns the number of new turns captured (0 if nothing new).
    """
    state_dir = project_dir / ".cmc"
    state_dir.mkdir(parents=True, exist_ok=True)
    capture_state = state_dir / "claude-capture-state.json"
    tp = str(transcript_path)

    offset = load_offset(capture_state, tp)
    turns, new_offset, n = read_new_turns(Path(transcript_path), offset)
    if n == 0:
        return 0

    pending = _pending_dir(project_dir)
    pending.mkdir(parents=True, exist_ok=True)
    buf = pending / f"{session_id}.md"

    lock_path = state_dir / "pending.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with buf.open("a", encoding="utf-8") as fh:
                fh.write(turns + "\n")
        finally:
            if sys.platform != "win32":
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    save_offset(capture_state, tp, new_offset, session_id)
    return n


def _spawn_flush(context_file: Path, flush_id: str, project_dir: Path) -> None:
    cmd = [
        "uv", "run", "--directory", str(TOOL_ROOT),
        "python", str(FLUSH_SCRIPT),
        str(context_file), flush_id, str(project_dir),
    ]
    env = os.environ.copy()
    # Marker inherited by detached memory jobs.
    env["CLAUDE_INVOKED_BY"] = "memory_flush"
    env["CMC_PROJECT_DIR"] = str(project_dir)

    kwargs: dict = {"env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def flush_session(project_dir: Path, session_id: str) -> bool:
    """Hand THIS session's pending buffer to flush.py (batched Grok summary).

    Renames the buffer to a .flushing.md sentinel first, so a crash mid-flush
    leaves the data for SessionStart recovery; flush.py unlinks it on success.
    """
    buf = _pending_dir(project_dir) / f"{session_id}.md"
    try:
        if not buf.exists() or buf.stat().st_size == 0:
            return False
    except OSError:
        return False
    flushing = buf.with_name(f"{session_id}.{_ts()}.flushing.md")
    try:
        buf.rename(flushing)
    except OSError:
        return False
    _spawn_flush(flushing, f"claude-batch-{session_id}-{_ts()}", project_dir)
    return True


def recover_orphans(project_dir: Path, min_age_seconds: int = 600) -> int:
    """Flush buffers left by sessions that ended without SessionEnd.

    Skips fresh ``<sid>.md`` buffers (likely a live session); always re-flushes
    leftover ``*.flushing.md`` (a previous flush died). Fire-and-forget.
    """
    pending = _pending_dir(project_dir)
    if not pending.exists():
        return 0
    spawned = 0
    for buf in list(pending.glob("*.md")):
        is_flushing = buf.name.endswith(".flushing.md")
        try:
            if buf.stat().st_size == 0:
                continue
            age = time() - buf.stat().st_mtime
        except OSError:
            continue
        if not is_flushing and age < min_age_seconds:
            continue  # likely an active session's buffer — leave it
        sid = buf.name.split(".")[0]
        target = buf if is_flushing else buf.with_name(f"{sid}.{_ts()}.flushing.md")
        try:
            if target != buf:
                buf.rename(target)
        except OSError:
            continue
        _spawn_flush(target, f"claude-recover-{sid}-{_ts()}", project_dir)
        spawned += 1
    return spawned
