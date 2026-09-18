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
import hashlib
import json
import os
import sys
from time import time
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
    SOURCES_DIR,
    ensure_state_dir,
    now_iso,
)
from utils import (
    file_hash,
    list_raw_files,
    list_source_files,
    list_wiki_articles,
    load_state,
    read_wiki_index,
    safe_regular_file,
    safe_root,
    save_state,
)
from memory_contract import apply_proposal, parse_proposal, redact_sensitive, recover_pending_commit
from init_project import AGENTS_TEMPLATE
from path_safety import append_text

ensure_state_dir()

# Provider reads project context; code owns all writes.
ROOT_DIR = PROJECT_DIR

# A long daily log is split into chunks compiled by separate agent runs, so no
# single agent has to hold the whole log in context and grind for many turns
# (the base context is re-read on every turn, so a fat context is the main cost
# driver). Tune via CMC_CHUNK_CHARS; 0 disables splitting.
CHUNK_CHARS = int(os.environ.get("CMC_CHUNK_CHARS", "40000"))

MAX_TURNS = int(os.environ.get("CMC_MAX_TURNS", "30"))

SYSTEM_PROMPT = "You are a knowledge compiler. Read only. Return JSON only."


# Every chunk gets bounded existing-wiki context from code. Models receive no
# filesystem tools; past this size the context becomes a truncated summary list.
WIKI_CTX_MAX_CHARS = int(os.environ.get("CMC_WIKI_CTX_MAX_CHARS", "60000"))
COMPILE_LOCK_STALE_SECONDS = 6 * 60 * 60


def _claim_compile_lock() -> bool:
    lock_path = PROJECT_DIR / ".cmc" / "compile.lock"
    inherited = os.environ.get("CMC_COMPILE_LOCK")
    if inherited and Path(inherited).resolve() == lock_path.resolve() and lock_path.is_file():
        return True
    if lock_path.exists() and time() - lock_path.stat().st_mtime >= COMPILE_LOCK_STALE_SECONDS:
        lock_path.unlink(missing_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print("Compile already running; leaving daily logs pending.")
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as lock:
        lock.write(f"pid={os.getpid()}\n")
    os.environ["CMC_COMPILE_LOCK"] = str(lock_path)
    return True


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
        "already here, update it through the returned proposal instead of "
        "creating a duplicate.\n\n"
        f"{listing}"
    )
    return text[:WIKI_CTX_MAX_CHARS], "index" if len(text) <= WIKI_CTX_MAX_CHARS else "index-truncated"


def _append_run(record: dict) -> None:
    """Append one JSON line describing a single compile attempt. Best-effort:
    logging must never break compilation, so failures here are swallowed."""
    try:
        ensure_state_dir()
        record = {"ts": now_iso(), **record}
        append_text(COMPILE_RUNS_FILE, json.dumps(record, ensure_ascii=False) + "\n")
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


def _build_prompt(log_path, source_rel, chunk_text, part_note, schema, wiki_index,
                  existing_articles_context, timestamp):
    return f"""You are a knowledge compiler. Your job is to read a daily conversation log
and extract knowledge into structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{wiki_index}

## Existing Wiki Articles

{existing_articles_context if existing_articles_context else "(No existing articles yet)"}

## Source Material to Compile

**File:** {source_rel}{part_note}

{chunk_text}

## Your Task

Read the daily log above. Return exactly one JSON object. Either
`{{"result":"noop","reason":"...","changes":[]}}` or
`{{"result":"changes","changes":[{{"path":"wiki/concepts/name.md","content":"..."}}]}}`.
Code, not you, writes files. Paths may only be wiki/index.md, wiki/log.md or a markdown
file under wiki/concepts, wiki/connections or wiki/qa.

### Rules:

1. Extract only durable knowledge. Zero articles is valid.
2. Propose concept articles in `wiki/concepts/` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing exactly to `{source_rel}`
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive
3. Propose connection articles in `wiki/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - Read the existing article, add the new information, add the source to frontmatter
   - If a concept in this part already has an article (see Existing Wiki Articles above),
     UPDATE it instead of creating a duplicate
5. Propose an update to wiki/index.md - Add new entries to the table
   - Each entry: `| [[path/slug]] | One-line summary | source-file | {timestamp[:10]} |`
6. Propose an append to wiki/log.md - Add a timestamped entry:
   ```
   ## [{timestamp}] compile | {source_rel}
   - Source: {source_rel}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```

### Managed file paths:
- concepts: wiki/concepts/
- connections: wiki/connections/
- index: wiki/index.md
- log: wiki/log.md

### Quality standards:
- Every article has title, kind, status, updated and sources frontmatter.
- kind is fact, decision, procedure, hypothesis or preference; status is proposed,
  active, disputed, superseded or cancelled. Use superseded only when a
  `superseded_by` wikilink names the replacement; use cancelled when no
  replacement exists. Cite precise daily anchors when available.
- Keep articles short when that is sufficient. No backlink or word-count quotas.
"""


