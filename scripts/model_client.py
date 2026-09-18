"""Configured memory-engine dispatcher."""

from __future__ import annotations

import json
import os
import math
import subprocess
from time import monotonic
from pathlib import Path

from memory_contract import redact_sensitive


TOOL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = TOOL_ROOT / "engine.json"
ENGINES = ("haiku", "luna", "grok")


def load_engines() -> tuple[str, str | None]:
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("memory engine is not configured") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("memory engine configuration is invalid") from exc
    if not isinstance(saved, dict) or not isinstance(saved.get("engine"), str):
        raise ValueError("memory engine configuration is invalid")
    primary = os.environ.get("CMC_ENGINE", saved["engine"])
    fallback = os.environ.get("CMC_FALLBACK_ENGINE", saved.get("fallback") or "") or None
    if primary not in ENGINES or fallback not in (*ENGINES, None):
        raise ValueError(f"Unknown memory engine: {primary!r}/{fallback!r}")
    return primary, fallback if fallback != primary else None


def _run(engine: str, prompt: str, cwd: Path, max_turns: int, tools: tuple[str, ...]) -> dict:
    if tools:
        raise ValueError("memory models cannot receive tools")
    if engine == "grok":
        from grok_client import run_text_prompt as run_grok
        return run_grok(prompt, cwd, max_turns=max_turns, tools=tools, fallback=False)
    if engine == "luna":
        from luna_client import run_luna
        return run_luna(prompt, cwd, writable=False)
    from claude_client import run_haiku
    return run_haiku(prompt, cwd, writable=False, max_turns=max_turns)


def run_text_prompt(prompt: str, cwd: Path, *, max_turns: int = 2, tools: tuple[str, ...] = ()) -> dict:
    from alerts import notify

    prompt, redacted = redact_sensitive(prompt)
    primary, fallback = load_engines()
    result = _attempt(primary, prompt, cwd, max_turns, tools)
    if result.get("ok") or not fallback:
        result["redacted"] = redacted
        return result
    notify(cwd, primary.title(), str(result.get("error") or "unknown error"))
    second = _attempt(fallback, prompt, cwd, max_turns, tools)
    second["fallback_from"] = primary
    second["primary_error"] = result.get("error")
    if not second.get("ok"):
        notify(cwd, fallback.title(), str(second.get("error") or "unknown error"))
    second["redacted"] = redacted
    return second


def _attempt(engine: str, prompt: str, cwd: Path, max_turns: int, tools: tuple[str, ...]) -> dict:
    started = monotonic()
    try:
        result = _run(engine, prompt, cwd, max_turns, tools)
        if not isinstance(result, dict):
            raise ValueError("adapter returned invalid result")
        result = dict(result)
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": "adapter timeout"}
    except Exception:
        result = {"ok": False, "error": "adapter failure"}
    text = result.get("text")
    result["text"] = text if isinstance(text, str) else ""
    result["ok"] = result.get("ok") is True and bool(result["text"].strip())
    result["engine"] = engine
    result.setdefault("model", engine)
    result.setdefault("duration_s", round(monotonic() - started, 3))
    cost = result.get("cost_usd")
    result["cost_usd"] = cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0 else None
    result["cost_known"] = result["cost_usd"] is not None
    result["error"] = None if result["ok"] else str(result.get("error") or "empty adapter response")[:500]
    result["error_kind"] = None if result["ok"] else ("timeout" if "timeout" in result["error"].lower() else "adapter_error")
    return result
