"""
Lint the knowledge base for structural and semantic health.

Usage:
    uv run python lint.py                    # all checks
    uv run python lint.py --structural-only  # skip LLM checks (faster, cheaper)
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

from config import KNOWLEDGE_DIR, PROJECT_DIR, REPORTS_DIR, ensure_state_dir, now_iso, today_iso
from utils import (
    extract_wikilinks,
    file_hash,
    get_article_word_count,
    list_raw_files,
    list_source_files,
    list_wiki_articles,
    load_state,
    read_all_wiki_content,
    resolve_wikilink,
    save_state,
    wiki_article_exists,
)
from memory_contract import validate_article_semantics
from path_safety import atomic_write_text

ensure_state_dir()
ROOT_DIR = PROJECT_DIR


def check_broken_links() -> list[dict]:
    """Check for [[wikilinks]] that point to non-existent articles."""
    issues = []
    for article in list_wiki_articles():
        content = article.read_text(encoding="utf-8")
        rel = article.relative_to(KNOWLEDGE_DIR)
        for link in extract_wikilinks(content):
            raw_link = link.split("|", 1)[0]
            if raw_link.startswith("daily/"):
                target, _, anchor = raw_link.partition("#")
                daily = PROJECT_DIR / f"{target}.md"
                anchor_found = daily.exists() and (not anchor or re.search(
                    rf"(?m)^###\s+{re.escape(anchor)}(?:\s|\|$)", daily.read_text(encoding="utf-8")
                ))
                if not anchor_found:
                    issues.append({"severity": "error", "check": "broken_source", "file": str(rel), "detail": f"Broken source: [[{link}]]"})
                continue
            if not wiki_article_exists(link, article):
                issues.append({
                    "severity": "error",
                    "check": "broken_link",
                    "file": str(rel),
                    "detail": f"Broken link: [[{link}]] - target does not exist",
                })
    return issues


def check_orphan_pages() -> list[dict]:
    """Check for articles with zero inbound links."""
    articles = list_wiki_articles()
    article_paths = {article.resolve() for article in articles}
    inbound_targets = {
        target
        for article in articles
        for link in extract_wikilinks(article.read_text(encoding="utf-8"))
        if (target := resolve_wikilink(link, article)) in article_paths
    }
    issues = []
    for article in articles:
        rel = article.relative_to(KNOWLEDGE_DIR)
        link_target = str(rel).replace(".md", "").replace("\\", "/")
        if article.resolve() not in inbound_targets:
            issues.append({
                "severity": "warning",
                "check": "orphan_page",
                "file": str(rel),
                "detail": f"Orphan page: no other articles link to [[{link_target}]]",
            })
    return issues


def check_orphan_sources() -> list[dict]:
    """Check for daily logs that haven't been compiled yet."""
    state = load_state()
    ingested = state.get("ingested", {})
    issues = []
    for log_path in list_raw_files() + list_source_files():
        rel = log_path.name if log_path.parent.name == "daily" else f"sources/{log_path.name}"
        if rel not in ingested:
            issues.append({
                "severity": "warning",
                "check": "orphan_source",
                "file": rel if rel.startswith("sources/") else f"daily/{rel}",
                "detail": f"Uncompiled daily log: {log_path.name} has not been ingested",
            })
    return issues


def check_stale_articles() -> list[dict]:
    """Check if source daily logs have changed since compilation."""
    state = load_state()
    ingested = state.get("ingested", {})
    issues = []
    for log_path in list_raw_files() + list_source_files():
        rel = log_path.name if log_path.parent.name == "daily" else f"sources/{log_path.name}"
        if rel in ingested:
            stored_hash = ingested[rel].get("hash", "")
            current_hash = file_hash(log_path)
            if stored_hash != current_hash:
                issues.append({
                    "severity": "warning",
                    "check": "stale_article",
                    "file": rel if rel.startswith("sources/") else f"daily/{rel}",
                    "detail": f"Stale: {rel} has changed since last compilation",
                })
    return issues


def check_missing_backlinks() -> list[dict]:
    """Check for asymmetric links: A links to B but B doesn't link to A."""
    issues = []
    for article in list_wiki_articles():
        content = article.read_text(encoding="utf-8")
        rel = article.relative_to(KNOWLEDGE_DIR)
        source_link = str(rel).replace(".md", "").replace("\\", "/")

        for link in extract_wikilinks(content):
            if link.split("|", 1)[0].startswith("daily/"):
                continue
            target_path = resolve_wikilink(link, article)
            if target_path is not None:
                target_content = target_path.read_text(encoding="utf-8")
                links_back = any(
                    resolve_wikilink(backlink, target_path) == article.resolve()
                    for backlink in extract_wikilinks(target_content)
                )
                if not links_back:
                    issues.append({
                        "severity": "suggestion",
                        "check": "missing_backlink",
                        "file": str(rel),
                        "detail": f"[[{source_link}]] links to [[{link}]] but not vice versa",
                        "auto_fixable": True,
                    })
    return issues


def check_sparse_articles() -> list[dict]:
    return []


def check_article_schema() -> list[dict]:
    issues = []
    for article in list_wiki_articles():
        try:
            validate_article_semantics(PROJECT_DIR, article, article.read_text(encoding="utf-8"))
        except ValueError as exc:
            issues.append({"severity": "error", "check": "schema", "file": str(article.relative_to(KNOWLEDGE_DIR)), "detail": str(exc)})
    return issues


