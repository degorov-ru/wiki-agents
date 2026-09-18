"""Shared incremental reader for Claude Code JSONL transcripts.

Claude transcripts are append-only, so a monotonic BYTE OFFSET is a reliable
"already captured up to here" cursor — no dependency on per-line uuids (some lines
are queue-operations with no uuid/message). Every memory hook (Stop / SessionEnd /
PreCompact) reads turns through here so they share one cursor and never double
count. The turn formatting matches the historical session-end extractor so daily
entries stay consistent.
"""

from __future__ import annotations

import json
from pathlib import Path
from path_safety import atomic_write_text


def _turn_from_entry(entry: dict) -> str | None:
    msg = entry.get("message")
    if isinstance(msg, dict):
        role = msg.get("role", "")
        content = msg.get("content", "")
    else:
        role = entry.get("role", "")
        content = entry.get("content", "")

    if role not in ("user", "assistant"):
        return None

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        content = "\n".join(parts)

    if isinstance(content, str) and content.strip():
        label = "User" if role == "user" else "Assistant"
        return f"**{label}:** {content.strip()}\n"
    return None


def read_new_turns(transcript_path: Path, since_offset: int) -> tuple[str, int, int]:
    """Return (turns_md, new_offset, n_turns) for COMPLETE JSONL lines after
    ``since_offset``.

    Only advances past newline-terminated lines, so a transcript being written
    concurrently never yields a half-written record. If the file is smaller than
    ``since_offset`` (rotated/truncated) the cursor resets to 0.
    """
    try:
        size = transcript_path.stat().st_size
    except OSError:
        return "", since_offset, 0

    start = since_offset if 0 <= since_offset <= size else 0
    if start >= size:
        return "", start, 0

    with transcript_path.open("rb") as f:
        f.seek(start)
        raw = f.read()

    nl = raw.rfind(b"\n")
    if nl == -1:
        return "", start, 0  # no complete line yet
    consumed = raw[: nl + 1]
    new_offset = start + len(consumed)

    turns: list[str] = []
    for line in consumed.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        turn = _turn_from_entry(entry)
        if turn:
            turns.append(turn)

    return "\n".join(turns), new_offset, len(turns)


def load_offset(state_file: Path, transcript_path: str) -> int:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    entry = data.get(transcript_path) if isinstance(data, dict) else None
    if isinstance(entry, dict):
        try:
            return int(entry.get("offset", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def save_offset(state_file: Path, transcript_path: str, offset: int, session_id: str) -> None:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[transcript_path] = {"offset": int(offset), "session_id": session_id}
    if len(data) > 200:  # bound the state file
        for key in list(data.keys())[:-200]:
            del data[key]
    state_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(state_file, json.dumps(data))
