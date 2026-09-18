"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses Grok
to decide what's worth saving, and appends the result to that project's
daily log.

Usage:
    uv run python flush.py <context_file.md> <session_id> <project_dir>
"""

from __future__ import annotations

# Pin the project dir before importing config (it reads env at import).
import os
import sys

os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

if len(sys.argv) >= 4:
    os.environ["CMC_PROJECT_DIR"] = sys.argv[3]

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
if (TOOL_ROOT / ".disabled-by-codex").exists() or os.environ.get("CMC_DISABLED") == "1":
    sys.exit(0)

from config import (
    DAILY_DIR,
    FLUSH_LOG_FILE,
    FLUSH_STATE_FILE,
    PROJECT_DIR,
    STATE_DIR,
    ensure_state_dir,
    memory_enabled,
)
from memory_contract import redact_sensitive
from pending import project_lock
from path_safety import append_text, atomic_write_text

ensure_state_dir()

logging.basicConfig(
    filename=None if FLUSH_LOG_FILE.is_symlink() else str(FLUSH_LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def load_flush_state() -> dict:
    if FLUSH_STATE_FILE.exists():
        try:
            return json.loads(FLUSH_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_flush_state(state: dict) -> None:
    atomic_write_text(FLUSH_STATE_FILE, json.dumps(state))


def append_to_daily_log(content: str, section: str = "Session", session_id: str = "unknown",
                        event_key: str | None = None) -> bool:
    """Append content to today's daily log inside the current project."""
    today = datetime.now(timezone.utc).astimezone()
    daily_date = os.environ.get("CMC_DAILY_DATE", today.strftime("%Y-%m-%d"))
    log_path = DAILY_DIR / f"{daily_date}.md"
    lock_path = STATE_DIR / "daily.lock"

    if log_path.is_symlink() or lock_path.is_symlink():
        raise ValueError("unsafe daily log path")

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {daily_date}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )

    time_str = today.strftime("%H:%M")
    identity = f"{daily_date}\0{event_key or content}"
    event_id = f"m-{daily_date.replace('-', '')}-{hashlib.sha256(identity.encode()).hexdigest()[:10]}"
    entry = (
        f"### {event_id} | {section} ({time_str})\n\n"
        f"**Session:** `{session_id}`  \n"
        f"**Evidence:** model summary; original transcript may be unavailable\n\n"
        f"{content}\n\n"
    )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        else:
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            if event_id in log_path.read_text(encoding="utf-8"):
                return False
            append_text(log_path, entry)
        finally:
            if sys.platform != "win32":
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            else:
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    return True


def run_flush(context: str) -> tuple[str, dict]:
    """Use Grok to extract important knowledge from conversation context."""
    context, redacted = redact_sensitive(context)
    prompt = f"""Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.
Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges

Preserve exact names, ports, IDs, and other values when they are part of a
durable fact or decision; do not replace them with vague references.

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK

## Conversation Context

{context}"""

    from model_client import run_text_prompt

    result = run_text_prompt(prompt, PROJECT_DIR, max_turns=2)
    if not result.get("ok"):
        return "", result
    if redacted:
        result["redacted"] = True
    return str(result.get("text") or ""), result


def maybe_trigger_compilation() -> None:
    """Delegate to maybe_compile.trigger_if_due (shared by SessionStart too)."""
    try:
        from maybe_compile import trigger_if_due
        status = trigger_if_due()
        logging.info("maybe_compile: %s", status)
    except Exception as e:
        logging.error("maybe_compile failed: %s", e)


def main():
    # ponytail: serialize per-project model flushes; per-item locks if throughput matters.
    with project_lock(PROJECT_DIR, "flush"):
        _main_locked()


def _remove_context(context_file: Path) -> None:
    with project_lock(PROJECT_DIR):
        context_file.unlink(missing_ok=True)
        context_file.with_suffix(".retry.json").unlink(missing_ok=True)


def _main_locked():
    if (STATE_DIR / "purge-paused").exists():
        return
    if len(sys.argv) < 3:
        logging.error("Usage: %s <context_file.md> <session_id> [project_dir]", sys.argv[0])
        sys.exit(1)

    context_file = Path(sys.argv[1])
    session_id = sys.argv[2]

    try:
        safe_context = context_file.resolve().is_relative_to(STATE_DIR.resolve())
    except OSError:
        safe_context = False
    if not safe_context or not context_file.is_file():
        logging.error("Context must be a regular file inside the project .cmc directory")
        return

    logging.info(
        "flush.py started for session %s in project %s, context: %s",
        session_id, PROJECT_DIR, context_file,
    )

    if not memory_enabled():
        logging.info("SKIP: memory not enabled for project %s (no wiki/ dir)", PROJECT_DIR)
        _remove_context(context_file)
        return

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        return

    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        _remove_context(context_file)
        return

    # Dedupe by CONTENT hash, not session_id+time window: per-turn capture flushes
    # the same session repeatedly with DIFFERENT deltas, so a time window would
    # drop real content. A content hash skips only exact repeats (e.g. a SessionEnd
    # flush racing a SessionStart recovery of the same buffer).
    content_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()[:16]
    state = load_flush_state()
    recent_hashes = state.get("recent_hashes", [])
    if not isinstance(recent_hashes, list):
        recent_hashes = []
    if content_hash in recent_hashes:
        logging.info("Skipping duplicate flush (content hash %s)", content_hash)
        _remove_context(context_file)
        return

    logging.info(
        "Flushing session %s: %d chars (hash %s)", session_id, len(context), content_hash
    )

    response, result = run_flush(context)

    # FLUSH_OK / FLUSH_ERROR go to the log file only: writing them into the
    # daily log changes its hash and re-triggers compilation after every
    # session, even when nothing of value was saved.
    if not result.get("ok"):
        logging.error("Result: FLUSH_ERROR: %s", result.get("error"))
        logging.info("Keeping context for retry: %s", context_file)
        maybe_trigger_compilation()
        return
    if response.strip() == "FLUSH_OK":
        logging.info("Result: FLUSH_OK engine=%s", result.get("engine", "unknown"))
    else:
        logging.info("Result: saved to daily log (%d chars) engine=%s", len(response), result.get("engine", "unknown"))
        append_to_daily_log(response, "Session", session_id, content_hash)

    recent_hashes.append(content_hash)
    save_flush_state(
        {
            "recent_hashes": recent_hashes[-50:],
            "last_session_id": session_id,
            "timestamp": time.time(),
        }
    )
    _remove_context(context_file)

    maybe_trigger_compilation()
    logging.info("Flush complete for session %s", session_id)


if __name__ == "__main__":
    main()