def check_index() -> list[dict]:
    index_path = KNOWLEDGE_DIR / "index.md"
    if not index_path.is_file() or not index_path.resolve().is_relative_to(KNOWLEDGE_DIR.resolve()):
        return [{"severity": "error", "check": "index", "file": "index.md", "detail": "Missing or unsafe wiki index"}]
    indexed = set(re.findall(r"\[\[((?:concepts|connections|qa)/[^\]|#]+)", index_path.read_text(encoding="utf-8")))
    actual = {str(path.relative_to(KNOWLEDGE_DIR).with_suffix("")) for path in list_wiki_articles()}
    return [
        {"severity": "error", "check": "index", "file": "index.md", "detail": f"Index mismatch: {kind} [[{item}]]"}
        for kind, items in (("missing", actual - indexed), ("stale", indexed - actual)) for item in sorted(items)
    ]


async def check_contradictions() -> list[dict]:
    """Use LLM to detect contradictions across articles."""
    wiki_content = read_all_wiki_content()

    prompt = f"""Review this knowledge base for contradictions, inconsistencies, or
conflicting claims across articles.

## Knowledge Base

{wiki_content}

## Instructions

Look for:
- Direct contradictions (article A says X, article B says not-X)
- Inconsistent recommendations (different articles recommend conflicting approaches)
- Outdated information that conflicts with newer entries

For each issue found, output EXACTLY one line in this format:
CONTRADICTION: [file1] vs [file2] - description of the conflict
INCONSISTENCY: [file] - description of the inconsistency

If no issues found, output exactly: NO_ISSUES

Do NOT output anything else - no preamble, no explanation, just the formatted lines."""

    from model_client import run_text_prompt

    result = run_text_prompt(prompt, ROOT_DIR, max_turns=2)
    if not result.get("ok"):
        return [{
            "severity": "error",
            "check": "contradiction",
            "file": "(system)",
            "detail": f"Memory engine check failed: {result.get('error')}",
        }]
    response = str(result.get("text") or "")

    issues = []
    if "NO_ISSUES" not in response:
        for line in response.strip().split("\n"):
            line = line.strip()
            if line.startswith("CONTRADICTION:") or line.startswith("INCONSISTENCY:"):
                issues.append({
                    "severity": "warning",
                    "check": "contradiction",
                    "file": "(cross-article)",
                    "detail": line,
                })

    return issues


def generate_report(all_issues: list[dict]) -> str:
    """Generate a markdown lint report."""
    errors = [i for i in all_issues if i["severity"] == "error"]
    warnings = [i for i in all_issues if i["severity"] == "warning"]
    suggestions = [i for i in all_issues if i["severity"] == "suggestion"]

    lines = [
        f"# Lint Report - {today_iso()}",
        "",
        f"**Total issues:** {len(all_issues)}",
        f"- Errors: {len(errors)}",
        f"- Warnings: {len(warnings)}",
        f"- Suggestions: {len(suggestions)}",
        "",
    ]

    for severity, issues, marker in [
        ("Errors", errors, "x"),
        ("Warnings", warnings, "!"),
        ("Suggestions", suggestions, "?"),
    ]:
        if issues:
            lines.append(f"## {severity}")
            lines.append("")
            for issue in issues:
                fixable = " (auto-fixable)" if issue.get("auto_fixable") else ""
                lines.append(f"- **[{marker}]** `{issue['file']}` - {issue['detail']}{fixable}")
            lines.append("")

    if not all_issues:
        lines.append("All checks passed. Knowledge base is healthy.")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Lint the knowledge base")
    parser.add_argument(
        "--structural-only",
        action="store_true",
        help="Skip LLM-based checks (contradictions) - faster and free",
    )
    args = parser.parse_args()

    if (PROJECT_DIR / ".cmc" / "purge-paused").exists():
        print("Memory lint blocked: controlled purge is incomplete.")
        return 1

    print("Running knowledge base lint checks...")
    all_issues: list[dict] = []

    # Structural checks (free, instant)
    checks = [
        ("Broken links", check_broken_links),
        ("Orphan pages", check_orphan_pages),
        ("Orphan sources", check_orphan_sources),
        ("Stale articles", check_stale_articles),
        ("Article schema", check_article_schema),
        ("Index", check_index),
    ]

    for name, check_fn in checks:
        print(f"  Checking: {name}...")
        issues = check_fn()
        all_issues.extend(issues)
        print(f"    Found {len(issues)} issue(s)")

    # LLM check (costs money)
    if not args.structural_only:
        print("  Checking: Contradictions (LLM)...")
        issues = asyncio.run(check_contradictions())
        all_issues.extend(issues)
        print(f"    Found {len(issues)} issue(s)")
    else:
        print("  Skipping: Contradictions (--structural-only)")

    # Generate and save report
    report = generate_report(all_issues)
    if REPORTS_DIR.is_symlink():
        print("Memory lint blocked: reports directory is a symlink.")
        return 1
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if not REPORTS_DIR.resolve().is_relative_to((PROJECT_DIR / ".cmc").resolve()):
        print("Memory lint blocked: reports directory escapes project state.")
        return 1
    report_path = REPORTS_DIR / f"lint-{today_iso()}.md"
    atomic_write_text(report_path, report)
    print(f"\nReport saved to: {report_path}")

    # Update state
    state = load_state()
    state["last_lint"] = now_iso()
    save_state(state)

    # Summary
    errors = sum(1 for i in all_issues if i["severity"] == "error")
    warnings = sum(1 for i in all_issues if i["severity"] == "warning")
    suggestions = sum(1 for i in all_issues if i["severity"] == "suggestion")
    print(f"\nResults: {errors} errors, {warnings} warnings, {suggestions} suggestions")

    if errors > 0:
        print("\nErrors found - knowledge base needs attention!")
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
