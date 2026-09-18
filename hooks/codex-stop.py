"""
Codex Stop hook - capture the explicitly identified session for this project.

Codex writes sessions as JSONL under ~/.codex/sessions. Unlike Claude Code,
Codex Stop hooks do not hand us a Claude transcript path, so this adapter finds
the exact Codex session whose session_meta.cwd matches the current project,
extracts the recent user/assistant messages, and reuses scripts/flush.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = TOOL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from pending import project_lock, claim_flush, valid_session_id
from path_safety import atomic_write_text, contained_file, managed_dir, validate_layout

if (TOOL_ROOT / ".disabled-by-codex").exists() or os.environ.get("CMC_DISABLED") == "1":
    sys.exit(0)


def _discover_project_dir() -> Path:
    for env_var in ("CODEX_PROJECT_DIR", "CMC_PROJECT_DIR", "PWD"):
        val = os.environ.get(env_var)
        if val:
            return Path(val).expanduser().resolve()
    return Path.cwd().resolve()


PROJECT_DIR = _discover_project_dir()
WIKI_DIR = PROJECT_DIR / "wiki"
STATE_DIR = PROJECT_DIR / ".cmc"
CODEX_STATE_FILE = STATE_DIR / "codex-last-flush.json"
HOOK_INPUT: dict = {}


def _read_hook_input() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


HOOK_INPUT = _read_hook_input()


def _load_project_config() -> dict:
    cfg_path = PROJECT_DIR / ".cmc-config.json"
    if cfg_path.is_symlink() or not cfg_path.is_file():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _config_int(name: str, default: int) -> int:
    cfg = _load_project_config()
    if name.lower() in cfg:
        try:
            return int(cfg[name.lower()])
        except (TypeError, ValueError):
            pass
    val = os.environ.get(f"CMC_{name}")
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return default


MAX_TURNS = _config_int("MAX_EXTRACT_TURNS", 30)
MAX_CONTEXT_CHARS = _config_int("MAX_EXTRACT_CHARS", 15_000)
MIN_TURNS_TO_FLUSH = _config_int("MIN_TURNS_TO_FLUSH", 3)


def _setup_logging() -> None:
    if not WIKI_DIR.exists():
        logging.basicConfig(level=logging.WARNING)
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if (STATE_DIR / "flush.log").is_symlink():
        logging.basicConfig(level=logging.WARNING)
        return
    logging.basicConfig(
        filename=str(STATE_DIR / "flush.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [codex-stop] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def _iter_session_files() -> list[Path]:
    sessions_dir = _codex_home() / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted((p for p in sessions_dir.rglob("*.jsonl") if _safe_session(p)),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _safe_session(path: Path) -> bool:
    root = _codex_home() / "sessions"
    try:
        contained_file(path, root)
    except ValueError:
        return False
    return True


def _session_meta(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "session_meta":
                    payload = entry.get("payload")
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    return {}


def _find_latest_session() -> tuple[Path | None, str]:
    explicit_id = HOOK_INPUT.get("session_id") or os.environ.get("CODEX_SESSION_ID")
    explicit_path = (
        HOOK_INPUT.get("session_path")
        or HOOK_INPUT.get("transcript_path")
        or os.environ.get("CODEX_SESSION_PATH")
        or os.environ.get("CODEX_TRANSCRIPT_PATH")
    )
    if explicit_path:
        path = Path(str(explicit_path)).expanduser()
        if _safe_session(path):
            meta = _session_meta(path)
            cwd = meta.get("cwd")
            if cwd and str(Path(cwd).expanduser().resolve()) == str(PROJECT_DIR):
                session_id = str(meta.get("id") or "")
                if valid_session_id(session_id) and (not explicit_id or str(explicit_id) == session_id):
                    return path, session_id
        return None, "unknown"

    if not explicit_id or not valid_session_id(str(explicit_id)):
        return None, "unknown"
    project_str = str(PROJECT_DIR)
    for path in _iter_session_files():
        meta = _session_meta(path)
        session_id = str(meta.get("id") or "")
        if explicit_id and session_id == str(explicit_id):
            cwd = meta.get("cwd")
            if cwd and str(Path(cwd).expanduser().resolve()) == project_str:
                return path, session_id

    return None, "unknown"


def _extract_text_from_message_payload(payload: dict) -> tuple[str | None, str | None]:
    payload_type = payload.get("type")

    if payload_type == "user_message":
        return "User", str(payload.get("message") or "").strip()

    if payload_type == "agent_message":
        return "Assistant", str(payload.get("message") or "").strip()

    if payload_type == "message":
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return None, None

        parts: list[str] = []
        content = payload.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
        elif isinstance(content, str):
            parts.append(content)

        label = "User" if role == "user" else "Assistant"
        return label, "\n".join(parts).strip()

    return None, None


def extract_codex_context_since(session_path: Path, offset: int = 0) -> tuple[str, int, int]:
    """Extract only complete JSONL records appended after a byte offset."""
    turns: list[str] = []
    previous: tuple[str, str] | None = None
    size = session_path.stat().st_size
    if not isinstance(offset, int) or offset < 0 or offset > size:
        offset = 0
    with session_path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    boundary = data.rfind(b"\n")
    if boundary < 0:
        return "", 0, offset
    complete = data[:boundary + 1]
    end_offset = offset + len(complete)

    for raw_line in complete.splitlines():
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue

        label, text = _extract_text_from_message_payload(payload)
        if not label or not text:
            continue

        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        key = (label, text)
        if key == previous:
            continue
        previous = key
        turns.append(f"**{label}:** {text}\n")

    recent = turns[-MAX_TURNS:]
    context = "\n".join(recent)

    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]
        boundary = context.find("\n**")
        if boundary > 0:
            context = context[boundary + 1 :]

    return context, len(recent), end_offset


def extract_codex_context(session_path: Path) -> tuple[str, int]:
    context, count, _ = extract_codex_context_since(session_path)
    return context, count


def _load_state() -> dict:
    if CODEX_STATE_FILE.exists():
        try:
            return json.loads(CODEX_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(CODEX_STATE_FILE, json.dumps(state, indent=2))


def _spawn_flush(context_file: Path, session_id: str) -> None:
    flush_script = SCRIPTS_DIR / "flush.py"
    cmd = [
        "uv",
        "run",
        "--directory",
        str(TOOL_ROOT),
        "python",
        str(flush_script),
        str(context_file),
        f"codex-{session_id}",
        str(PROJECT_DIR),
    ]

    env = os.environ.copy()
    env["CMC_PROJECT_DIR"] = str(PROJECT_DIR)
    env["CLAUDE_INVOKED_BY"] = "codex_memory_flush"

    kwargs: dict = {"env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def _retry_failed_flush() -> bool:
    """Retry one retained Grok flush after a short backoff."""
    now = datetime.now(timezone.utc).timestamp()
    for context_file in sorted(STATE_DIR.glob("codex-flush-*.md")):
        try:
            if now - context_file.stat().st_mtime < 600:
                continue
        except OSError:
            continue
        with project_lock(PROJECT_DIR):
            if claim_flush(context_file, "retry", PROJECT_DIR):
                return True
    return False


def main() -> int:
    try:
        validate_layout(PROJECT_DIR, create_state=False)
    except ValueError:
        return 0
    _setup_logging()

    if "--dry-run" in sys.argv:
        session_path, session_id = _find_latest_session()
        context, turn_count = extract_codex_context(session_path) if session_path else ("", 0)
        print(json.dumps({"project_dir": str(PROJECT_DIR), "session_id": session_id,
                          "session_path": str(session_path) if session_path else None,
                          "turn_count": turn_count, "context_chars": len(context)}, indent=2))
        return 0

    if not managed_dir(PROJECT_DIR, "wiki").is_dir():
        logging.info("SKIP: memory not enabled for project %s (no wiki/ dir)", PROJECT_DIR)
        return 0

    if _retry_failed_flush():
        logging.info("Retrying retained Codex context")

    session_path, session_id = _find_latest_session()
    if session_path is None:
        logging.info("SKIP: no Codex session file found")
        return 0

    with project_lock(PROJECT_DIR):
        return _capture_session(session_path, session_id)


def _capture_session(session_path: Path, session_id: str) -> int:
    if (STATE_DIR / "purge-paused").exists():
        return 0
    state = _load_state()
    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
    prior = sessions.get(session_id, {})
    if not isinstance(prior, dict) or prior.get("session_path") != str(session_path):
        prior = {}
    session_mtime = session_path.stat().st_mtime
    if (
        prior.get("session_path") == str(session_path)
        and float(prior.get("session_mtime", 0)) >= session_mtime
    ):
        logging.info("SKIP: Codex session already flushed: %s", session_path)
        return 0

    offset = prior.get("session_offset", 0)
    context, turn_count, end_offset = extract_codex_context_since(session_path, offset)
    next_state = {
        "session_path": str(session_path),
        "session_offset": end_offset,
        "session_mtime": session_mtime,
        "flushed_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    sessions[session_id] = next_state
    state = {"sessions": dict(sorted(
        sessions.items(), key=lambda item: str(item[1].get("flushed_at", "")), reverse=True
    )[:50])}
    if not context.strip():
        _save_state(state)
        logging.info("SKIP: empty Codex context from %s", session_path)
        return 0
    if turn_count < MIN_TURNS_TO_FLUSH:
        # Keep the previous cursor so the next Stop can batch this short delta.
        logging.info("SKIP: only %d turns (min %d)", turn_count, MIN_TURNS_TO_FLUSH)
        return 0

    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S-%f")
    context_file = STATE_DIR / f"codex-flush-{session_id}-{timestamp}.md"
    context_file.write_text(context, encoding="utf-8")
    _save_state(state)

    try:
        claimed = claim_flush(context_file, session_id, PROJECT_DIR)
    except Exception as e:
        logging.error("Failed to spawn flush.py for Codex session %s: %s", session_id, e)
        return 0
    logging.info("%s flush.py for Codex session %s (%d turns)",
                 "Spawned" if claimed else "Retained", session_id, turn_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
