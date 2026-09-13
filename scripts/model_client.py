"""Configured memory-engine dispatcher."""

from __future__ import annotations

import json
import os
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = TOOL_ROOT / "engine.json"
ENGINES = ("haiku", "luna", "grok")


def load_engines() -> tuple[str, str | None]:
    config = {"engine": "grok", "fallback": "luna"}
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            config.update(saved)
    except (OSError, json.JSONDecodeError):
        pass
    primary = os.environ.get("CMC_ENGINE", config["engine"])
    fallback = os.environ.get("CMC_FALLBACK_ENGINE", config.get("fallback") or "") or None
    if primary not in ENGINES or fallback not in (*ENGINES, None):
        raise ValueError(f"Unknown memory engine: {primary!r}/{fallback!r}")
    return primary, fallback if fallback != primary else None


def _run(engine: str, prompt: str, cwd: Path, max_turns: int, tools: tuple[str, ...]) -> dict:
    writable = bool({"Write", "Edit"}.intersection(tools))
    if engine == "grok":
        from grok_client import run_text_prompt as run_grok
        return run_grok(prompt, cwd, max_turns=max_turns, tools=tools, fallback=False)
    if engine == "luna":
        from luna_client import run_luna
        return run_luna(prompt, cwd, writable=writable)
    from claude_client import run_haiku
    return run_haiku(prompt, cwd, writable=writable, max_turns=max_turns)


def run_text_prompt(prompt: str, cwd: Path, *, max_turns: int = 2, tools: tuple[str, ...] = ()) -> dict:
    from alerts import notify

    primary, fallback = load_engines()
    result = _run(primary, prompt, cwd, max_turns, tools)
    if result.get("ok") or not fallback:
        return result
    notify(cwd, primary.title(), str(result.get("error") or "unknown error"))
    second = _run(fallback, prompt, cwd, max_turns, tools)
    second["fallback_from"] = primary
    second["primary_error"] = result.get("error")
    if not second.get("ok"):
        notify(cwd, fallback.title(), str(second.get("error") or "unknown error"))
    return second
