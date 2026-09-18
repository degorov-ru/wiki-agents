"""Per-turn raw capture buffers + batched hand-off to flush.py (the Grok step).

Design: the Stop hook captures raw turn-deltas into .cmc/pending/<session>.md with
NO LLM call (loop-free hot path). Grok summarization runs batched —
at SessionEnd / PreCompact (flush this session's buffer) and at SessionStart
(recover buffers orphaned by an abrupt close). Every flush.py spawn carries
CLAUDE_INVOKED_BY so Claude hooks no-op for detached memory jobs.
"""

from __future__ import annotations

import os
import json
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import time

TOOL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = TOOL_ROOT / "scripts"
FLUSH_SCRIPT = SCRIPTS_DIR / "flush.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from transcript import load_offset, read_new_turns, save_offset  # noqa: E402
from path_safety import atomic_write_text, contained_file, managed_dir, validate_layout  # noqa: E402


@contextmanager
def project_lock(project_dir: Path, name: str = "pending"):
    project_dir = validate_layout(project_dir, create_state=True)
    state = managed_dir(project_dir, ".cmc")
    lock_path = state / f"{name}.lock"
    if lock_path.is_symlink():
        raise ValueError("unsafe project lock")
    with lock_path.open("a+") as lock:
        if sys.platform == "win32":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def valid_session_id(session_id: str) -> bool:
    return bool(isinstance(session_id, str) and session_id != "unknown"
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", session_id))


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S-%f")


def _pending_dir(project_dir: Path) -> Path:
    state = managed_dir(validate_layout(project_dir, create_state=True), ".cmc")
    pending = state / "pending"
    if pending.is_symlink() or (pending.exists() and not pending.is_dir()):
        raise ValueError("unsafe pending directory")
    if pending.exists() and not pending.resolve().is_relative_to(state.resolve()):
        raise ValueError("pending directory escapes project state")
    return pending


def _capture_intent(project_dir: Path) -> Path:
    return project_dir / ".cmc" / "capture-intent.json"


def _recover_capture(project_dir: Path) -> None:
    intent = _capture_intent(project_dir)
    if not intent.exists():
        return
    state_dir = project_dir / ".cmc"
    if state_dir.is_symlink() or intent.is_symlink():
        raise RuntimeError("unsafe capture intent")
    data = json.loads(intent.read_text(encoding="utf-8"))
    session_id = data.get("session_id")
    payload_text = data.get("payload")
    old_size = data.get("old_size")
    new_offset = data.get("new_offset")
    transcript = data.get("transcript")
    expected = f".cmc/pending/{session_id}.md"
    if (not valid_session_id(session_id) or data.get("buffer") != expected
            or not isinstance(payload_text, str) or not isinstance(old_size, int)
            or old_size < 0 or not isinstance(new_offset, int) or new_offset < 0
            or not isinstance(transcript, str)):
        raise RuntimeError("invalid capture intent")
    pending = state_dir / "pending"
    buf = pending / f"{session_id}.md"
    if pending.is_symlink() or buf.is_symlink():
        raise RuntimeError("unsafe capture buffer")
    payload = payload_text.encode()
    buf.parent.mkdir(parents=True, exist_ok=True)
    current = buf.stat().st_size if buf.exists() else 0
    if current == old_size:
        with buf.open("ab") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    elif current < old_size or buf.read_bytes()[old_size:] != payload:
        raise RuntimeError("capture buffer changed during recovery")
    save_offset(state_dir / "claude-capture-state.json", transcript, new_offset, session_id)
    intent.unlink()


def capture_to_buffer(project_dir: Path, session_id: str, transcript_path: str | Path) -> int:
    """Append the new turns (since the shared cursor) to this session's raw buffer.

    No LLM. Returns the number of new turns captured (0 if nothing new).
    """
    if not valid_session_id(session_id):
        return 0
    project_dir = validate_layout(project_dir, create_state=True)
    state_dir = managed_dir(project_dir, ".cmc")
    transcript = Path(transcript_path)
    if transcript.is_symlink() or not transcript.is_file():
        raise ValueError("transcript must be a regular non-symlink file")
    capture_state = state_dir / "claude-capture-state.json"
    tp = str(transcript_path)

    with project_lock(project_dir):
        if (state_dir / "purge-paused").exists():
            return 0
        _recover_capture(project_dir)
        offset = load_offset(capture_state, tp)
        turns, new_offset, n = read_new_turns(Path(transcript_path), offset)
        if n:
            pending = _pending_dir(project_dir)
            pending.mkdir(parents=True, exist_ok=True)
            buf = pending / f"{session_id}.md"
            payload = turns + "\n"
            intent = _capture_intent(project_dir)
            atomic_write_text(intent, json.dumps({
                "buffer": str(buf.relative_to(project_dir)), "payload": payload,
                "old_size": buf.stat().st_size if buf.exists() else 0,
                "transcript": tp, "new_offset": new_offset, "session_id": session_id,
            }))
            _recover_capture(project_dir)
        else:
            save_offset(capture_state, tp, new_offset, session_id)
    return n


def _spawn_flush(context_file: Path, flush_id: str, project_dir: Path):
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

    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def claim_flush(context_file: Path, flush_id: str, project_dir: Path) -> bool:
    """Called under pending.lock; retain failures, stop after three attempts."""
    project_dir = validate_layout(project_dir, create_state=True)
    state_dir = managed_dir(project_dir, ".cmc")
    try:
        context_file = contained_file(context_file, state_dir).resolve()
    except ValueError:
        return False
    allowed = context_file.parent == state_dir / "pending" or (
        context_file.parent == state_dir and context_file.name.startswith("codex-flush-")
    )
    if not allowed or (state_dir / "purge-paused").exists():
        return False
    receipt = context_file.with_suffix(".retry.json")
    if receipt.is_symlink():
        return False
    try:
        state = json.loads(receipt.read_text())
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict):
        return False
    attempts = state.get("attempts", 0)
    retry_after = state.get("retry_after", 0)
    if not isinstance(attempts, int) or not 0 <= attempts < 3 or not isinstance(retry_after, (int, float)):
        return False
    if time() < retry_after:
        return False
    pid = state.get("pid")
    if isinstance(pid, int) and pid > 0 and sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
    stable_flush_id = state.get("flush_id") or flush_id
    if not valid_session_id(stable_flush_id):
        return False
    state = {"attempts": attempts + 1, "retry_after": time() + 600 * 2 ** attempts,
             "flush_id": stable_flush_id}
    atomic_write_text(receipt, json.dumps(state))
    try:
        proc = _spawn_flush(context_file, stable_flush_id, project_dir)
    except OSError:
        return False
    if isinstance(getattr(proc, "pid", None), int):
        state["pid"] = proc.pid
        atomic_write_text(receipt, json.dumps(state))
    return True


