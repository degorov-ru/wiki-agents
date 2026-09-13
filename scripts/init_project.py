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
    <project>/CLAUDE.md    (pointer to AGENTS.md — only if absent)
    <project>/.codex/hooks.json  (Codex Stop hook — merged if present)
    <project>/.obsidian/   (minimal vault settings — only if absent)
    <project>/.cmc-config.json  (token-budget knobs — only if absent)
    <project>/.gitignore   (adds .cmc/ entry if missing)

If ai-memory/ already exists, prefers to defer to migrate_ai_memory.py.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


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
`CLAUDE.md` points here for Claude Code compatibility.

## Goal

Maintain continuity between chats and agents. Each project owns its own memory:
raw sources, daily conversation logs, compiled wiki articles, and Obsidian vault
metadata all live inside the project directory.

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

CLAUDE_POINTER_TEMPLATE = """# Claude Project Pointer

Read `AGENTS.md` in this project before doing substantial work.

`AGENTS.md` is the single source of truth for:

- how this project's wiki is organized;
- what to read at agent startup;
- what to write back before ending a substantial turn or session;
- how Claude Code hooks, Codex, and the compiler share continuity.
"""

OBSIDIAN_FILES = {
    "app.json": "{}",
    "appearance.json": "{}",
    "community-plugins.json": "[]",
    "core-plugins.json": json.dumps(
        {
            "file-explorer": True,
            "global-search": True,
            "switcher": True,
            "graph": True,
            "backlink": True,
            "canvas": True,
            "outgoing-link": True,
            "tag-pane": True,
            "properties": True,
            "page-preview": True,
            "daily-notes": True,
            "templates": True,
            "note-composer": True,
            "command-palette": True,
            "editor-status": True,
            "bookmarks": True,
            "outline": True,
            "word-count": True,
            "file-recovery": True,
        },
        indent=2,
    ),
    "graph.json": json.dumps(
        {
            "collapse-filter": True,
            "search": "",
            "showTags": False,
            "showAttachments": False,
            "hideUnresolved": False,
            "showOrphans": True,
            "collapse-color-groups": True,
            "colorGroups": [],
            "collapse-display": True,
            "showArrow": False,
            "textFadeMultiplier": 0,
            "nodeSizeMultiplier": 1,
            "lineSizeMultiplier": 1,
            "collapse-forces": True,
            "centerStrength": 0.518713248970312,
            "repelStrength": 10,
            "linkStrength": 1,
            "linkDistance": 250,
            "scale": 1,
            "close": False,
        },
        indent=2,
    ),
    "workspace.json": json.dumps(
        {
            "main": {
                "id": "memory-main",
                "type": "split",
                "children": [
                    {
                        "id": "memory-tabs",
                        "type": "tabs",
                        "children": [
                            {
                                "id": "memory-index",
                                "type": "leaf",
                                "state": {
                                    "type": "markdown",
                                    "state": {"file": "wiki/index.md", "mode": "source"},
                                    "icon": "lucide-file",
                                    "title": "index",
                                },
                            }
                        ],
                    }
                ],
                "direction": "vertical",
            },
            "left": {
                "id": "memory-left",
                "type": "split",
                "children": [],
                "direction": "horizontal",
                "width": 300,
            },
            "right": {
                "id": "memory-right",
                "type": "split",
                "children": [],
                "direction": "horizontal",
                "width": 300,
                "collapsed": True,
            },
            "active": "memory-index",
        },
        indent=2,
    ),
}

GITIGNORE_ENTRIES = [".cmc/", "daily/", "sources/"]


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
    new_lines = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if not new_lines:
        return False
    block = "\n# claude-memory-compiler\n" + "\n".join(new_lines) + "\n"
    if existing and not existing.endswith("\n"):
        block = "\n" + block
    gi.write_text(existing + block, encoding="utf-8")
    return True


def _codex_stop_command() -> str:
    return f"uv run --directory {json.dumps(str(Path(__file__).resolve().parent.parent))} python hooks/codex-stop.py"


def _update_codex_hooks(project_dir: Path) -> bool:
    hooks_path = project_dir / ".codex" / "hooks.json"
    command = _codex_stop_command()

    data: dict
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    hooks = data.setdefault("hooks", {})
    stop_entries = hooks.setdefault("Stop", [])
    if not isinstance(stop_entries, list):
        stop_entries = []
        hooks["Stop"] = stop_entries

    for entry in stop_entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and hook.get("command") == command:
                return False

    stop_entries.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                }
            ]
        }
    )

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def init(project_dir: Path) -> dict:
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise SystemExit(f"Not a directory: {project_dir}")

    created: list[str] = []

    # If there's a legacy ai-memory/, gently nudge to migrate first.
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

    if _ensure(project_dir / "AGENTS.md", AGENTS_TEMPLATE):
        created.append("AGENTS.md")
    if _ensure(project_dir / "CLAUDE.md", CLAUDE_POINTER_TEMPLATE):
        created.append("CLAUDE.md")

    if _update_codex_hooks(project_dir):
        created.append(".codex/hooks.json (updated)")

    obsidian_dir = project_dir / ".obsidian"
    if _ensure_dir(obsidian_dir):
        created.append(".obsidian/")
    for filename, content in OBSIDIAN_FILES.items():
        if _ensure(obsidian_dir / filename, content + "\n"):
            created.append(f".obsidian/{filename}")

    config_path = project_dir / ".cmc-config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        created.append(".cmc-config.json")

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