async def compile_daily_log(log_path: Path, state: dict) -> float:
    """Compile a single daily log into knowledge articles.

    A long log is split into chunks compiled by separate, sequential agent
    runs. Wiki state (index + existing articles) is re-read before each chunk
    so later chunks see the articles earlier chunks produced and update/link
    them instead of creating duplicates.

    Returns the total API cost of the compilation.
    """
    if (PROJECT_DIR / ".cmc" / "purge-paused").exists():
        raise RuntimeError("memory is paused for controlled purge")
    safe_log = safe_regular_file(log_path, DAILY_DIR)
    source_rel = f"daily/{log_path.name}"
    if safe_log is None:
        safe_log = safe_regular_file(log_path, SOURCES_DIR)
        source_rel = f"sources/{log_path.name}"
    if safe_log is None:
        raise ValueError("input must be a regular file inside daily/ or sources/")
    log_path = safe_log
    snapshot = log_path.read_bytes()
    snapshot_hash = hashlib.sha256(snapshot).hexdigest()[:16]
    log_content = snapshot.decode("utf-8")
    rel_path = log_path.name if source_rel.startswith("daily/") else source_rel

    def record_failure(stage: str, error: object) -> None:
        failed_at = now_iso()
        state.setdefault("ingested", {})[rel_path] = {
            "hash": snapshot_hash, "failed_at": failed_at,
            "stage": stage, "error": str(error)[:500],
        }
        state["last_failure"] = {"stage": stage, "at": failed_at,
                                 "source": rel_path, "error": str(error)[:500]}
        save_state(state)
    schema_path = safe_regular_file(AGENTS_FILE, PROJECT_DIR)
    if schema_path is None:
        raise ValueError("AGENTS input escapes the project and product roots")
    schema = AGENTS_TEMPLATE + "\n\n## Project-specific agent rules\n\n" + schema_path.read_text(encoding="utf-8")

    # One proposal keeps publication atomic. Provider context limits are safer
    # than publishing half of a multi-chunk source.
    chunks = [log_content]
    n = len(chunks)
    if n > 1:
        print(f"  Log is large ({len(log_content)} chars) - splitting into {n} chunks.")

    total_cost = 0.0
    all_costs_known = True

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

        safe_chunk, chunk_redacted = redact_sensitive(chunk_text)
        prompt = _build_prompt(
            log_path, source_rel, safe_chunk, part_note, schema, wiki_index,
            existing_articles_context, timestamp,
        )
        safe_prompt, prompt_redacted = redact_sensitive(SYSTEM_PROMPT + "\n\n" + prompt)
        redacted = chunk_redacted or prompt_redacted
        result = run_text_prompt(
            safe_prompt,
            ROOT_DIR,
            max_turns=MAX_TURNS,
            tools=(),
        )
        _append_run({**chunk_meta, **result, "fallback": bool(result.get("fallback_from")),
                     "deferred": not result.get("ok")})
        cost_known = result.get("cost_known") is True or isinstance(result.get("cost_usd"), (int, float))
        cost = result.get("cost_usd") if cost_known else 0.0
        all_costs_known = all_costs_known and cost_known
        if not result.get("ok"):
            print(f"  [{result.get('engine', 'engine')}] FAIL: {result.get('error')} - leaving log pending")
            record_failure("provider", result.get("error") or "unknown")
            state["total_cost"] = state.get("total_cost", 0.0) + total_cost
            return total_cost
        try:
            proposal = parse_proposal(str(result.get("text") or ""))
            if file_hash(log_path) != snapshot_hash:
                print("  Daily changed during compile - leaving it pending")
                record_failure("source_changed", "source changed during compile")
                return total_cost
            changed = apply_proposal(PROJECT_DIR, proposal)
        except ValueError as exc:
            result = {**result, "ok": False, "error": str(exc)}
            _append_run({**chunk_meta, **result, "deferred": True})
            record_failure("proposal", exc)
            return total_cost
        if file_hash(log_path) != snapshot_hash:
            print("  Daily changed while applying changes - leaving it pending")
            record_failure("source_changed", "source changed while applying changes")
            return total_cost
        total_cost += cost
        cost_label = f"${cost:.4f}" if cost_known else "unknown"
        print(f"  [{result['engine']}] ok  cost={cost_label}  {result.get('total_tokens') or ''}")

    # Update state
    state.setdefault("ingested", {})[rel_path] = {
        "hash": snapshot_hash,
        "compiled_at": now_iso(),
        "cost_usd": total_cost if all_costs_known else None,
        "chunks": n, "changes": changed, "redacted": redacted,
    }
    state["total_cost"] = state.get("total_cost", 0.0) + total_cost
    state["last_success"] = {"stage": "compile", "at": now_iso(), "source": rel_path}
    save_state(state)

    return total_cost


