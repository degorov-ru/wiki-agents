"""Install the portable memory compiler on the current machine."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import sys
from pathlib import Path

from init_project import init


TOOL_ROOT = Path(__file__).resolve().parent.parent
HOOKS = {
    "SessionStart": ("session-start.py", 15),
    "PreCompact": ("pre-compact.py", 10),
    "SessionEnd": ("session-end.py", 10),
}
ENGINES = ("haiku", "luna", "grok")
ENGINE_COMMANDS = {"haiku": "claude", "luna": "codex", "grok": "grok"}
ENGINE_FIXES = {
    "haiku": "Установи Claude Code, выполни `claude login` и повтори установку.",
    "luna": "Установи Codex CLI, выполни вход в Codex и повтори установку.",
    "grok": "Установи Grok CLI, выполни вход в Grok и повтори установку.",
}


def _command(script: str) -> str:
    return f"uv run --directory {shlex.quote(str(TOOL_ROOT))} python hooks/{shlex.quote(script)}"


def install_claude_hooks(settings_path: Path) -> None:
    if settings_path.is_symlink():
        raise RuntimeError(f"Refusing symlinked Claude settings: {settings_path}")
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Файл {settings_path} содержит повреждённый JSON. "
            "Исправь JSON или восстанови файл из резервной копии, затем повтори установку."
        ) from exc

    hooks = data.setdefault("hooks", {})
    for event, (script, timeout) in HOOKS.items():
        entries = hooks.setdefault(event, [])
        command = _command(script)
        kept = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            remaining = [hook for hook in entry.get("hooks", []) if not (
                isinstance(hook, dict)
                and f"hooks/{script}" in str(hook.get("command", ""))
            )]
            if remaining:
                updated = dict(entry)
                updated["hooks"] = remaining
                kept.append(updated)
        entries[:] = kept
        entries.append({"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": timeout}]})

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _choose_engine(caller: str) -> tuple[str, str | None]:
    recommended = "haiku" if caller == "claude" else "luna" if caller == "codex" else None
    note = f"; рекомендуется {recommended} — тот же провайдер" if recommended else ""
    primary = input(f"Движок памяти: haiku / luna / grok{note}: ").strip().lower()
    if primary not in ENGINES:
        raise SystemExit(f"Unknown engine: {primary!r}")
    fallback = input("Fallback: haiku / luna / grok / none [none]: ").strip().lower() or "none"
    if fallback not in (*ENGINES, "none"):
        raise SystemExit(f"Unknown fallback: {fallback!r}")
    return primary, None if fallback in ("none", primary) else fallback


def _detect_caller() -> str:
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    return "other"


def _test_engine(engine: str) -> str | None:
    command = ENGINE_COMMANDS[engine]
    if not shutil.which(command):
        return f"Команда `{command}` не найдена. {ENGINE_FIXES[engine]}"
    try:
        from model_client import _run

        result = _run(engine, "Ответь только: CMC_ENGINE_OK", TOOL_ROOT, 1, ())
    except Exception as exc:
        return f"Не удалось запустить {engine}: {exc}. {ENGINE_FIXES[engine]}"
    if result.get("ok") and "CMC_ENGINE_OK" in str(result.get("text", "")):
        return None
    detail = str(result.get("error") or result.get("text") or "нет ответа")[-300:]
    return f"{engine} не прошёл проверку: {detail}. {ENGINE_FIXES[engine]}"


def _print_report(issues: list[str], projects: list[str]) -> None:
    if not issues:
        print("\nOK — память установлена, движки отвечают, хуки записаны.")
        print("В Codex Desktop: открой проект, выполни /hooks, проверь команду/hash и доверь hook.")
        print("Осталось провести E2E-тест отдельным чатом по инструкции INSTALL.md.")
        return
    print("\nPARTIAL — установка выполнена не полностью.")
    for number, issue in enumerate(issues, 1):
        print(f"\nПроблема {number}: {issue}")
    if not projects:
        print("\nЧто исправить: повтори установку с `--project \"/путь/к/проекту\"`.")
    print("После исправления повтори эту же команду установки.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", default=[], help="project to initialize; repeatable")
    parser.add_argument("--root", action="append", default=[], help="root whose child projects may auto-initialize; repeatable")
    parser.add_argument("--no-claude", action="store_true", help="do not modify ~/.claude/settings.json")
    parser.add_argument("--caller", choices=("claude", "codex", "other"), help="agent running the installer")
    parser.add_argument("--engine", choices=ENGINES, help="primary memory engine")
    parser.add_argument("--fallback", choices=(*ENGINES, "none"), help="fallback memory engine")
    parser.add_argument("--dry-run", action="store_true", help="print planned writes without changing files or calling providers")
    args = parser.parse_args()
    caller = args.caller or _detect_caller()

    if args.engine:
        primary = args.engine
        fallback = None if args.fallback in (None, "none", primary) else args.fallback
    else:
        primary, fallback = _choose_engine(caller)

    projects = list(args.project)
    if not projects and sys.stdin.isatty():
        project = input("Путь к проекту, где включить память: ").strip()
        if project:
            projects.append(project)

    roots = [str(Path(path).expanduser().resolve()) for path in args.root]
    if args.dry_run:
        print(json.dumps({"status": "DRY_RUN", "tool_root": str(TOOL_ROOT),
                          "engine": primary, "fallback": fallback,
                          "claude_hooks": not args.no_claude, "auto_init_roots": roots,
                          "projects": [str(Path(p).expanduser().resolve()) for p in projects]}, indent=2))
        return 0

    if not args.no_claude:
        install_claude_hooks(Path.home() / ".claude" / "settings.json")

    (TOOL_ROOT / "auto-init-roots.json").write_text(json.dumps(roots, indent=2) + "\n", encoding="utf-8")
    (TOOL_ROOT / "engine.json").write_text(
        json.dumps({"engine": primary, "fallback": fallback}, indent=2) + "\n",
        encoding="utf-8",
    )

    issues = []
    results = []
    for path in projects:
        try:
            result = init(Path(path))
            results.append(result)
            if result.get("status") == "needs_migration":
                issues.append(f"Проект `{path}` требует миграции: {result.get('hint')}")
        except (OSError, SystemExit) as exc:
            issues.append(f"Не удалось подготовить проект `{path}`: {exc}")
    if not projects:
        issues.append("Не выбран ни один проект, поэтому проектные хуки не установлены.")

    for engine in dict.fromkeys(filter(None, (primary, fallback))):
        if error := _test_engine(engine):
            issues.append(error)

    print(json.dumps({"tool_root": str(TOOL_ROOT), "engine": primary, "fallback": fallback,
                      "auto_init_roots": roots, "projects": results}, indent=2))
    _print_report(issues, projects)
    return 2 if issues else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nFAILED — установка остановлена.\nПроблема: {exc}", file=sys.stderr)
        print("Как исправить: проверь сообщение выше, права на файлы и повтори ту же команду.", file=sys.stderr)
        raise SystemExit(1) from exc
