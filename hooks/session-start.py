"""
SessionStart hook - inject the current project's access registry and wiki index.

What it does:

  1. Determines the project directory ($CLAUDE_PROJECT_DIR or cwd).
  2. If the project has a legacy ai-memory/ folder but no wiki/, migrates
     it to wiki/ before reading.
  3. If wiki/ is missing, initializes the project memory scaffold.
  4. Reads ACCESS.md when present, then wiki/index.md and the most recent daily
     log, budgets them against MAX_TOTAL_CHARS, and outputs additionalContext.

The hook itself does NO API calls and finishes in well under a second.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Recursion guard: skip when running inside an Agent SDK session spawned by
# flush.py / compile.py — otherwise inner sessions re-trigger compilation.
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

_TOOL_ROOT = Path(__file__).resolve().parent.parent
if (_TOOL_ROOT / ".disabled-by-codex").exists() or os.environ.get("CMC_DISABLED") == "1":
    sys.exit(0)


def _safe_dir(path: Path, parent: Path) -> Path | None:
    if path.is_symlink() or not path.is_dir():
        return None
    resolved = path.resolve()
    return resolved if resolved.is_relative_to(parent.resolve()) else None


def _safe_file(path: Path, root: Path) -> Path | None:
    safe_root = _safe_dir(root, root.parent)
    if safe_root is None or path.is_symlink() or not path.is_file():
        return None
    resolved = path.resolve()
    return resolved if resolved.is_relative_to(safe_root) else None


def _config_int(name: str, default: int) -> int:
    val = os.environ.get(f"CMC_{name}")
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return default


def _load_project_config(project_dir: Path) -> dict:
    cfg_path = project_dir / ".cmc-config.json"
    if _safe_file(cfg_path, project_dir) is None:
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _budget(project_dir: Path) -> tuple[int, int, int]:
    cfg = _load_project_config(project_dir)
    max_index = int(cfg.get("max_index_chars", _config_int("MAX_INDEX_CHARS", 10_000)))
    max_log_lines = int(cfg.get("max_log_lines", _config_int("MAX_LOG_LINES", 30)))
    max_total = int(cfg.get("max_total_chars", _config_int("MAX_TOTAL_CHARS", 15_000)))
    return max_index, max_log_lines, max_total


def _read_recent_daily(daily_dir: Path, max_lines: int) -> str:
    if _safe_dir(daily_dir, daily_dir.parent) is None:
        return "(no recent daily log)"
    logs = [path for path in sorted(daily_dir.glob("*.md"))
            if _safe_file(path, daily_dir) is not None]
    if logs:
        lines = logs[-1].read_text(encoding="utf-8").splitlines()
        tail = lines[-max_lines:] if len(lines) > max_lines else lines
        return "\n".join(tail)
    return "(no recent daily log)"


def _maybe_trigger_compilation(project_dir: Path) -> None:
    """Wake-on-resume: spawn the compile trigger as a detached background job.

    The SessionStart hook must return quickly (it blocks chat start), so we
    just fire-and-forget. maybe_compile.py decides for itself whether the
    hour threshold and pending-work checks are satisfied.
    """
    tool_root = Path(__file__).resolve().parent.parent
    script = tool_root / "scripts" / "maybe_compile.py"
    if not script.exists():
        return

    cmd = [
        "uv", "run",
        "--directory", str(tool_root),
        "python", str(script),
    ]

    env = os.environ.copy()
    env["CMC_PROJECT_DIR"] = str(project_dir)

    kwargs: dict = {"env": env, "cwd": str(tool_root)}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception:
        pass


def _maybe_recover_pending(project_dir: Path) -> None:
    """Flush raw-capture buffers orphaned by sessions that ended without a
    SessionEnd (abrupt close / crash). recover_orphans spawns detached flush.py
    jobs (CLAUDE_INVOKED_BY set), so it returns immediately and cannot recurse."""
    try:
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from pending import recover_orphans

        recover_orphans(project_dir)
    except Exception:
        pass


def _maybe_migrate_ai_memory(project_dir: Path) -> str | None:
    """If ai-memory/ exists and wiki/ doesn't, run the migration script."""
    ai_memory = project_dir / "ai-memory"
    wiki = project_dir / "wiki"
    if ai_memory.is_symlink() or wiki.is_symlink():
        return "(migration refused: managed path is a symlink)"
    if not ai_memory.is_dir() or wiki.exists():
        return None

    tool_root = Path(__file__).resolve().parent.parent
    migrate_script = tool_root / "scripts" / "migrate_ai_memory.py"
    if not migrate_script.exists():
        return None

    try:
        result = subprocess.run(
            ["uv", "run", "--directory", str(tool_root), "python", str(migrate_script), str(project_dir)],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0:
            return "(legacy ai-memory/ was just migrated to wiki/)"
        return f"(ai-memory migration failed: {result.stderr.strip()[:200]})"
    except Exception as e:
        return f"(ai-memory migration error: {e})"


def _auto_init_roots() -> list[Path]:
    """Directories whose CHILDREN are allowed to get an auto-created wiki/.

    Configured via auto-init-roots.json in the tool root (a JSON array of
    paths) or the CMC_AUTO_INIT_ROOTS env var (colon-separated). Projects
    outside these roots can still opt in manually via scripts/init_project.py —
    an existing wiki/ is always honored regardless of this list.
    """
    env_val = os.environ.get("CMC_AUTO_INIT_ROOTS")
    if env_val:
        return [Path(p).expanduser().resolve() for p in env_val.split(":") if p.strip()]

    cfg_path = Path(__file__).resolve().parent.parent / "auto-init-roots.json"
    try:
        roots = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(roots, list):
            return [Path(p).expanduser().resolve() for p in roots]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return []


def _allowed_to_auto_init(project_dir: Path) -> bool:
    for root in _auto_init_roots():
        if project_dir != root and root in project_dir.parents:
            return True
    return False


def _maybe_init_project(project_dir: Path) -> str | None:
    """Create the memory scaffold for projects that do not have wiki/ yet."""
    if (project_dir / "wiki").is_symlink():
        return "(memory refused: wiki is a symlink)"
    if (project_dir / "wiki").exists():
        return None

    # Avoid accidentally creating vaults in broad shell locations.
    if project_dir == project_dir.home() or project_dir == Path("/"):
        return "(wiki not initialized in home/root directory)"

    # Auto-init is restricted to children of the configured roots; everywhere
    # else memory stays opt-in (run scripts/init_project.py manually).
    if not _allowed_to_auto_init(project_dir):
        return None

    tool_root = Path(__file__).resolve().parent.parent
    init_script = tool_root / "scripts" / "init_project.py"
    if not init_script.exists():
        return "(wiki missing and init_project.py was not found)"

    try:
        result = subprocess.run(
            ["uv", "run", "--directory", str(tool_root), "python", str(init_script), str(project_dir)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode == 0:
            return "(project memory was initialized automatically)"
        return f"(project memory init failed: {result.stderr.strip()[:200]})"
    except Exception as e:
        return f"(project memory init error: {e})"


def _discover_project_dir() -> Path:
    val = os.environ.get("CLAUDE_PROJECT_DIR")
    if val:
        return Path(val).expanduser().resolve()
    return Path(os.getcwd()).resolve()


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n…(truncated)"


def build_context(project_dir: Path) -> str:
    today = datetime.now(timezone.utc).astimezone()
    max_index, max_log_lines, max_total = _budget(project_dir)

    parts: list[str] = [
        f"## Project\n{project_dir}",
        f"## Today\n{today.strftime('%A, %B %d, %Y')}",
    ]

    if (project_dir / ".cmc" / "purge-paused").exists():
        parts.append("## Memory\n\n(paused: controlled purge is incomplete)")
        return "\n\n---\n\n".join(parts)

    migration_note = _maybe_migrate_ai_memory(project_dir)
    if migration_note:
        parts.append(f"## Migration\n{migration_note}")

    init_note = _maybe_init_project(project_dir)
    if init_note:
        parts.append(f"## Memory Init\n{init_note}")

    wiki_dir = project_dir / "wiki"
    daily_dir = project_dir / "daily"
    index_file = wiki_dir / "index.md"
    access_file = project_dir / "ACCESS.md"

    if _safe_file(access_file, project_dir) is not None:
        access = access_file.read_text(encoding="utf-8")
        parts.append(f"## Access Registry\n\n{_truncate(access, 4_000)}")

    safe_wiki = _safe_dir(wiki_dir, project_dir)
    if safe_wiki is None:
        parts.append(
            "## Wiki\n\n(no wiki: memory is opt-in for this directory — "
            "run scripts/init_project.py from claude-memory-compiler to enable)"
        )
    else:
        if _safe_file(index_file, wiki_dir) is not None:
            idx = index_file.read_text(encoding="utf-8")
            parts.append(f"## Wiki Index\n\n{_truncate(idx, max_index)}")
        else:
            parts.append("## Wiki Index\n\n(empty - no articles compiled yet)")

        recent = _read_recent_daily(daily_dir, max_log_lines)
        parts.append(f"## Recent Daily Log\n\n{recent}")

        # Recover raw-capture buffers orphaned by an abrupt close (no SessionEnd):
        # flush them through Grok now. Detached, does not block startup.
        _maybe_recover_pending(project_dir)

        # Wake-on-resume: fire-and-forget compile if past the configured
        # hour and any daily log is pending. Detached, does not block startup.
        _maybe_trigger_compilation(project_dir)

    context = "\n\n---\n\n".join(parts)
    return _truncate(context, max_total)


def main() -> None:
    project_dir = _discover_project_dir()
    context = build_context(project_dir)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