def main() -> int:
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
            target = ROOT_DIR / target if len(target.parts) > 1 else DAILY_DIR / target.name
        if not target.exists():
            # Try resolving relative to project root
            target = ROOT_DIR / args.file
        if not target.exists():
            print(f"Error: {args.file} not found")
            sys.exit(1)
        to_compile = [target]
    else:
        all_logs = list_raw_files() + list_source_files()
        if args.all:
            to_compile = all_logs
        else:
            to_compile = []
            for log_path in all_logs:
                rel = log_path.name if log_path.parent == DAILY_DIR else f"sources/{log_path.name}"
                prev = state.get("ingested", {}).get(rel, {})
                # Recompile if never seen, changed, or last attempt failed
                # (failure retry pacing is handled by maybe_compile.py).
                if not prev or prev.get("hash") != file_hash(log_path) or prev.get("failed_at"):
                    to_compile.append(log_path)

    if not to_compile:
        print("Nothing to compile - all daily logs are up to date.")
        return 0

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")

    if args.dry_run:
        return 0

    if not _claim_compile_lock():
        return 0

    try:
        # Compile each file sequentially
        total_cost = 0.0
        unknown_cost = False
        failed = False
        for i, log_path in enumerate(to_compile, 1):
            print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
            cost = asyncio.run(compile_daily_log(log_path, state))
            total_cost += cost
            rel = log_path.name if log_path.parent == DAILY_DIR else f"sources/{log_path.name}"
            entry = state.get("ingested", {}).get(rel, {})
            failed = failed or bool(entry.get("failed_at"))
            unknown_cost = unknown_cost or entry.get("cost_usd") is None
            print("  Failed; left pending." if entry.get("failed_at") else "  Done.")

        articles = list_wiki_articles()
        print(f"\nCompilation complete. Reported cost: {'unknown' if unknown_cost else f'${total_cost:.2f}'}")
        print(f"Knowledge base: {len(articles)} articles")
        return 1 if failed else 0
    finally:
        lock_path = os.environ.get("CMC_COMPILE_LOCK")
        expected_lock = PROJECT_DIR / ".cmc" / "compile.lock"
        if lock_path and Path(lock_path).resolve() == expected_lock.resolve():
            expected_lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