def flush_session(project_dir: Path, session_id: str) -> bool:
    """Hand THIS session's pending buffer to flush.py (batched Grok summary).

    Renames the buffer to a .flushing.md sentinel first, so a crash mid-flush
    leaves the data for SessionStart recovery; flush.py unlinks it on success.
    """
    if not valid_session_id(session_id):
        return False
    with project_lock(project_dir):
        _recover_capture(project_dir)
        buf = _pending_dir(project_dir) / f"{session_id}.md"
        if not buf.exists() or buf.stat().st_size == 0:
            return False
        flushing = buf.with_name(f"{session_id}.{_ts()}.flushing.md")
        buf.rename(flushing)
        return claim_flush(flushing, session_id, project_dir)


def recover_orphans(project_dir: Path, min_age_seconds: int = 600) -> int:
    """Flush buffers left by sessions that ended without SessionEnd.

    Skips fresh buffers, claimed workers, and exhausted retries. Failed buffers
    remain available for inspection after three attempts.
    """
    with project_lock(project_dir):
        _recover_capture(project_dir)
        return _recover_orphans(project_dir, min_age_seconds)


def _recover_orphans(project_dir: Path, min_age_seconds: int) -> int:
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
        spawned += claim_flush(target, sid, project_dir)
    return spawned
