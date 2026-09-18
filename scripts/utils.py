"""Shared utilities for the personal knowledge base."""

import hashlib
import json
import re
import os
import tempfile
from pathlib import Path

from config import (
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    INDEX_FILE,
    KNOWLEDGE_DIR,
    LOG_FILE,
    PROJECT_DIR,
    QA_DIR,
    SOURCES_DIR,
    STATE_FILE,
)


def safe_root(root: Path) -> Path | None:
    """Resolve one managed directory, rejecting a symlinked root."""
    if root.is_symlink() or not root.is_dir():
        return None
    resolved = root.resolve()
    return resolved if resolved.is_relative_to(root.parent.resolve()) else None


def safe_regular_file(path: Path, root: Path) -> Path | None:
    """Return a contained regular file without following file/root escapes."""
    resolved_root = safe_root(root)
    if resolved_root is None or path.is_symlink() or not path.is_file():
        return None
    resolved = path.resolve()
    return resolved if resolved.is_relative_to(resolved_root) else None


# ── State management ──────────────────────────────────────────────────

def load_state() -> dict:
    """Load persistent state from state.json."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"ingested": {}, "query_count": 0, "last_lint": None, "total_cost": 0.0}


def save_state(state: dict) -> None:
    """Merge state under one lock; a stale reader cannot erase another ingest."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_FILE.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        if os.name != "nt":
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = load_state() if STATE_FILE.exists() else {}
            merged = {**current, **state}
            merged["ingested"] = {**current.get("ingested", {}), **state.get("ingested", {})}
            for key in ("query_count", "total_cost"):
                merged[key] = max(current.get(key, 0) or 0, state.get(key, 0) or 0)
            merged["last_lint"] = max(current.get("last_lint") or "", state.get("last_lint") or "") or None
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=STATE_FILE.parent, delete=False) as fh:
                json.dump(merged, fh, indent=2)
                fh.write("\n")
                temp = Path(fh.name)
            os.replace(temp, STATE_FILE)
        finally:
            if os.name != "nt":
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def increment_state_counter(name: str) -> int:
    """Increment one numeric field without a stale read/write window."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_FILE.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        if os.name != "nt":
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = load_state()
            state[name] = int(state.get(name, 0)) + 1
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=STATE_FILE.parent, delete=False) as fh:
                json.dump(state, fh, indent=2)
                fh.write("\n")
                temp = Path(fh.name)
            os.replace(temp, STATE_FILE)
            return state[name]
        finally:
            if os.name != "nt":
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# ── File hashing ──────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    """SHA-256 hash of a file (first 16 hex chars)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ── Slug / naming ─────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert text to a filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


# ── Wikilink helpers ──────────────────────────────────────────────────

def extract_wikilinks(content: str) -> list[str]:
    """Extract all [[wikilinks]] from markdown content."""
    return re.findall(r"\[\[([^\]]+)\]\]", content)


def wikilink_target(link: str) -> str:
    """Return the path part of a wikilink, without alias, heading, or .md."""
    target = link.split("|", 1)[0].split("#", 1)[0].strip()
    return target[:-3] if target.endswith(".md") else target


def resolve_wikilink(link: str, source_file: Path | None = None) -> Path | None:
    """Resolve rooted and Obsidian-style bare wikilinks to an article."""
    target = wikilink_target(link)
    if not target:
        return None

    candidates = [KNOWLEDGE_DIR / f"{target}.md", PROJECT_DIR / f"{target}.md"]
    if "/" not in target:
        if source_file is not None:
            candidates.append(source_file.parent / f"{target}.md")
        candidates.extend(
            subdir / f"{target}.md" for subdir in (CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR)
        )

    for path in candidates:
        if safe_regular_file(path, PROJECT_DIR):
            return path.resolve()
    return None


def wiki_article_exists(link: str, source_file: Path | None = None) -> bool:
    """Check if a wikilinked article exists on disk."""
    return resolve_wikilink(link, source_file) is not None


# ── Wiki content helpers ──────────────────────────────────────────────

def read_wiki_index() -> str:
    """Read the knowledge base index file."""
    if safe_regular_file(INDEX_FILE, KNOWLEDGE_DIR):
        return INDEX_FILE.read_text(encoding="utf-8")
    return "# Knowledge Base Index\n\n| Article | Summary | Compiled From | Updated |\n|---------|---------|---------------|---------|"


def read_all_wiki_content() -> str:
    """Read index + all wiki articles into a single string for context."""
    parts = [f"## INDEX\n\n{read_wiki_index()}"]

    for md_file in list_wiki_articles():
        rel = md_file.relative_to(KNOWLEDGE_DIR)
        content = md_file.read_text(encoding="utf-8")
        parts.append(f"## {rel}\n\n{content}")

    return "\n\n---\n\n".join(parts)


def list_wiki_articles() -> list[Path]:
    """List wiki articles without following links outside managed folders."""
    wiki_root = safe_root(KNOWLEDGE_DIR)
    if wiki_root is None:
        return []
    articles = []
    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR]:
        root = safe_root(subdir)
        if root is not None and root.is_relative_to(wiki_root):
            articles.extend(
                path for path in sorted(subdir.glob("*.md"))
                if safe_regular_file(path, subdir)
            )
    return articles


def list_raw_files() -> list[Path]:
    """List all daily log files."""
    if safe_root(DAILY_DIR) is None:
        return []
    return [path for path in sorted(DAILY_DIR.glob("*.md"))
            if safe_regular_file(path, DAILY_DIR)]


def list_source_files() -> list[Path]:
    """List manually imported Markdown/text sources."""
    if safe_root(SOURCES_DIR) is None:
        return []
    return [path for path in sorted(SOURCES_DIR.iterdir())
            if path.suffix.lower() in {".md", ".txt"}
            and safe_regular_file(path, SOURCES_DIR)]


def latest_daily_log() -> Path | None:
    """Return the newest available daily log, including after a gap."""
    logs = list_raw_files()
    return logs[-1] if logs else None


# ── Index helpers ─────────────────────────────────────────────────────

def count_inbound_links(target: str, exclude_file: Path | None = None) -> int:
    """Count how many wiki articles link to a given target."""
    target_path = (KNOWLEDGE_DIR / f"{wikilink_target(target)}.md").resolve()
    count = 0
    for article in list_wiki_articles():
        if article == exclude_file:
            continue
        content = article.read_text(encoding="utf-8")
        if any(resolve_wikilink(link, article) == target_path for link in extract_wikilinks(content)):
            count += 1
    return count


def get_article_word_count(path: Path) -> int:
    """Count words in an article, excluding YAML frontmatter."""
    content = path.read_text(encoding="utf-8")
    # Strip frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:]
    return len(content.split())


def build_index_entry(rel_path: str, summary: str, sources: str, updated: str) -> str:
    """Build a single index table row."""
    link = rel_path.replace(".md", "")
    return f"| [[{link}]] | {summary} | {sources} | {updated} |"
