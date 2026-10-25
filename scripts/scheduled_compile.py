"""Check opted-in project queues independently of conversations (macOS launchd)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
LABEL = "local.wiki-agents.compile"
PROJECT_ROOTS = (Path.home() / "AI Projects", Path.home() / "Local Sites")
SKIP_DIRS = {".git", ".hg", ".svn", ".worktrees", "worktrees", "node_modules",
             "vendor", ".venv", "venv", "__pycache__", ".cache", "cache", "caches",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".cmc"}


def discover_projects(roots: tuple[Path, ...] | None = None) -> list[Path]:
    """Find initialized projects without following links or leaving configured roots."""
    found = []
    for root in PROJECT_ROOTS if roots is None else roots:
        if root.is_symlink() or not root.is_dir():
            continue
        pending = [root]
        while pending:
            directory = pending.pop()
            agents = directory / "AGENTS.md"
            if not agents.is_symlink() and agents.is_file() and all(
                not (directory / name).is_symlink() and (directory / name).is_dir()
                for name in ("wiki", "daily")
            ):
                found.append(directory.resolve())
                continue
            try:
                with os.scandir(directory) as entries:
                    pending.extend(Path(entry.path) for entry in entries
                                   if entry.name.lower() not in SKIP_DIRS
                                   and entry.is_dir(follow_symlinks=False))
            except OSError:
                continue
    return sorted(set(found))


def install_schedule(projects: list[Path]) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("Automatic schedule currently requires macOS")
    path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if path.is_symlink():
        raise ValueError("Schedule path is a symlink")
    # Keep already registered projects when the installer adds another project.
    if path.exists():
        existing = plistlib.loads(path.read_bytes()).get("ProgramArguments", [])
        for i, arg in enumerate(existing[:-1]):
            if arg == "--project":
                projects.append(Path(existing[i + 1]))
    projects = list(dict.fromkeys(p.expanduser().resolve() for p in projects))
    command = [sys.executable, "-u", str(TOOL_ROOT / "scripts/scheduled_compile.py")]
    for project in projects:
        command += ["--project", str(project)]
    data = {"Label": LABEL, "ProgramArguments": command, "StartInterval": 300,
            "RunAtLoad": True, "WorkingDirectory": str(TOOL_ROOT),
            "EnvironmentVariables": {"PATH": ":".join([str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])},
            "StandardOutPath": str(TOOL_ROOT / "scheduled-compile.log"),
            "StandardErrorPath": str(TOOL_ROOT / "scheduled-compile.log")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(data))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    return path


def _live_compile_lock(projects: list[Path]) -> str | None:
    for project in projects:
        lock = project / ".cmc/compile.lock"
        if lock.parent.is_symlink() or lock.is_symlink():
            return f"{lock}: cannot verify symlinked lock"
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(lock, flags), "r", encoding="utf-8") as handle:
                line = handle.readline()
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"{lock}: cannot read lock ({type(exc).__name__})"
        try:
            pid = int(line.split("pid=", 1)[1].strip())
            if pid <= 0:
                continue
        except (ValueError, IndexError):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        except OSError as exc:
            return f"{lock}: cannot check process ({type(exc).__name__})"
        return str(lock)
    return None


def _has_uncompiled_daily(project: Path) -> bool:
    state_file = project / ".cmc/state.json"
    if state_file.is_symlink():
        return False
    try:
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        ingested = saved.get("ingested", {}) if isinstance(saved, dict) else {}
        if not isinstance(ingested, dict):
            ingested = {}
    except (OSError, ValueError):
        ingested = {}
    for source in (project / "daily").glob("*.md"):
        if source.is_symlink() or not source.is_file():
            continue
        previous = ingested.get(source.name, {})
        if not isinstance(previous, dict) or not previous.get("compiled_at"):
            return True
        try:
            if previous.get("hash") != hashlib.sha256(source.read_bytes()).hexdigest()[:16]:
                return True
        except OSError:
            return True
    return False


def check_projects(projects: list[Path]) -> int:
    failed = False
    all_projects = [*discover_projects(), *projects]
    all_projects = list(dict.fromkeys(p.expanduser().resolve() for p in all_projects))
    def last_spawn(project: Path) -> float:
        try:
            marker = project / ".cmc/last-compile-spawn"
            return 0 if marker.is_symlink() else marker.stat().st_mtime
        except OSError:
            return 0
    all_projects.sort(key=lambda project: (not _has_uncompiled_daily(project), last_spawn(project)))
    for project in all_projects:
        lock = _live_compile_lock(all_projects)
        if lock:
            print(f"queue deferred: compile lock active or unverifiable: {lock}", flush=True)
            failed |= "cannot verify" in lock or "cannot read" in lock or "cannot check" in lock
            break
        if not (project / "wiki").is_dir():
            print(f"{project}: skip: no wiki/", flush=True)
            continue
        if (project / ".cmc/purge-paused").exists():
            print(f"{project}: skip: memory paused", flush=True)
            continue
        env = dict(os.environ, CMC_PROJECT_DIR=str(project))
        try:
            result = subprocess.run([sys.executable, str(TOOL_ROOT / "scripts/maybe_compile.py")],
                                    env=env, cwd=TOOL_ROOT, capture_output=True, text=True, timeout=60)
            status = result.stdout.strip()
            print(f"{project}: {status or 'trigger failed'}", flush=True)
            failed |= result.returncode != 0
            if status.startswith("spawned:"):
                break
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"{project}: trigger failed: {type(exc).__name__}", flush=True)
            failed = True
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, action="append", default=[])
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if args.install:
        print(install_schedule(args.project))
        return 0
    return check_projects(args.project)


if __name__ == "__main__":
    raise SystemExit(main())
