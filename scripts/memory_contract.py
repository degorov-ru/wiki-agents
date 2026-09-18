"""Small, deterministic boundary around model-produced memory changes."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path
from path_safety import atomic_write_text


ALLOWED_KINDS = {"fact", "decision", "procedure", "hypothesis", "preference"}
ALLOWED_STATUS = {"proposed", "active", "disputed", "superseded", "cancelled"}
ALLOWED_PATHS = {"wiki/index.md", "wiki/log.md"}
SECRET_RE = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)"
    r"|authorization\s*:\s*bearer\s+\S+"
    r"|(?:api\s*[_-]?\s*key|token|password|secret)\s*[:=]\s*\S+"
    r"|\b(?:sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,}|xox[baprs]-[A-Za-z0-9_-]{10,})\b",
    re.IGNORECASE | re.DOTALL,
)


def redact_sensitive(text: str) -> tuple[str, bool]:
    """Remove common credential assignments before a provider boundary."""
    redacted, count = SECRET_RE.subn("[REDACTED_SECRET]", text)
    return redacted, bool(count)


def parse_proposal(text: str) -> dict:
    try:
        proposal = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("model response is not JSON") from exc
    if not isinstance(proposal, dict) or proposal.get("result") not in {"noop", "changes"}:
        raise ValueError("model response must declare result=noop or result=changes")
    changes = proposal.get("changes", [])
    if proposal["result"] == "noop" and changes:
        raise ValueError("noop cannot contain changes")
    if proposal["result"] == "noop" and not str(proposal.get("reason") or "").strip():
        raise ValueError("noop needs a reason")
    if proposal["result"] == "changes" and (not isinstance(changes, list) or not changes):
        raise ValueError("changes must be a non-empty list")
    return proposal


def _allowed_path(project_dir: Path, raw: object) -> Path:
    if not isinstance(raw, str) or raw.startswith("/") or ".." in Path(raw).parts:
        raise ValueError("unsafe output path")
    if raw not in ALLOWED_PATHS and not re.fullmatch(r"wiki/(?:concepts|connections|qa)/[\w.-]+\.md", raw):
        raise ValueError("output path outside managed wiki")
    lexical = project_dir / raw
    current = project_dir
    for part in Path(raw).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("output path crosses symlink")
    target = lexical.resolve()
    wiki_path = project_dir / "wiki"
    if wiki_path.is_symlink():
        raise ValueError("managed wiki root is a symlink")
    wiki = wiki_path.resolve()
    if not target.is_relative_to(wiki):
        raise ValueError("output path escapes wiki")
    return target


def _regular_project_file(project_dir: Path, raw: Path) -> Path | None:
    """Resolve a source while rejecting symlinks at every managed component."""
    if raw.is_absolute() or ".." in raw.parts:
        return None
    current = project_dir
    for part in raw.parts:
        current /= part
        if current.is_symlink():
            return None
    resolved = (project_dir / raw).resolve()
    return resolved if resolved.is_relative_to(project_dir) and resolved.is_file() else None


def validate_article(content: str) -> None:
    fields, list_fields = _frontmatter(content)
    for name in ("title", "kind", "status", "updated", "sources"):
        if not fields.get(name) or not any(fields[name]):
            raise ValueError(f"article missing {name}")
    if fields["kind"][0] not in ALLOWED_KINDS or fields["status"][0] not in ALLOWED_STATUS:
        raise ValueError("article kind or status is invalid")
    if not all(source.startswith(("daily/", "sources/")) for source in fields["sources"]):
        raise ValueError("every article source must name daily/ or sources/")
    if "scope" in fields and not any(scope.strip() for scope in fields["scope"]):
        raise ValueError("article scope must be a non-empty string or list")
    if fields["status"][0] == "superseded" and not fields.get("superseded_by"):
        raise ValueError("superseded article needs superseded_by")
    if fields["status"][0] == "cancelled" and fields.get("superseded_by"):
        raise ValueError("cancelled article cannot name superseded_by")
    for name in ("supersedes", "superseded_by"):
        for link in fields.get(name, []):
            if link and not _is_wikilink(link):
                raise ValueError(f"{name} must contain wikilinks")
    if "derived_from" in fields:
        if "derived_from" not in list_fields or not fields["derived_from"]:
            raise ValueError("derived_from must be a non-empty list")
        if not all(_is_wikilink(link) for link in fields["derived_from"]):
            raise ValueError("derived_from must contain wikilinks")


def _frontmatter(content: str) -> tuple[dict[str, list[str]], set[str]]:
    if not content.startswith("---\n"):
        raise ValueError("article needs YAML frontmatter")
    end = content.find("\n---", 4)
    if end < 0:
        raise ValueError("article frontmatter is incomplete")
    fields: dict[str, list[str]] = {}
    list_fields: set[str] = set()
    current = ""
    for line in content[4:end].splitlines():
        if line.startswith("  - ") and current:
            if fields[current] and current not in list_fields:
                raise ValueError(f"mixed scalar and list frontmatter field: {current}")
            fields.setdefault(current, []).append(line[4:].strip().strip('"'))
            list_fields.add(current)
        elif line and not line.startswith(" ") and ":" in line:
            current, value = line.split(":", 1)
            current = current.strip()
            if not current or current in fields:
                raise ValueError(f"duplicate or invalid frontmatter field: {current}")
            value = value.strip().strip('"')
            if value:
                fields.setdefault(current, []).append(value)
            else:
                fields.setdefault(current, [])
        elif line.strip():
            raise ValueError("invalid frontmatter line")
    return fields, list_fields


def article_metadata(content: str) -> dict[str, list[str]]:
    """Return normalized frontmatter fields for trusted code workflows."""
    fields, _ = _frontmatter(content)
    return fields


def _is_wikilink(value: str) -> bool:
    return bool(re.fullmatch(r"\[\[(?:concepts|connections|qa)/[\w.-]+\]\]", value))


def _article_target(project_dir: Path, link: str) -> Path:
    return project_dir / "wiki" / f"{link[2:-2]}.md"


def _validate_semantics(project_dir: Path, target: Path, content: str,
                        pending: dict[Path, str]) -> None:
    fields, _ = _frontmatter(content)
    for source in fields["sources"]:
        source_path, marker, anchor = source.partition("#")
        if not source_path.startswith(("daily/", "sources/")):
            raise ValueError(f"unsafe source path: {source_path}")
        raw_source = Path(source_path)
        if raw_source.is_absolute() or ".." in raw_source.parts:
            raise ValueError(f"unsafe source path: {source_path}")
        source_file = _regular_project_file(project_dir, raw_source)
        if source_file is None:
            raise ValueError(f"source does not exist: {source_path}")
        if marker:
            source_text = source_file.read_text(encoding="utf-8")
            if source_path.startswith("daily/"):
                found = re.search(rf"(?m)^###\s+{re.escape(anchor)}(?:\s|\|$)", source_text)
            else:
                found = anchor in source_text
            if not found:
                raise ValueError(f"source anchor does not exist: {source}")
    if len(fields["sources"]) != len(set(fields["sources"])):
        raise ValueError("duplicate source event")

    def article_for(link: str) -> str:
        candidate = _article_target(project_dir, link)
        candidate_content = pending.get(candidate)
        if candidate_content is None:
            safe_candidate = _regular_project_file(
                project_dir, candidate.relative_to(project_dir)
            )
            if safe_candidate is not None:
                candidate_content = safe_candidate.read_text(encoding="utf-8")
        if candidate_content is None:
            raise ValueError(f"referenced article does not exist: {link}")
        return candidate_content

    for name in ("supersedes", "superseded_by"):
        for link in fields.get(name, []):
            article_for(link)

    if target.parent.name == "qa":
        derived = fields.get("derived_from", [])
        if not derived:
            raise ValueError("QA article needs derived_from")
        for link in derived:
            source_fields, _ = _frontmatter(article_for(link))
            if source_fields["status"][0] != "active":
                raise ValueError(f"QA derived source is stale: {link}")


def validate_article_semantics(project_dir: Path, target: Path, content: str) -> None:
    """Validate provenance and article references already present on disk."""
    validate_article(content)
    _validate_semantics(project_dir.resolve(), target.resolve(), content, {})


def _hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _recover_pending_commit_locked(project_dir: Path) -> int:
    """Finish a validated staged commit, refusing to replace a user edit."""
    project_dir = project_dir.resolve()
    state_dir = project_dir / ".cmc"
    journal = state_dir / "pending-commit.json"
    if not journal.exists():
        return 0
    if state_dir.is_symlink() or journal.is_symlink():
        raise ValueError("unsafe commit journal")
    data = json.loads(journal.read_text(encoding="utf-8"))
    transaction_id = data.get("transaction")
    files = data.get("files")
    if not isinstance(transaction_id, str) or not re.fullmatch(r"[a-f0-9]{32}", transaction_id):
        raise ValueError("invalid commit transaction")
    if not isinstance(files, list):
        raise ValueError("invalid commit journal")
    transactions = state_dir / "transactions"
    transaction = transactions / transaction_id
    if transactions.is_symlink() or transaction.is_symlink() or not transaction.resolve().is_relative_to(transactions.resolve()):
        raise ValueError("unsafe commit transaction")
    completed = 0
    seen_staged: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("invalid commit entry")
        target = _allowed_path(project_dir, entry["path"])
        staged_name = entry.get("staged")
        if not isinstance(staged_name, str) or not re.fullmatch(r"\d+", staged_name) or staged_name in seen_staged:
            raise ValueError("invalid staged file")
        seen_staged.add(staged_name)
        staged = transaction / staged_name
        if staged.is_symlink() or not staged.resolve().is_relative_to(transaction.resolve()):
            raise ValueError("unsafe staged file")
        for field in ("new_hash", "old_hash"):
            value = entry.get(field)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)):
                raise ValueError("invalid commit hash")
        current = _hash(target)
        if current == entry["new_hash"]:
            completed += 1
            continue
        if current != entry["old_hash"]:
            raise ValueError(f"managed file changed during commit: {entry['path']}")
        if not staged.is_file() or _hash(staged) != entry["new_hash"]:
            raise ValueError(f"staged file missing or changed: {entry['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        completed += 1
    journal.unlink()
    shutil.rmtree(transaction, ignore_errors=True)
    return completed


def recover_pending_commit(project_dir: Path) -> int:
    """Recover under the same lock used for normal publication."""
    project_dir = project_dir.resolve()
    state_dir = project_dir / ".cmc"
    if state_dir.is_symlink():
        raise ValueError("unsafe state directory")
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "wiki.lock"
    if lock_path.is_symlink():
        raise ValueError("unsafe wiki lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        if os.name != "nt":
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _recover_pending_commit_locked(project_dir)
        finally:
            if os.name != "nt":
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def apply_proposal(project_dir: Path, proposal: dict) -> int:
    """Validate then atomically replace only managed wiki files."""
    project_dir = project_dir.resolve()
    changes = proposal.get("changes", [])
    targets: list[tuple[Path, str]] = []
    expected_hashes: dict[Path, str | None] = {}
    seen: set[Path] = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("invalid change")
        target = _allowed_path(project_dir, change.get("path"))
        content = change.get("content")
        if target in seen or not isinstance(content, str):
            raise ValueError("duplicate path or missing content")
        seen.add(target)
        expected_hashes[target] = _hash(target)
        if target.parent.name in {"concepts", "connections", "qa"}:
            validate_article(content)
            if target.is_file():
                old_fields, _ = _frontmatter(target.read_text(encoding="utf-8"))
                new_fields, _ = _frontmatter(content)
                if not set(old_fields.get("sources", ())).issubset(new_fields.get("sources", ())):
                    raise ValueError("article update cannot drop provenance")
                old_status = old_fields.get("status", [""])[0]
                new_status = new_fields.get("status", [""])[0]
                if old_status in {"superseded", "cancelled"} and new_status != old_status:
                    raise ValueError("terminal article status cannot be reactivated")
        elif target.name == "index.md" and target.is_file():
            old_links = set(re.findall(r"\[\[((?:concepts|connections|qa)/[\w.-]+)", target.read_text(encoding="utf-8")))
            new_links = set(re.findall(r"\[\[((?:concepts|connections|qa)/[\w.-]+)", content))
            if not old_links.issubset(new_links):
                raise ValueError("index update cannot drop existing articles")
        elif target.name == "log.md" and target.is_file():
            old = target.read_text(encoding="utf-8")
            if not content.startswith(old.rstrip()):
                content = old.rstrip() + "\n\n" + content.strip() + "\n"
        targets.append((target, content))
    if not targets:
        return 0
    explicit_count = len(targets)
    article_targets = [(target, content) for target, content in targets
                       if target.parent.name in {"concepts", "connections", "qa"}]
    target_names = {target.name for target, _ in targets if target.parent == project_dir / "wiki"}
    if article_targets:
        index_target = _allowed_path(project_dir, "wiki/index.md")
        index = next((content for target, content in targets if target == index_target), None)
        if index is None:
            index = index_target.read_text(encoding="utf-8") if index_target.is_file() else (
                "# Wiki Index\n\n| Article | Summary | Source | Updated |\n|---|---|---|---|\n"
            )
        for target, content in article_targets:
            rel = str(target.relative_to(project_dir / "wiki").with_suffix(""))
            if f"[[{rel}]]" in index:
                continue
            fields, _ = _frontmatter(content)
            title = fields["title"][0].replace("|", " ")
            index = index.rstrip() + (
                f"\n| [[{rel}]] | {title} | {fields['sources'][0]} | {fields['updated'][0]} |\n"
            )
        if "index.md" in target_names:
            targets = [(target, index if target == index_target else content)
                       for target, content in targets]
        else:
            targets.append((index_target, index))
            expected_hashes[index_target] = _hash(index_target)

        log_target = _allowed_path(project_dir, "wiki/log.md")
        links = ", ".join(
            f"[[{target.relative_to(project_dir / 'wiki').with_suffix('')}]]"
            for target, _ in article_targets
        )
        fragment = f"## memory contract | articles published\n- Articles: {links}\n"
        log = next((content for target, content in targets if target == log_target), None)
        if log is None:
            log = log_target.read_text(encoding="utf-8") if log_target.is_file() else "# Log\n"
        if fragment.strip() not in log:
            log = log.rstrip() + "\n\n" + fragment
        if "log.md" in target_names:
            targets = [(target, log if target == log_target else content)
                       for target, content in targets]
        else:
            targets.append((log_target, log))
            expected_hashes[log_target] = _hash(log_target)
    pending = dict(targets)
    for target, content in targets:
        if target.name != "index.md" or target.parent != project_dir / "wiki":
            continue
        for rel in re.findall(r"\[\[((?:concepts|connections|qa)/[\w.-]+)", content):
            article = project_dir / "wiki" / f"{rel}.md"
            if article not in pending and _regular_project_file(
                project_dir, article.relative_to(project_dir)
            ) is None:
                raise ValueError(f"index references missing article: {rel}")
    for target, content in targets:
        if target.parent.name in {"concepts", "connections", "qa"}:
            _validate_semantics(project_dir, target, content, pending)
    state_dir = project_dir / ".cmc"
    if state_dir.is_symlink():
        raise ValueError("unsafe state directory")
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "wiki.lock"
    if lock_path.is_symlink() or (state_dir / "transactions").is_symlink():
        raise ValueError("unsafe wiki runtime path")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        if os.name != "nt":
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if (state_dir / "purge-paused").exists():
                raise ValueError("memory publication is paused for purge")
            _recover_pending_commit_locked(project_dir)
            for target, expected in expected_hashes.items():
                if _hash(target) != expected:
                    raise ValueError("managed wiki changed during proposal validation; retry")
            for target, content in targets:
                if target.parent.name in {"concepts", "connections", "qa"}:
                    _validate_semantics(project_dir, target, content, pending)
            transaction_id = uuid.uuid4().hex
            transaction = state_dir / "transactions" / transaction_id
            transaction.mkdir(parents=True)
            entries = []
            for index, (target, content) in enumerate(targets):
                staged = transaction / str(index)
                staged.write_text(content, encoding="utf-8")
                entries.append({"path": str(target.relative_to(project_dir)), "staged": str(index),
                                "old_hash": _hash(target), "new_hash": _hash(staged)})
            journal = state_dir / "pending-commit.json"
            atomic_write_text(journal, json.dumps({"transaction": transaction_id, "files": entries}, indent=2) + "\n")
            _recover_pending_commit_locked(project_dir)
            return explicit_count
        finally:
            if os.name != "nt":
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
