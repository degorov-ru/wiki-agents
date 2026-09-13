"""
Compile daily conversation logs into structured knowledge articles.

This is the "LLM compiler" - it reads daily logs (source code) and produces
organized knowledge articles (the executable).

Usage:
    uv run python compile.py                    # compile new/changed logs only
    uv run python compile.py --all              # force recompile everything
    uv run python compile.py --file daily/2026-04-01.md  # compile a specific log
    uv run python compile.py --dry-run          # show what would be compiled
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
if (TOOL_ROOT / ".disabled-by-codex").exists() or os.environ.get("CMC_DISABLED") == "1":
    print("disabled by Codex kill-switch")
    sys.exit(0)

from config import (
    AGENTS_FILE,
    COMPILE_RUNS_FILE,
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    KNOWLEDGE_DIR,
    PROJECT_DIR,
    ensure_state_dir,
    now_iso,
)
from utils import (
    file_hash,
    list_raw_files,
    list_wiki_articles,
    load_state,
    read_wiki_index,
    save_state,
)

ensure_state_dir()

# Grok needs the project CWD so Read/Write tools resolve inside its wiki.
ROOT_DIR = PROJECT_DIR

# A long daily log is split into chunks compiled by separate agent runs, so no
# single agent has to hold the whole log in context and grind for many turns
# (the base context is re-read on every turn, so a fat context is the main cost
# driver). Tune via CMC_CHUNK_CHARS; 0 disables splitting.
CHUNK_CHARS = int(os.environ.get("CMC_CHUNK_CHARS", "40000"))

MAX_TURNS = int(os.environ.get("CMC_MAX_TURNS", "30"))

SYSTEM_PROMPT = (
    "You are a knowledge compiler. Read a daily conversation log and extract "
    "knowledge into structured wiki articles using the Read/Write/Edit/Glob/"
    "Grep tools. Follow the task instructions exactly. Be concise; do not "
    "narrate."
)


# Every chunk re-sends the whole wiki so the agent knows what already exists
# and updates instead of duplicating. That context grows with the wiki and
# eventually dwarfs the log itself, and it is re-read on every agent turn.
# Past this many characters we send an index (path + one-line summary) instead
# of full article bodies and let the agent Read the few it actually needs —
# it already has the Read/Grep tools. Set 0 to always inline everything.
WIKI_CTX_MAX_CHARS = int(os.environ.get("CMC_WIKI_CTX_MAX_CHARS", "60000"))


def _article_summary(content: str, limit: int = 120) -> str:
    """One-line gist of an article: frontmatter description/title if present,
    else the first real line of prose."""
    lines = content.splitlines()
    in_fm = False
    body_start = 0
    for idx, line in enumerate(lines):
        if idx == 0 and line.strip() == "---":
            in_fm = True
            continue
        if in_fm:
            if line.strip() == "---":
                body_start = idx + 1
                break
            for key in ("description:", "summary:", "title:"):
                if line.strip().lower().startswith(key):
                    val = line.split(":", 1)[1].strip().strip('"\'')
                    if val:
                        return val[:limit]
    for line in lines[body_start:]:
        s = line.strip()
        if s and not s.startswith("#"):
            return s[:limit]
    return "(no summary)"


def _build_wiki_context(existing: dict) -> tuple[str, str]:
    """Render the 'Existing Wiki Articles' prompt section.

    Returns (text, mode) where mode is full/index/empty — recorded per run so
    the effect on token usage is visible in compile-runs.jsonl.
    """
    if not existing:
        return "(No existing articles yet)", "empty"

    full = "\n\n".join(
        f"### {rel}\n```markdown\n{content}\n```"
        for rel, content in existing.items()
    )
    if WIKI_CTX_MAX_CHARS <= 0 or len(full) <= WIKI_CTX_MAX_CHARS:
        return full, "full"

    listing = "\n".join(
        f"- `{rel}` — {_article_summary(content)}"
        for rel, content in sorted(existing.items())
    )
    text = (
        f"The wiki already contains {len(existing)} articles. They are listed "
        "below by path and summary rather than inlined in full.\n\n"
        "**Before you create an article, check this list.** If the concept is "
        "already here, open that file with the Read tool and update it instead "
        "of creating a duplicate. Use Read/Grep to inspect any article you need "
        "to link to or extend.\n\n"
        f"{listing}"
    )
    return text, "index"


def _append_run(record: dict) -> None:
    """Append one JSON line describing a single compile attempt. Best-effort:
    logging must never break compilation, so failures here are swallowed."""
    try:
        ensure_state_dir()
        record = {"ts": now_iso(), **record}
        with open(COMPILE_RUNS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def split_log(text: str, max_chars: int) -> list[str]:
    """Split a daily log into chunks no larger than ~max_chars, breaking only
    at blank-line (paragraph) boundaries so log entries stay intact.

    Returns a single-element list when the log fits or splitting is disabled.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        piece = para + "\n\n"
        if cur and len(cur) + len(piece) > max_chars:
            chunks.append(cur.rstrip("\n"))
            cur = ""
        cur += piece
        # A single paragraph bigger than max_chars becomes its own chunk.
        if len(cur) >= max_chars:
            chunks.append(cur.rstrip("\n"))
            cur = ""
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    return chunks or [text]


