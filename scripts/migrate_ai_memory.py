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
    if _has_frontmatter(content):
        return content
    src_yaml = "\n".join(f"  - {s}" for s in sources) if sources else "  - migrated from ai-memory/"
    today = _today()
    return (
        "---\n"
        f"title: {title}\n"
        f"created: {today}\n"
        "sources:\n"
        f"{src_yaml}\n"
        "tags: [migrated]\n"
        "---\n\n"
        f"{content.rstrip()}\n"
    )


def migrate(project_dir: Path) -> dict:
    ai_memory = project_dir / "ai-memory"
    wiki = project_dir / "wiki"
    concepts = wiki / "concepts"
    daily = project_dir / "daily"
    state_dir = project_dir / ".cmc"

    if not ai_memory.is_dir():
        return {"status": "noop", "reason": "no ai-memory/ found"}

    if wiki.exists():
        return {"status": "skip", "reason": "wiki/ already exists; refusing to overwrite"}

    # Create target tree
    concepts.mkdir(parents=True, exist_ok=True)
    (wiki / "connections").mkdir(parents=True, exist_ok=True)
    (wiki / "qa").mkdir(parents=True, exist_ok=True)
    daily.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    migrated: list[tuple[str, str]] = []  # (slug, summary)
    index_special: str | None = None

    for md in sorted(ai_memory.rglob("*.md")):
        rel_name = md.stem
        slug = _slugify(rel_name)
        raw = md.read_text(encoding="utf-8")
        summary = _one_line_summary(raw)

        # MEMORY.md was the old top-level index; we'll regenerate index.md
        # instead of porting it verbatim.
        if rel_name.upper() == "MEMORY":
            index_special = raw
            continue

        target = concepts / f"{slug}.md"
        wrapped = _wrap_with_frontmatter(
            raw,
            title=rel_name.replace("-", " ").replace("_", " ").title(),
            sources=[f"ai-memory/{md.name}"],
        )
        target.write_text(wrapped, encoding="utf-8")
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

    (wiki / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    # log.md
    (wiki / "log.md").write_text(
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

    # Safety net: rename instead of delete.
    backup = project_dir / "ai-memory.migrated"
    if backup.exists():
        # Should be rare; keep both by appending timestamp.
        backup = project_dir / f"ai-memory.migrated-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.move(str(ai_memory), str(backup))

    return {
        "status": "ok",
        "project_dir": str(project_dir),
        "migrated_files": len(migrated),
        "backup_path": str(backup),
    }


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
