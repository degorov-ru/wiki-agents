"""
Opt-in initializer: turn the current directory into a project the
claude-memory-compiler watches.

Usage:
    uv run python scripts/init_project.py [project_dir]

Creates (idempotently):
    <project>/wiki/
        index.md
        log.md
        concepts/
        connections/
        qa/
    <project>/daily/
    <project>/sources/
    <project>/.cmc/        (state, logs — gitignored)
    <project>/AGENTS.md    (single source of agent rules — only if absent)
    <project>/.codex/hooks.json  (Codex Stop hook — merged if present)
    <project>/.cmc-config.json  (token-budget knobs — only if absent)
    <project>/.gitignore   (adds .cmc/ entry if missing)

If ai-memory/ already exists, prefers to defer to migrate_ai_memory.py.
"""

from __future__ import annotations

import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from path_safety import validate_layout


DEFAULT_CONFIG = {
    "max_index_chars": 10000,
    "max_log_lines": 30,
    "max_total_chars": 15000,
    "min_turns_to_flush": 3,
    "min_turns_to_flush_compact": 5,
    "max_extract_turns": 30,
    "max_extract_chars": 15000,
    "compile_after_hour": 4,
}

INDEX_TEMPLATE = """# Wiki Index

_Auto-maintained by the claude-memory-compiler. One row per article._

| Article | Summary | Source | Updated |
|---|---|---|---|
| _(no articles yet)_ | | | |
"""

LOG_TEMPLATE = """# Log

Append-only chronological record of wiki changes. Never delete past entries.

---
"""

ACCESS_TEMPLATE = """# Project Access Registry

Store operational coordinates only: environments, public URLs, SSH aliases,
remote root paths, deployment targets, account labels, and important access
notes.

Never store passwords, API tokens, private keys, cookies, recovery codes, or
other secret values here. Record only a secure-storage location or credential
label when needed.
"""

AGENTS_TEMPLATE = """# Project Agent Guide

This file is the single source of truth for coding agents and the memory compiler.

## Goal

Maintain continuity between chats and agents. Each project owns its own memory:
raw sources, daily conversation logs, and compiled Markdown wiki articles all
live inside the project directory.

## Agent startup

At the start of meaningful work:

1. Read `ACCESS.md` if it exists, especially before any remote or production work.
2. Read `wiki/index.md`.
3. Read the most recent `daily/YYYY-MM-DD.md` if it exists.
4. Open any relevant pages under `wiki/concepts/`, `wiki/connections/`, or
   `wiki/qa/` before answering from memory.

When a chat reveals or corrects a URL, SSH alias, hosting account, remote path,
deployment target, or other access coordinate, update `ACCESS.md` immediately.
Do not store passwords, tokens, private keys, cookies, or other secret values;
store only safe coordinates and references to secure credential storage.

Claude Code normally receives this context from SessionStart hooks. Codex reads
this `AGENTS.md` automatically when substantial work starts.

When Claude Code and Codex are open at the same time, treat `daily/` as the
shared append-only activity log. Before making project decisions or writing
wiki pages, reread `wiki/index.md` and the tail of today's `daily/` log.

## Agent shutdown / memory writeback

Before ending a substantial turn or session, preserve useful knowledge:

1. Append a concise entry to `daily/YYYY-MM-DD.md` with decisions, commands,
   gotchas, and next actions.
2. If stable project knowledge changed, update or create the relevant wiki page.
3. Keep `wiki/index.md` and `wiki/log.md` in sync with wiki changes.

Skip trivial chatter and routine tool output. Prefer durable facts, decisions,
procedures, and unresolved follow-ups.

## Layers

- `sources/` — raw documents you hand-feed (never edited by the LLM)
- `daily/`   — append-only conversation logs (auto-populated by hooks)
- `wiki/`    — the compiled knowledge base
    - `wiki/index.md`        — master catalog (one row per article)
    - `wiki/log.md`          — chronological build log
    - `wiki/concepts/`       — atomic articles, one topic per file
    - `wiki/connections/`    — articles that link 2+ concepts
    - `wiki/qa/`             — filed query answers

## Article format

Every article in `wiki/concepts/*.md` should have YAML frontmatter:

```yaml
---
title: Short Title
kind: decision
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
sources:
  - daily/2026-05-22.md
  - sources/<doc>.md
tags: [tag-a, tag-b]
---
```

Cross-link liberally with `[[concepts/slug]]` wikilinks.

## Categories to fill (delete or extend as needed)

- Infrastructure
- Networking
- Services
- Security
- Operations
- Troubleshooting

## Auto-flush

Claude Code hooks in `~/.claude/settings.json` capture sessions:

- `SessionStart` initializes missing project memory, injects `ACCESS.md` when
  present plus `wiki/index.md`, includes the recent daily log, and triggers a
  wake-on-resume compile check.
- `SessionEnd` and `PreCompact` extract important conversation details into
  `daily/YYYY-MM-DD.md`.
- After `compile_after_hour` (default `04:00` local), changed daily logs are
  compiled into `wiki/` on the next flush or session start. If the computer was
  off at 04:00, the next session start is the fallback trigger.

Codex uses a project-local `.codex/hooks.json` Stop hook. The hook calls
`claude-memory-compiler/hooks/codex-stop.py`, which finds the latest Codex
session JSONL for this project, extracts recent user/assistant messages, and
reuses the same `flush.py` path as Claude Code.

Parallel safety:

- `daily/YYYY-MM-DD.md` is append-only and writes are protected by `.cmc/daily.lock`.
- `compile.py` is protected by `.cmc/compile.lock`, so only one compiler process updates `wiki/` at a time.
- Agents should not assume another active agent has finished until its session has flushed into `daily/`.
"""

