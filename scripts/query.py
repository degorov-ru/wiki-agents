"""
Query the knowledge base using index-guided retrieval (no RAG).

The LLM reads the index, picks relevant articles, and synthesizes an answer.
No vector database, no embeddings, no chunking - just structured markdown
and an index the LLM can reason over.

Usage:
    uv run python query.py "How should I handle auth redirects?"
    uv run python query.py "What patterns do I use for API design?" --file-back
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from config import KNOWLEDGE_DIR, PROJECT_DIR, ensure_state_dir, now_iso
from memory_contract import apply_proposal, article_metadata, redact_sensitive
from utils import (
    extract_wikilinks,
    increment_state_counter,
    read_wiki_index,
    safe_regular_file,
    safe_root,
    slugify,
    wikilink_target,
)

ensure_state_dir()
ROOT_DIR = PROJECT_DIR
MAX_QUERY_ARTICLES = 6
MAX_QUERY_CONTEXT_CHARS = 60_000
MAX_FALLBACK_ARTICLES = 3


def _terms(text: str) -> set[str]:
    text = text.lower()
    return set(re.findall(r"[\w-]{2,}", text)) | set(re.findall(r"\w{3,}", text))


def _current_index_entries() -> list[tuple[str, str, str]]:
    """Return contained evidence, retaining labelled withdrawals but not stale QA."""
    if safe_root(KNOWLEDGE_DIR) is None:
        return []
    entries = []
    for line in read_wiki_index().splitlines():
        match = re.search(r"\[\[((?:concepts|connections|qa)/[\w.-]+)", line)
        if not match:
            continue
        rel = match.group(1)
        path = safe_regular_file(KNOWLEDGE_DIR / f"{rel}.md", KNOWLEDGE_DIR)
        if path is None:
            continue
        content = path.read_text(encoding="utf-8")
        try:
            fields = article_metadata(content)
            status = fields.get("status", [""])[0]
        except ValueError:
            continue
        if rel.startswith("qa/"):
            current = True
            for link in fields.get("derived_from", []):
                if not re.fullmatch(r"\[\[(?:concepts|connections|qa)/[\w.-]+\]\]", link):
                    current = False
                    break
                source = safe_regular_file(
                    KNOWLEDGE_DIR / f"{link[2:-2]}.md", KNOWLEDGE_DIR
                )
                if source is None:
                    current = False
                    break
                try:
                    source_status = article_metadata(
                        source.read_text(encoding="utf-8")
                    ).get("status", [""])[0]
                except ValueError:
                    current = False
                    break
                if source_status != "active":
                    current = False
                    break
            if not current:
                continue
        # Withdrawals must remain searchable so other pages cannot revive them.
        status = status or "proposed"
        entries.append((f"[status: {status}] {line}", rel,
                        f"Article status: {status}\n\n{content}"))
    return entries


def build_query_context(question: str, limit: int = MAX_QUERY_ARTICLES,
                        fallback_links: tuple[str, ...] = ()) -> tuple[str, int, int]:
    """Select a bounded set of articles from index rows matching question terms."""
    terms = _terms(question)
    entries = _current_index_entries()
    wanted = set(fallback_links)
    candidates: list[tuple[int, str, str]] = []
    for line, rel, content in entries:
        score = len(terms & _terms(line))
        if score or rel in wanted:
            candidates.append((score, rel, content))
    # The index selects evidence; only article bodies belong in the answer context.
    parts = []
    separator = "\n\n---\n\n"
    selected = 0
    used = 0
    for _, rel, content in sorted(candidates, key=lambda item: (-item[0], item[1])):
        section = f"## {rel}\n\n{content}"
        size = len(section) + (len(separator) if parts else 0)
        if selected >= limit or used + size > MAX_QUERY_CONTEXT_CHARS:
            continue
        parts.append(section)
        used += size
        selected += 1
    return separator.join(parts), selected, len(candidates)


def _select_fallback_links(question: str) -> tuple[str, ...]:
    """Use the bounded index to bridge languages, including explicit withdrawals."""
    entries = _current_index_entries()
    if not entries:
        return ()
    rows = "\n".join(line for line, _, _ in entries)
    safe_rows, _ = redact_sensitive(rows[:MAX_QUERY_CONTEXT_CHARS])
    prompt = f"""Select up to {MAX_FALLBACK_ARTICLES} relevant articles for the question.
