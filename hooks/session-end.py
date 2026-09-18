"""
SessionEnd hook - capture conversation transcript and spawn flush.py.

When a Claude Code session ends, this hook reads the transcript path from
stdin, extracts the last N conversation turns, and spawns flush.py as a
detached background process to extract knowledge into the current project's
daily log.

The hook itself does NO API calls — only local file I/O (<10s).
Opt-in: skips entirely if the project has no wiki/ directory.
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

# Recursion guard: if we were spawned by flush.py (which calls the Agent SDK,
# which runs Claude Code, which would fire this hook again), exit immediately.
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

TOOL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = TOOL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from path_safety import managed_dir, validate_layout

if (TOOL_ROOT / ".disabled-by-codex").exists() or os.environ.get("CMC_DISABLED") == "1":
    sys.exit(0)


def _discover_project_dir() -> Path:
    val = os.environ.get("CLAUDE_PROJECT_DIR")
    if val:
        p = Path(val).expanduser().resolve()
        if p.exists():
            return p
    return Path(os.getcwd()).resolve()


def _config_int(name: str, default: int) -> int:
    val = os.environ.get(f"CMC_{name}")
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return default


def _load_project_config(project_dir: Path) -> dict:
    cfg_path = project_dir / ".cmc-config.json"
    if cfg_path.is_symlink() or not cfg_path.is_file():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


PROJECT_DIR = _discover_project_dir()
WIKI_DIR = PROJECT_DIR / "wiki"
STATE_DIR = PROJECT_DIR / ".cmc"

_cfg = _load_project_config(PROJECT_DIR)
MAX_TURNS = int(_cfg.get("max_extract_turns", _config_int("MAX_EXTRACT_TURNS", 30)))
MAX_CONTEXT_CHARS = int(_cfg.get("max_extract_chars", _config_int("MAX_EXTRACT_CHARS", 15_000)))
MIN_TURNS_TO_FLUSH = int(_cfg.get("min_turns_to_flush", _config_int("MIN_TURNS_TO_FLUSH", 1)))


def _setup_logging() -> None:
    if not managed_dir(PROJECT_DIR, "wiki").is_dir():
        logging.basicConfig(level=logging.WARNING)
        return
    managed_dir(PROJECT_DIR, ".cmc", create=True)
    if (STATE_DIR / "flush.log").is_symlink():
        logging.basicConfig(level=logging.WARNING)
        return
    logging.basicConfig(
        filename=str(STATE_DIR / "flush.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [session-end] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def extract_conversation_context(transcript_path: Path) -> tuple[str, int]:
    """Read JSONL transcript and extract last ~N conversation turns as markdown."""
    turns: list[str] = []

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
            else:
                role = entry.get("role", "")
                content = entry.get("content", "")

            if role not in ("user", "assistant"):
                continue

            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)

            if isinstance(content, str) and content.strip():
                label = "User" if role == "user" else "Assistant"
                turns.append(f"**{label}:** {content.strip()}\n")

    recent = turns[-MAX_TURNS:]
    context = "\n".join(recent)

    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]
        boundary = context.find("\n**")
        if boundary > 0:
            context = context[boundary + 1:]

    return context, len(recent)


def main() -> None:
    try:
        validate_layout(PROJECT_DIR)
    except ValueError:
        return
    _setup_logging()

    if not managed_dir(PROJECT_DIR, "wiki").is_dir():
        # Opt-in: silent on projects that don't use the wiki.
        return

    try:
        raw_input = sys.stdin.read()
        try:
            hook_input: dict = json.loads(raw_input)
        except json.JSONDecodeError:
            fixed_input = re.sub(r'(?<!\\)\\(?!["\\])', r'\\\\', raw_input)
            hook_input = json.loads(fixed_input)
    except (json.JSONDecodeError, ValueError, EOFError) as e:
        logging.error("Failed to parse stdin: %s", e)
        return

    session_id = hook_input.get("session_id", "unknown")
    source = hook_input.get("source", "unknown")
    transcript_path_str = hook_input.get("transcript_path", "")

    logging.info(
        "SessionEnd fired: session=%s source=%s project=%s",
        session_id, source, PROJECT_DIR,
    )

    if not transcript_path_str or not isinstance(transcript_path_str, str):
        logging.info("SKIP: no transcript path")
        return

    transcript_path = Path(transcript_path_str)
    if transcript_path.is_symlink() or not transcript_path.is_file():
        logging.info("SKIP: transcript missing: %s", transcript_path_str)
        return

    # Per-turn Stop already captured most turns into .cmc/pending/<session>.md.
    # Capture any tail since the last Stop, then hand the whole buffer to flush.py
    # (batched Grok). Only the un-flushed delta is summarized, so this never
    # duplicates the Stop captures. If Stop never ran, the buffer holds the whole
    # session and is flushed here.
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from pending import capture_to_buffer, flush_session

        n = capture_to_buffer(PROJECT_DIR, session_id, str(transcript_path))
        flushed = flush_session(PROJECT_DIR, session_id)
        logging.info(
            "SessionEnd: captured %d tail turn(s), flushed=%s for session %s",
            n, flushed, session_id,
        )
    except Exception as e:
        logging.error("SessionEnd capture/flush failed: %s", e)


if __name__ == "__main__":
    main()