def _build_prompt(log_path, chunk_text, part_note, schema, wiki_index,
                  existing_articles_context, timestamp):
    return f"""You are a knowledge compiler. Your job is to read a daily conversation log
and extract knowledge into structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{wiki_index}

## Existing Wiki Articles

{existing_articles_context if existing_articles_context else "(No existing articles yet)"}

## Daily Log to Compile

**File:** {log_path.name}{part_note}

{chunk_text}

## Your Task

Read the daily log above and compile it into wiki articles following the schema exactly.

### Rules:

1. **Extract key concepts** - Identify 3-7 distinct concepts worth their own article
2. **Create concept articles** in `wiki/concepts/` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing to the daily log file
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `wiki/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - Read the existing article, add the new information, add the source to frontmatter
   - If a concept in this part already has an article (see Existing Wiki Articles above),
     UPDATE it instead of creating a duplicate
5. **Update wiki/index.md** - Add new entries to the table
   - Each entry: `| [[path/slug]] | One-line summary | source-file | {timestamp[:10]} |`
6. **Append to wiki/log.md** - Add a timestamped entry:
   ```
   ## [{timestamp}] compile | {log_path.name}
   - Source: daily/{log_path.name}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```

### File paths:
- Write concept articles to: {CONCEPTS_DIR}
- Write connection articles to: {CONNECTIONS_DIR}
- Update index at: {KNOWLEDGE_DIR / 'index.md'}
- Append log at: {KNOWLEDGE_DIR / 'log.md'}

### Quality standards:
- Every article must have complete YAML frontmatter
- Every article must link to at least 2 other articles via [[wikilinks]]
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts section should have 2+ entries
- Sources section should cite the daily log with specific claims extracted
"""


async def compile_daily_log(log_path: Path, state: dict) -> float:
    """Compile a single daily log into knowledge articles.

    A long log is split into chunks compiled by separate, sequential agent
    runs. Wiki state (index + existing articles) is re-read before each chunk
    so later chunks see the articles earlier chunks produced and update/link
    them instead of creating duplicates.

    Returns the total API cost of the compilation.
    """
    log_content = log_path.read_text(encoding="utf-8")
    schema = AGENTS_FILE.read_text(encoding="utf-8")

    chunks = split_log(log_content, CHUNK_CHARS)
    n = len(chunks)
    if n > 1:
        print(f"  Log is large ({len(log_content)} chars) - splitting into {n} chunks.")

    total_cost = 0.0

    for i, chunk_text in enumerate(chunks, 1):
        # Re-read wiki state each chunk so later chunks see earlier output.
        wiki_index = read_wiki_index()
        existing = {}
        for article_path in list_wiki_articles():
            rel = article_path.relative_to(KNOWLEDGE_DIR)
            existing[str(rel)] = article_path.read_text(encoding="utf-8")
        existing_articles_context, wiki_ctx_mode = _build_wiki_context(existing)

        part_note = f" (part {i} of {n})" if n > 1 else ""
        timestamp = now_iso()
        prompt = _build_prompt(
            log_path, chunk_text, part_note, schema, wiki_index,
            existing_articles_context, timestamp,
        )

        if n > 1:
            print(f"  Chunk {i}/{n} ({len(chunk_text)} chars)...")

        chunk_meta = {
            "file": log_path.name,
            "chunk": f"{i}/{n}",
            "chunk_chars": len(chunk_text),
            "wiki_ctx_chars": len(existing_articles_context),
            "wiki_ctx_mode": wiki_ctx_mode,
            "n_articles": len(existing),
        }

        from model_client import run_text_prompt

        result = run_text_prompt(
            SYSTEM_PROMPT + "\n\n" + prompt,
            ROOT_DIR,
            max_turns=MAX_TURNS,
            tools=("Read", "Write", "Edit", "Glob", "Grep"),
        )
        _append_run({**chunk_meta, **result, "fallback": bool(result.get("fallback_from")),
                     "deferred": not result.get("ok")})
        cost = result.get("cost_usd") or 0.0
        if not result.get("ok"):
            print(f"  [{result.get('engine', 'engine')}] FAIL: {result.get('error')} - leaving log pending")
            state["total_cost"] = state.get("total_cost", 0.0) + total_cost
            save_state(state)
            return total_cost
        total_cost += cost
        print(f"  [{result['engine']}] ok  cost≈${cost:.4f}  {result.get('total_tokens') or ''}")

    # Update state
    rel_path = log_path.name
    state.setdefault("ingested", {})[rel_path] = {
        "hash": file_hash(log_path),
        "compiled_at": now_iso(),
        "cost_usd": total_cost,
        "chunks": n,
    }
    state["total_cost"] = state.get("total_cost", 0.0) + total_cost
    save_state(state)

    return total_cost


def main():
    parser = argparse.ArgumentParser(description="Compile daily logs into knowledge articles")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    state = load_state()

    # Determine which files to compile
    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = DAILY_DIR / target.name
        if not target.exists():
            # Try resolving relative to project root
            target = ROOT_DIR / args.file
        if not target.exists():
            print(f"Error: {args.file} not found")
            sys.exit(1)
        to_compile = [target]
    else:
        all_logs = list_raw_files()
        if args.all:
            to_compile = all_logs
        else:
            to_compile = []
            for log_path in all_logs:
                rel = log_path.name
                prev = state.get("ingested", {}).get(rel, {})
                # Recompile if never seen, changed, or last attempt failed
                # (failure retry pacing is handled by maybe_compile.py).
                if not prev or prev.get("hash") != file_hash(log_path) or prev.get("failed_at"):
                    to_compile.append(log_path)

    if not to_compile:
        print("Nothing to compile - all daily logs are up to date.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")

    if args.dry_run:
        return

    try:
        # Compile each file sequentially
        total_cost = 0.0
        for i, log_path in enumerate(to_compile, 1):
            print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
            cost = asyncio.run(compile_daily_log(log_path, state))
            total_cost += cost
            print(f"  Done.")

        articles = list_wiki_articles()
        print(f"\nCompilation complete. Total cost: ${total_cost:.2f}")
        print(f"Knowledge base: {len(articles)} articles")
    finally:
        lock_path = os.environ.get("CMC_COMPILE_LOCK")
        if lock_path:
            Path(lock_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