Return JSON only: {{"articles":["concepts/example"]}}. Choose only links from
this index. Include relevant cancelled/superseded decisions to prevent using
withdrawn values from other articles. Return an empty list when none are relevant.

Index:
{safe_rows}

Question:
{question}"""
    from model_client import run_text_prompt

    result = run_text_prompt(prompt, ROOT_DIR, max_turns=2, tools=())
    if not result.get("ok"):
        return ()
    try:
        proposed = json.loads(str(result.get("text") or "")).get("articles", [])
    except (AttributeError, json.JSONDecodeError):
        return ()
    allowed = {rel for _, rel, _ in entries}
    chosen = []
    for rel in proposed if isinstance(proposed, list) else []:
        if isinstance(rel, str) and rel in allowed and rel not in chosen:
            chosen.append(rel)
    return tuple(chosen[:MAX_FALLBACK_ARTICLES])


def _file_back_qa(question: str, answer: str) -> str:
    """Persist an explicitly requested answer only when every citation is active."""
    wiki_root = safe_root(KNOWLEDGE_DIR)
    if wiki_root is None:
        raise ValueError("managed wiki root is unsafe")
    links = list(dict.fromkeys(wikilink_target(link) for link in extract_wikilinks(answer)))
    if not links:
        raise ValueError("answer has no cited sources")
    sources: set[str] = set()
    derived: list[str] = []
    for link in links:
        if not re.fullmatch(r"(?:concepts|connections|qa)/[\w.-]+", link):
            raise ValueError(f"unsafe or unsupported citation: {link}")
        article = safe_regular_file(KNOWLEDGE_DIR / f"{link}.md", KNOWLEDGE_DIR)
        if article is None:
            raise ValueError(f"cited article does not exist: {link}")
        fields = article_metadata(article.read_text(encoding="utf-8"))
        if fields.get("status", [""])[0] != "active":
            raise ValueError(f"cited article is not active: {link}")
        sources.update(fields.get("sources", []))
        derived.append(f"[[{link}]]")
    if not sources:
        raise ValueError("cited articles have no primary sources")

    safe_question, _ = redact_sensitive(question)
    safe_answer, _ = redact_sensitive(answer)
    title = re.sub(r"\s+", " ", safe_question).strip()[:120] or "Saved answer"
    slug = slugify(title)[:60] or "answer"
    slug += "-" + hashlib.sha256(safe_question.encode()).hexdigest()[:8]
    rel = f"qa/{slug}"
    updated = now_iso()[:10]
    qa = (
        "---\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        "kind: fact\nstatus: active\n"
        f"updated: {updated}\n"
        "sources:\n" + "".join(f"  - {source}\n" for source in sorted(sources)) +
        "derived_from:\n" + "".join(f"  - {link}\n" for link in derived) +
        "---\n\n"
        f"# {title}\n\n{safe_answer.strip()}\n"
    )
    index = read_wiki_index()
    if f"[[{rel}]]" not in index:
        index = index.rstrip() + f"\n| [[{rel}]] | Saved answer: {title.replace('|', ' ')[:80]} | {', '.join(sorted(sources))} | {updated} |\n"
    log_path = KNOWLEDGE_DIR / "log.md"
    safe_log = safe_regular_file(log_path, KNOWLEDGE_DIR)
    log = safe_log.read_text(encoding="utf-8") if safe_log else "# Log\n"
    log = log.rstrip() + f"\n\n## [{now_iso()}] query file-back | [[{rel}]]\n"
    apply_proposal(PROJECT_DIR, {"result": "changes", "changes": [
        {"path": f"wiki/{rel}.md", "content": qa},
        {"path": "wiki/index.md", "content": index},
        {"path": "wiki/log.md", "content": log},
    ]})
    return rel


def run_query(question: str, file_back: bool = False) -> str:
    """Query the knowledge base and optionally file the answer back."""
    if (PROJECT_DIR / ".cmc" / "purge-paused").exists():
        return "Memory query blocked: controlled purge is incomplete."
    safe_question, question_redacted = redact_sensitive(question)
    wiki_content, selected, candidates = build_query_context(safe_question)
    fallback_used = False
    if selected == 0 and candidates == 0:
        fallback_links = _select_fallback_links(safe_question)
        if fallback_links:
            wiki_content, selected, candidates = build_query_context(
                safe_question, fallback_links=fallback_links
            )
            fallback_used = True
    safe_wiki, wiki_redacted = redact_sensitive(wiki_content)

    tools = []

    file_back_instructions = ""
    if file_back:
        file_back_instructions = f"""