GITIGNORE_ENTRIES = [".cmc/", "daily/", "sources/", "wiki/", "ACCESS.md", ".cmc-config.json"]
AGENTS_BLOCK_START = "<!-- wiki-agents:memory-start -->"
AGENTS_BLOCK_END = "<!-- wiki-agents:memory-end -->"
AGENTS_BLOCK = f"""{AGENTS_BLOCK_START}
## Project memory

Before substantial work, read `wiki/index.md`, the latest file in `daily/`,
and relevant pages under `wiki/`. Keep stable facts traceable to `daily/` or
`sources/`; never store credentials in project memory.
{AGENTS_BLOCK_END}
"""


def _preflight(project_dir: Path) -> None:
    validate_layout(project_dir)
    for path in (
        project_dir / "wiki/concepts", project_dir / "wiki/connections",
        project_dir / "wiki/qa", project_dir / ".gitignore",
        project_dir / "ACCESS.md", project_dir / "AGENTS.md",
        project_dir / ".cmc-config.json", project_dir / ".codex/hooks.json",
    ):
        if path.is_symlink():
            raise ValueError(f"initializer refuses symlinked path: {path.relative_to(project_dir)}")


def _ensure(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _ensure_dir(path: Path) -> bool:
    if path.exists():
        return False
    path.mkdir(parents=True, exist_ok=True)
    return True


def _update_gitignore(project_dir: Path) -> bool:
    gi = project_dir / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    rules = {line.strip() for line in existing.splitlines() if line.strip() and not line.lstrip().startswith("#")}
    new_lines = [e for e in GITIGNORE_ENTRIES if e not in rules]
    if not new_lines:
        return False
    block = "\n# claude-memory-compiler\n" + "\n".join(new_lines) + "\n"
    if existing and not existing.endswith("\n"):
        block = "\n" + block
    gi.write_text(existing + block, encoding="utf-8")
    return True


def _update_agents(project_dir: Path) -> bool:
    path = project_dir / "AGENTS.md"
    if not path.exists():
        return _ensure(path, AGENTS_TEMPLATE.rstrip() + "\n\n" + AGENTS_BLOCK + "\n")
    existing = path.read_text(encoding="utf-8")
    if AGENTS_BLOCK_START in existing:
        return False
    separator = "" if not existing or existing.endswith("\n\n") else "\n" if existing.endswith("\n") else "\n\n"
    path.write_text(existing + separator + AGENTS_BLOCK + "\n", encoding="utf-8")
    return True


def _codex_stop_command() -> str:
    root = shlex.quote(str(Path(__file__).resolve().parent.parent))
    return f"uv run --directory {root} python hooks/codex-stop.py"


def _update_codex_hooks(project_dir: Path) -> bool:
    hooks_path = project_dir / ".codex" / "hooks.json"
    command = _codex_stop_command()

    data: dict
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Refusing to overwrite invalid JSON: {hooks_path}") from exc
        except OSError as exc:
            raise RuntimeError(f"Cannot read hooks file: {hooks_path}") from exc
    else:
        data = {}

    hooks = data.setdefault("hooks", {})
    stop_entries = hooks.setdefault("Stop", [])
    if not isinstance(stop_entries, list):
        stop_entries = []
        hooks["Stop"] = stop_entries

    before = json.dumps(data, sort_keys=True)
    kept = []
    for entry in stop_entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        remaining = [hook for hook in entry.get("hooks", []) if not (
            isinstance(hook, dict) and "hooks/codex-stop.py" in str(hook.get("command", ""))
        )]
        if remaining:
            entry = dict(entry)
            entry["hooks"] = remaining
            kept.append(entry)
    hooks["Stop"] = kept
    hooks["Stop"].append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                }
            ]
        }
    )
    if json.dumps(data, sort_keys=True) == before:
        return False

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def init(project_dir: Path) -> dict:
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise SystemExit(f"Not a directory: {project_dir}")
    _preflight(project_dir)

    created: list[str] = []

    # If there's a legacy ai-memory/, gently nudge to migrate first.
    if (project_dir / "ai-memory").is_symlink():
        raise ValueError("initializer refuses symlinked ai-memory")
    if (project_dir / "ai-memory").is_dir() and not (project_dir / "wiki").exists():
        return {
            "status": "needs_migration",
            "hint": "Run scripts/migrate_ai_memory.py first (it converts ai-memory/ to wiki/).",
            "project_dir": str(project_dir),
        }

    wiki = project_dir / "wiki"
    for sub in ("concepts", "connections", "qa"):
        if _ensure_dir(wiki / sub):
            created.append(f"wiki/{sub}/")

    if _ensure(wiki / "index.md", INDEX_TEMPLATE):
        created.append("wiki/index.md")

    if _ensure(wiki / "log.md", LOG_TEMPLATE):
        created.append("wiki/log.md")

    if _ensure_dir(project_dir / "daily"):
        created.append("daily/")
    if _ensure_dir(project_dir / "sources"):
        created.append("sources/")
    if _ensure_dir(project_dir / ".cmc"):
        created.append(".cmc/")

    if _ensure(project_dir / "ACCESS.md", ACCESS_TEMPLATE):
        created.append("ACCESS.md")

    if _update_agents(project_dir):
        created.append("AGENTS.md (created or updated)")

    if _update_codex_hooks(project_dir):
        created.append(".codex/hooks.json (updated)")

    config_path = project_dir / ".cmc-config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        created.append(".cmc-config.json")

    version_path = project_dir / ".cmc" / "version.json"
    if not version_path.exists():
        version_path.write_text(
            json.dumps({"product_version": "0.2.2", "schema_version": 1}, indent=2) + "\n",
            encoding="utf-8",
        )
        created.append(".cmc/version.json")

    if _update_gitignore(project_dir):
        created.append(".gitignore (updated)")

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    log_path = project_dir / "daily" / f"{today}.md"
    if not log_path.exists():
        log_path.write_text(
            f"# Daily Log: {today}\n\n## Sessions\n\n## Memory Maintenance\n\n"
            f"### Init ({datetime.now(timezone.utc).astimezone().strftime('%H:%M')})\n\n"
            f"Initialized wiki/ for this project.\n\n",
            encoding="utf-8",
        )
        created.append(f"daily/{today}.md")

    return {
        "status": "ok",
        "project_dir": str(project_dir),
        "created": created,
    }


def main() -> int:
    if len(sys.argv) >= 2:
        project_dir = Path(sys.argv[1])
    else:
        project_dir = Path.cwd()

    result = init(project_dir)
    print(json.dumps(result, indent=2))

    if result["status"] == "needs_migration":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
