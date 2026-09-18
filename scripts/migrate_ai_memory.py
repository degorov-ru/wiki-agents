"""
One-shot, idempotent migration of legacy ai-memory/ -> wiki/.

Triggered automatically by session-start.py the first time a project with
ai-memory/ (and no wiki/) is opened in Claude Code. Can also be invoked
manually:

    uv run python scripts/migrate_ai_memory.py [project_dir]

Behavior:

  * Creates wiki/concepts/, wiki/connections/, wiki/qa/, daily/, .cmc/.
  * Generates wiki/index.md (table format) and wiki/log.md (chronological).
  * Copies each ai-memory/*.md into wiki/concepts/ with light frontmatter.
  * Renames the source folder to ai-memory.migrated/ as a safety net.
  * Refuses to run if wiki/ already exists (so it never clobbers data).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from pending import project_lock
from memory_contract import validate_article_semantics


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def _slugify(stem: str) -> str:
    stem = stem.lower().strip()
    stem = re.sub(r"[^\w\s-]", "", stem)
    stem = re.sub(r"[\s_]+", "-", stem)
    stem = re.sub(r"-+", "-", stem)
    return stem.strip("-") or "untitled"


def _one_line_summary(content: str) -> str:
    """First non-empty, non-heading, non-frontmatter line, truncated."""
    in_fm = False
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "---":
            in_fm = not in_fm
            continue
        if in_fm:
            continue
        if line.startswith("#"):
            continue
        if len(line) > 140:
            line = line[:137].rstrip() + "..."
        return line
    return "(no description)"


def _has_frontmatter(content: str) -> bool:
    return content.lstrip().startswith("---")


def _wrap_with_frontmatter(content: str, title: str, sources: list[str]) -> str:
    src_yaml = "\n".join(f"  - {s}" for s in sources) if sources else "  - migrated from ai-memory/"
    today = _today()
    if _has_frontmatter(content):
        normalized = content.lstrip()
        end = normalized.find("\n---", 4)
        if end < 0:
            raise ValueError("legacy frontmatter is incomplete")
        content = normalized[end + 4:].lstrip()
    return (
        "---\n"
        f"title: {title}\n"
        "kind: fact\n"
        "status: proposed\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        "sources:\n"
        f"{src_yaml}\n"
        "tags: [migrated]\n"
        "---\n\n"
        f"{content.rstrip()}\n"
    )


def _backup_source(project_dir: Path, ai_memory: Path) -> Path:
    if ai_memory.is_symlink() or not ai_memory.resolve().is_relative_to(project_dir.resolve()):
        raise ValueError("unsafe legacy source directory")
    backup = project_dir / "ai-memory.migrated"
    if backup.exists():
        backup = project_dir / f"ai-memory.migrated-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.move(str(ai_memory), str(backup))
    return backup


def _migrate(project_dir: Path) -> dict:
    project_dir = project_dir.resolve()
    ai_memory = project_dir / "ai-memory"
    wiki = project_dir / "wiki"
    daily = project_dir / "daily"
    state_dir = project_dir / ".cmc"
    marker = state_dir / "migration.json"

    for path in (ai_memory, wiki, daily, state_dir, project_dir / "sources"):
        if path.is_symlink():
            raise ValueError(f"migration refuses symlinked managed path: {path.name}")
    sources_root = project_dir / "sources"
    if sources_root.exists() and any(path.is_symlink() for path in sources_root.rglob("*")):
        raise ValueError("migration refuses symlinks below sources/")

    if marker.exists() and wiki.is_dir():
        backup = _backup_source(project_dir, ai_memory) if ai_memory.is_dir() else None
        marker.unlink()
        from init_project import init
        init(project_dir)
        return {"status": "ok", "project_dir": str(project_dir), "recovered": True,
                "backup_path": str(backup) if backup else None}

    if not ai_memory.is_dir():
        return {"status": "noop", "reason": "no ai-memory/ found"}

    if wiki.exists():
        return {"status": "skip", "reason": "wiki/ already exists; refusing to overwrite"}

    state_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"schema_from": 0, "schema_to": 1}) + "\n", encoding="utf-8")
    stage = state_dir / "migration-wiki"
    if stage.is_symlink():
        raise ValueError("unsafe migration staging directory")
    if stage.exists():
        shutil.rmtree(stage)
    concepts = stage / "concepts"

    # Build the complete wiki off to the side; one rename publishes it.
    concepts.mkdir(parents=True, exist_ok=True)
    (stage / "connections").mkdir(parents=True, exist_ok=True)
    (stage / "qa").mkdir(parents=True, exist_ok=True)
    daily.mkdir(parents=True, exist_ok=True)

    migrated: list[tuple[str, str]] = []  # (slug, summary)
    index_special: str | None = None

    planned: list[tuple[Path, Path, str]] = []
    slugs: set[str] = set()
    for md in sorted(ai_memory.rglob("*.md")):
        if md.is_symlink() or not md.resolve().is_relative_to(ai_memory.resolve()):
            raise ValueError("migration refuses symlinked legacy files")
        relative = md.relative_to(ai_memory)
        rel_name = md.stem
        if rel_name.upper() == "MEMORY":
            planned.append((md, relative, ""))
            continue
        slug = _slugify("-".join(relative.with_suffix("").parts))
        if slug in slugs:
            raise ValueError(f"migration name collision: {relative}")
        slugs.add(slug)
        planned.append((md, relative, slug))

    for md, relative, slug in planned:
        rel_name = md.stem
        raw = md.read_text(encoding="utf-8")
        summary = _one_line_summary(raw)

        # MEMORY.md was the old top-level index; we'll regenerate index.md
        # instead of porting it verbatim.
        if rel_name.upper() == "MEMORY":
            index_special = raw
            continue

        source_rel = Path("sources") / "legacy-ai-memory" / relative
        source_target = project_dir / source_rel
        source_target.parent.mkdir(parents=True, exist_ok=True)
        if source_target.exists() and source_target.read_bytes() != md.read_bytes():
            raise ValueError(f"legacy source collision: {source_rel}")
        shutil.copy2(md, source_target)
        target = concepts / f"{slug}.md"
        wrapped = _wrap_with_frontmatter(
            raw,
            title=rel_name.replace("-", " ").replace("_", " ").title(),
            sources=[source_rel.as_posix()],
        )
        target.write_text(wrapped, encoding="utf-8")
        validate_article_semantics(project_dir, target, wrapped)
        migrated.append((slug, summary))

    # Build a fresh index.md
    today = _today()
    index_lines = [
        "# Wiki Index",
        "",
        f"_Migrated from ai-memory/ on {today}._",
        "",
        "| Article | Summary | Source | Updated |",
        "|---|---|---|---|",
    ]
    for slug, summary in migrated:
        index_lines.append(f"| [[concepts/{slug}]] | {summary} | ai-memory | {today} |")

    if not migrated:
        index_lines.append("| _(no articles yet)_ | | | |")

    if index_special:
        index_lines += ["", "---", "", "## Notes from legacy MEMORY.md", "", index_special.strip()]

    (stage / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    # log.md
    (stage / "log.md").write_text(
        f"# Log\n\nAppend-only chronological record of wiki changes.\n\n"
        f"---\n\n## [{_now_iso()}] migrate | ai-memory -> wiki\n"
        f"- Migrated {len(migrated)} file(s) from ai-memory/ to wiki/concepts/\n"
        f"- Source folder renamed to ai-memory.migrated/ for safety\n",
        encoding="utf-8",
    )

    # Mark first daily log so SessionStart has something to inject.
    daily_today = daily / f"{today}.md"
    if not daily_today.exists():
        daily_today.write_text(
            f"# Daily Log: {today}\n\n## Sessions\n\n## Memory Maintenance\n\n"
            f"### Migration ({datetime.now(timezone.utc).astimezone().strftime('%H:%M')})\n\n"
            f"Migrated legacy ai-memory/ to wiki/ ({len(migrated)} files).\n\n",
            encoding="utf-8",
        )

    stage.replace(wiki)
    backup = _backup_source(project_dir, ai_memory)
    marker.unlink()
    from init_project import init
    init(project_dir)

    return {
        "status": "ok",
        "project_dir": str(project_dir),
        "migrated_files": len(migrated),
        "backup_path": str(backup),
    }


def migrate(project_dir: Path) -> dict:
    with project_lock(project_dir, "migration"):
        return _migrate(project_dir)


def main() -> int:
    if len(sys.argv) >= 2:
        project_dir = Path(sys.argv[1]).expanduser().resolve()
    else:
        project_dir = Path.cwd()

    if not project_dir.is_dir():
        print(json.dumps({"status": "error", "reason": f"not a directory: {project_dir}"}))
        return 1

    try:
        result = migrate(project_dir)
    except Exception as e:
        print(json.dumps({"status": "error", "reason": str(e)}))
        return 1

    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("ok", "noop", "skip") else 1


if __name__ == "__main__":
    sys.exit(main())