## File Back Instructions

Do not write files. Cite every durable claim with an exact included wikilink.
Code will save the answer only after validating that every citation is active
and traceable to primary daily/ or sources/ evidence.
"""

    prompt = f"""You are a knowledge base query engine. Answer the user's question by
consulting the knowledge base below.

## How to Answer

1. Use only the included articles; selection was done by code
2. Treat active claims as current, proposed claims as unconfirmed, and disputed
   claims as conflicts that must be labeled. Cancelled and superseded decisions
   are historical, NEVER current. An explicit withdrawal overrides incidental
   mentions of the old value in other articles; mention the withdrawal.
3. Synthesize a clear answer without inventing missing evidence
4. Cite your sources using [[wikilinks]] (e.g., [[concepts/supabase-auth]])
5. If the bounded context is insufficient, say so honestly

## Knowledge Base

{safe_wiki}

## Retrieval boundary

Selected {selected} of {candidates} index-matched articles. This is a bounded
subset; say when the selected context is insufficient.

## Question

{safe_question}
{file_back_instructions}"""

    from model_client import run_text_prompt

    result = run_text_prompt(prompt, ROOT_DIR, max_turns=15, tools=tuple(tools))
    answer = str(result.get("text") or "") if result.get("ok") else (
        f"Error querying knowledge base: {result.get('error')}"
    )
    filed = None
    file_error = None
    if file_back and result.get("ok"):
        try:
            filed = _file_back_qa(safe_question, answer)
        except ValueError as exc:
            file_error = str(exc)

    # Update state
    increment_state_counter("query_count")

    notices = []
    if wiki_redacted or question_redacted:
        notices.append("Sensitive values were redacted before the model request.")
    if candidates > selected:
        notices.append(f"Retrieval truncated: selected {selected} of {candidates} index-matched articles.")
    if fallback_used:
        notices.append(f"Cross-language index fallback selected {selected} evidence article(s).")
    if filed:
        notices.append(f"Filed derived Q&A: [[{filed}]].")
    elif file_error:
        notices.append(f"Q&A not filed: {file_error}.")
    if notices:
        return "\n".join(notices) + f"\n\n{answer}"
    return answer


def main():
    parser = argparse.ArgumentParser(description="Query the personal knowledge base")
    parser.add_argument("question", help="The question to ask")
    parser.add_argument(
        "--file-back",
        action="store_true",
        help="save a source-validated derived Q&A article",
    )
    args = parser.parse_args()

    print(f"Question: {args.question}")
    print(f"File back: {'yes' if args.file_back else 'no'}")
    print("-" * 60)

    answer = run_query(args.question, file_back=args.file_back)
    print(answer)

if __name__ == "__main__":
    main()
