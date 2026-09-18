"""Recover project daily logs from completed Codex JSONL sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = TOOL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from memory_contract import redact_sensitive
from path_safety import atomic_write_text, managed_dir, validate_layout

FLUSH_SCRIPT = TOOL_ROOT / "scripts" / "flush.py"
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[^\s]{8,}"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"),
)


def session_meta(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict):
                    return item["payload"]
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def message(payload: dict) -> tuple[str | None, str | None]:
    kind = payload.get("type")
    if kind == "user_message":
        return "User", str(payload.get("message") or "").strip()
    if kind == "agent_message":
        return "Assistant", str(payload.get("message") or "").strip()
    if kind != "message" or payload.get("role") not in ("user", "assistant"):
        return None, None
    content = payload.get("content")
    blocks = content if isinstance(content, list) else [content]
    text = "\n".join(
        block.get("text", "") if isinstance(block, dict) else str(block or "")
        for block in blocks
    ).strip()
    return ("User" if payload["role"] == "user" else "Assistant"), text


def session_turns(path: Path) -> list[str]:
    turns: list[str] = []
    previous: tuple[str, str] | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                role, text = message(payload)
                if not role or not text or (role, text) == previous:
                    continue
                previous = (role, text)
                turns.append(f"**{role}:** {re.sub(r'\n{3,}', '\n\n', text)}\n")
    except OSError:
        return []
    return turns


def chunks(turns: list[str], max_turns: int = 30, max_chars: int = 15_000) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    size = 0
    for turn in turns:
        if current and (len(current) >= max_turns or size + len(turn) > max_chars):
            result.append("\n".join(current))
            current, size = [], 0
        current.append(turn)
        size += len(turn)
    if current:
        result.append("\n".join(current))
    return result


def has_secret(text: str) -> bool:
    return redact_sensitive(text)[1]


def valid_session_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", value))


def project_roots(roots: list[Path]) -> set[Path]:
    projects: set[Path] = set()
    for root in roots:
        for index in root.glob("**/wiki/index.md"):
            projects.add(index.parent.parent.resolve())
    return projects


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"processed": {}}


def save_state(path: Path, state: dict) -> None:
    atomic_write_text(path, json.dumps(state, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="YYYY-MM-DD")
    parser.add_argument("--run", action="store_true", help="Process sessions; default is dry-run")
    parser.add_argument("--compile", action="store_true", help="Compile changed daily logs afterwards")
    parser.add_argument("--root", action="append", type=Path, dest="roots")
    parser.add_argument(
        "--settle-minutes", type=int, default=10,
        help="Skip sessions modified this recently (likely still active)",
    )
    args = parser.parse_args()
    since = date.fromisoformat(args.since)
    configured_roots = [
        Path(item).expanduser()
        for item in os.environ.get("CMC_PROJECT_ROOTS", "").split(os.pathsep)
        if item.strip()
    ]
    roots = args.roots or configured_roots
    if not roots:
        parser.error("pass --root or set CMC_PROJECT_ROOTS")
    projects = project_roots(roots)
    sessions = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    planned: dict[Path, list[tuple[Path, str, list[str]]]] = {}

    for path in sessions.rglob("*.jsonl"):
        if time.time() - path.stat().st_mtime < args.settle_minutes * 60:
            continue
        meta = session_meta(path)
        if meta.get("thread_source") != "user":
            continue
        cwd = meta.get("cwd")
        if not cwd:
            continue
        project = Path(str(cwd)).expanduser().resolve()
        if project not in projects:
            continue
        session_date = datetime.fromtimestamp(path.stat().st_mtime).astimezone().date()
        if session_date < since:
            continue
        session_id = str(meta.get("id") or path.stem)
        if not valid_session_id(session_id):
            continue
        turns = session_turns(path)
        if turns:
            planned.setdefault(project, []).append((path, session_id, turns))

    total = sum(len(items) for items in planned.values())
    print(f"Projects: {len(planned)}; sessions: {total}; mode: {'run' if args.run else 'dry-run'}")
    for project, items in sorted(planned.items(), key=lambda item: str(item[0])):
        print(f"{project}: {len(items)} session(s)")
        if not args.run:
            continue
        validate_layout(project, create_state=True)
        state_path = project / ".cmc" / "codex-backfill.json"
        state = load_state(state_path)
        processed = state.setdefault("processed", {})
        for path, session_id, turns in sorted(items, key=lambda item: item[0].stat().st_mtime):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            if processed.get(session_id) == digest:
                continue
            session_date = datetime.fromtimestamp(path.stat().st_mtime).astimezone().date().isoformat()
            session_ok = True
            for part, context in enumerate(chunks(turns), 1):
                if has_secret(context):
                    print(f"SKIP secret-like content: {session_id} part {part}")
                    session_ok = False
                    continue
                backfill_dir = managed_dir(project, ".cmc") / "backfill"
                if backfill_dir.is_symlink():
                    raise ValueError("unsafe backfill directory")
                backfill_dir.mkdir(exist_ok=True)
                context_file = backfill_dir / f"{session_id}-{part}.md"
                if context_file.is_symlink():
                    raise ValueError("unsafe backfill context path")
                atomic_write_text(context_file, context)
                env = os.environ.copy()
                env["CMC_PROJECT_DIR"] = str(project)
                env["CMC_DAILY_DATE"] = session_date
                proc = subprocess.run(
                    ["uv", "run", "--directory", str(TOOL_ROOT), "python", str(FLUSH_SCRIPT),
                     str(context_file), f"backfill-{session_id}-{part}", str(project)],
                    env=env,
                )
                if proc.returncode != 0 or context_file.exists():
                    session_ok = False
            if session_ok:
                processed[session_id] = digest
                save_state(state_path, state)
        if args.compile:
            env = os.environ.copy()
            env["CMC_PROJECT_DIR"] = str(project)
            subprocess.run(
                ["uv", "run", "--directory", str(TOOL_ROOT), "python", str(TOOL_ROOT / "scripts/compile.py")],
                env=env,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
