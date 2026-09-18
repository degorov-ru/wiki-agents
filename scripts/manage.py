"""Status, diagnostics and reversible lifecycle operations for one project."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import re
from datetime import datetime, timezone
from pathlib import Path

from init_project import init, _codex_stop_command
from path_safety import validate_layout
from memory_contract import apply_proposal, article_metadata, validate_article_semantics


TOOL_ROOT = Path(__file__).resolve().parent.parent
PRODUCT_VERSION = "0.2.2"
SCHEMA_VERSION = 1
DISABLED = TOOL_ROOT / ".disabled-by-codex"
MANAGED_PROJECT_FILES = (
    ".cmc-config.json", ".codex/hooks.json", ".gitignore", "ACCESS.md", "AGENTS.md", "CLAUDE.md",
    "AGENT_GUIDE.md", "wiki/index.md", "wiki/log.md", ".cmc/version.json",
)


def _managed_backup_path(rel: str) -> bool:
    return rel in MANAGED_PROJECT_FILES or bool(re.fullmatch(
        r"wiki/(?:concepts|connections|qa)/[\w.-]+\.md", rel
    ))


def _articles(project: Path) -> list[Path]:
    result = []
    for name in ("concepts", "connections", "qa"):
        directory = project / "wiki" / name
        if directory.is_symlink():
            raise ValueError("article directory is a symlink")
        for path in sorted(directory.glob("*.md")):
            if path.is_symlink() or not path.is_file():
                raise ValueError("unsafe article file")
            result.append(path)
    return result


def migrate_articles(project: Path, apply: bool = False) -> dict:
    """Add conservative metadata only; never infer current truth from legacy text."""
    validate_layout(project)
    changes = []
    rejected = []
    for path in _articles(project):
        original = path.read_text(encoding="utf-8")
        fields = article_metadata(original)
        additions = []
        if "kind" not in fields:
            additions.append("kind: hypothesis")
        if "status" not in fields:
            additions.append("status: proposed")
        content = "---\n" + "\n".join(additions) + "\n" + original[4:] if additions else original
        try:
            validate_article_semantics(project, path, content)
        except ValueError as exc:
            rejected.append({"file": str(path.relative_to(project)), "reason": str(exc)})
            continue
        if additions:
            changes.append({"path": str(path.relative_to(project)), "content": content})
    result = {"status": "PREVIEW" if changes else "NOOP",
              "articles": len(changes), "files": [item["path"] for item in changes]}
    if rejected:
        return {**result, "status": "BLOCKED", "rejected": rejected}
    if changes and apply:
        backup = _backup(project)
        apply_proposal(project, {"result": "changes", "changes": changes})
        result.update(status="MIGRATED", backup=str(backup))
    return result


def _json(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError(f"Refusing symlinked JSON: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def status(project: Path) -> dict:
    validate_layout(project)
    state = _json(project / ".cmc" / "state.json")
    version = _json(project / ".cmc" / "version.json")
    engine = _json(TOOL_ROOT / "engine.json")
    pending_dir = project / ".cmc" / "pending"
    pending_claude = list(pending_dir.glob("*.md")) if pending_dir.exists() else []
    pending_codex = list((project / ".cmc").glob("codex-flush-*.md")) if (project / ".cmc").exists() else []
    exhausted = 0
    for receipt in pending_dir.glob("*.retry.json") if pending_dir.exists() else ():
        try:
            exhausted += int(_json(receipt).get("attempts", 0) >= 3)
        except ValueError:
            exhausted += 1
    return {
        "status": "PAUSED" if DISABLED.exists() else (
            "PURGE_PAUSED" if (project / ".cmc" / "purge-paused").exists() else "ACTIVE"
        ),
        "product_version": version.get("product_version"),
        "schema_version": version.get("schema_version"),
        "engine": engine.get("engine"),
        "fallback": engine.get("fallback"),
        "pending": len(pending_claude) + len(pending_codex),
        "pending_claude": len(pending_claude),
        "pending_codex": len(pending_codex),
        "retry_exhausted": exhausted,
        "ingested": len(state.get("ingested", {})),
        "last_lint": state.get("last_lint"),
        "last_success": state.get("last_success"),
        "last_failure": state.get("last_failure"),
    }


def _git_privacy(project: Path) -> tuple[bool, str]:
    probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=project, capture_output=True, text=True)
    if probe.returncode:
        return True, "not a Git worktree"
    tracked = subprocess.run(
        ["git", "ls-files", "--", ".cmc", ".cmc-config.json", "daily", "sources", "wiki", "ACCESS.md"],
        cwd=project, capture_output=True, text=True,
    ).stdout.splitlines()
    if tracked:
        return False, "tracked private memory: " + ", ".join(tracked[:5])
    samples = (".cmc/state.json", ".cmc-config.json", "daily/example.md", "sources/example.md", "wiki/index.md", "ACCESS.md")
    missing = [sample for sample in samples if subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", sample], cwd=project,
    ).returncode]
    return (not missing, "ignored" if not missing else "not ignored: " + ", ".join(missing))


def doctor(project: Path) -> dict:
    checks: list[dict] = []
    for name, ok, fix in (
        ("project", project.is_dir(), "pass an existing --project"),
        ("wiki", (project / "wiki").is_dir(), "run init_project.py"),
        ("engine", bool(_json(TOOL_ROOT / "engine.json").get("engine")), "run install.py with --engine"),
    ):
        checks.append({"name": name, "ok": ok, "fix": None if ok else fix})
    version = _json(project / ".cmc" / "version.json")
    compatible = version.get("schema_version") == SCHEMA_VERSION
    checks.append({"name": "schema", "ok": compatible, "fix": None if compatible else "run manage.py upgrade"})
    hook_ok = False
    try:
        hook_data = _json(project / ".codex" / "hooks.json")
        hook_ok = any(
            hook.get("command") == _codex_stop_command()
            for entry in hook_data.get("hooks", {}).get("Stop", []) if isinstance(entry, dict)
            for hook in entry.get("hooks", []) if isinstance(hook, dict)
        )
    except (AttributeError, ValueError):
        pass
    checks.append({"name": "codex_hook", "ok": hook_ok,
                   "fix": None if hook_ok else "run manage.py upgrade to restore the project Stop hook"})
    purge_ok = not (project / ".cmc" / "purge-paused").exists()
    checks.append({"name": "purge", "ok": purge_ok,
                   "fix": None if purge_ok else "review purge results, then remove .cmc/purge-paused explicitly"})
    checks.append({"name": "codex_hook_trust", "ok": False, "manual": True,
                   "fix": "open this project in Codex, run /hooks, review the exact project hook hash, and trust it"})
    private, detail = _git_privacy(project)
    checks.append({"name": "git_privacy", "ok": private, "detail": detail,
                   "fix": None if private else "untrack memory files and keep generated memory ignored"})
    return {"status": "OK" if all(c["ok"] for c in checks) else "PARTIAL", "checks": checks}


def pause() -> dict:
    DISABLED.write_text("paused by manage.py\n", encoding="utf-8")
    return {"status": "PAUSED"}


def resume() -> dict:
    DISABLED.unlink(missing_ok=True)
    return {"status": "ACTIVE"}


def _backup(project: Path) -> Path:
    validate_layout(project)
    root = project / ".cmc" / "backups"
    if root.is_symlink():
        raise ValueError("backup directory is a symlink")
    root.mkdir(parents=True, exist_ok=True)
    backup = root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir()
    copied = []
    absent = []
    paths = list(MANAGED_PROJECT_FILES) + [str(path.relative_to(project)) for path in _articles(project)]
    for rel in paths:
        source = project / rel
        if source.is_symlink():
            raise ValueError(f"managed backup source is a symlink: {rel}")
        if source.is_file():
            target = backup / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(rel)
        else:
            absent.append(rel)
    (backup / "manifest.json").write_text(json.dumps({"files": copied, "absent": absent}, indent=2) + "\n", encoding="utf-8")
    return backup


def upgrade(project: Path) -> dict:
    if not (project / "wiki").is_dir():
        raise ValueError("project memory is not initialized; run init_project.py first")
    backup = _backup(project)
    result = init(project)
    version = project / ".cmc" / "version.json"
    version.write_text(json.dumps({"product_version": PRODUCT_VERSION, "schema_version": SCHEMA_VERSION}, indent=2) + "\n", encoding="utf-8")
    return {"status": result["status"], "backup": str(backup), "version": PRODUCT_VERSION, "schema": SCHEMA_VERSION}


def rollback(project: Path, backup: Path) -> dict:
    validate_layout(project)
    backup = backup.resolve()
    if (project / ".cmc" / "backups").is_symlink():
        raise ValueError("backup directory is a symlink")
    backups = (project / ".cmc" / "backups").resolve()
    if not backup.is_relative_to(backups) or not (backup / "manifest.json").is_file():
        raise ValueError("backup is not a managed project backup")
    manifest = _json(backup / "manifest.json")
    files = manifest.get("files", [])
    absent = manifest.get("absent", [])
    if (not isinstance(files, list) or not isinstance(absent, list)
            or any(not isinstance(rel, str) or not _managed_backup_path(rel) for rel in files + absent)
            or len(set(files + absent)) != len(files + absent)):
        raise ValueError("backup manifest contains unmanaged paths")
    restored = []
    for rel in files:
        source = backup / rel
        target = project / rel
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(backup):
            raise ValueError("backup contains unsafe file")
        if target.is_symlink() or not target.resolve().is_relative_to(project.resolve()):
            raise ValueError("managed rollback target is unsafe")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored.append(rel)
    for rel in absent:
        target = project / rel
        if target.is_symlink() or not target.resolve().is_relative_to(project.resolve()):
            raise ValueError("managed rollback target is unsafe")
        if target.is_file():
            target.unlink()
    return {"status": "RESTORED", "files": restored}


def uninstall_project(project: Path) -> dict:
    validate_layout(project)
    hooks_path = project / ".codex" / "hooks.json"
    if not hooks_path.exists():
        return {"status": "NOOP", "memory_preserved": True}
    data = _json(hooks_path)
    stop = data.get("hooks", {}).get("Stop", [])
    kept = []
    for entry in stop if isinstance(stop, list) else []:
        hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
        remaining = [h for h in hooks if not (
            isinstance(h, dict) and "hooks/codex-stop.py" in str(h.get("command", ""))
        )]
        if remaining:
            entry = dict(entry)
            entry["hooks"] = remaining
            kept.append(entry)
    data.setdefault("hooks", {})["Stop"] = kept
    hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"status": "UNINSTALLED", "memory_preserved": True}


def uninstall_claude(settings_path: Path) -> dict:
    if settings_path.is_symlink():
        raise ValueError("Claude settings path is a symlink")
    data = _json(settings_path)
    changed = 0
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude hooks must be an object")
    for event, script in (("SessionStart", "session-start.py"),
                          ("PreCompact", "pre-compact.py"),
                          ("SessionEnd", "session-end.py")):
        kept = []
        for entry in hooks.get(event, []) if isinstance(hooks.get(event, []), list) else []:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            remaining = []
            for hook in entry.get("hooks", []):
                own = isinstance(hook, dict) and f"hooks/{script}" in str(hook.get("command", ""))
                changed += int(own)
                if not own:
                    remaining.append(hook)
            if remaining:
                updated = dict(entry)
                updated["hooks"] = remaining
                kept.append(updated)
        hooks[event] = kept
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"status": "UNINSTALLED" if changed else "NOOP", "removed": changed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "doctor", "pause", "resume", "upgrade", "migrate-articles", "rollback", "uninstall", "uninstall-claude"))
    parser.add_argument("--apply", action="store_true", help="apply the previewed legacy metadata migration")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--settings", type=Path, default=Path.home() / ".claude" / "settings.json")
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    actions = {
        "status": lambda: status(project), "doctor": lambda: doctor(project),
        "pause": pause, "resume": resume, "upgrade": lambda: upgrade(project),
        "migrate-articles": lambda: migrate_articles(project, args.apply),
        "rollback": lambda: rollback(project, args.backup) if args.backup else (_ for _ in ()).throw(ValueError("--backup is required")),
        "uninstall": lambda: uninstall_project(project),
        "uninstall-claude": lambda: uninstall_claude(args.settings.expanduser()),
    }
    try:
        result = actions[args.command]()
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 2 if result.get("status") in {"PARTIAL", "BLOCKED"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
