"""Small, tool-free Grok CLI adapter for memory text processing."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from time import time


GROK_BIN = os.environ.get("CMC_GROK_BIN") or shutil.which("grok") or "grok"
GROK_MODEL = os.environ.get("CMC_GROK_MODEL", "")
GROK_TIMEOUT = int(os.environ.get("CMC_GROK_TIMEOUT", "600"))


def completed(stop_reason: object) -> bool:
    return isinstance(stop_reason, str) and stop_reason.replace("_", "").lower() == "endturn"


def run_text_prompt(
    prompt: str,
    cwd: Path,
    *,
    max_turns: int = 2,
    tools: tuple[str, ...] = (),
    fallback: bool = True,
) -> dict:
    """Run Grok with an optional bounded tool set and normalize its result."""
    cmd = [
        GROK_BIN,
        "-p", prompt,
        "--output-format", "json",
        "--always-approve",
        "--tools", ",".join(tools),
        "--no-subagents",
        "--disable-web-search",
        "--max-turns", str(max_turns),
        "--cwd", str(cwd),
    ]
    if GROK_MODEL:
        cmd += ["-m", GROK_MODEL]

    started = time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GROK_TIMEOUT)
    except subprocess.TimeoutExpired:
        result = {"ok": False, "engine": "grok", "error": f"timeout after {GROK_TIMEOUT}s"}
        return _fallback(result, prompt, cwd, tools) if fallback else result
    except Exception as exc:
        result = {"ok": False, "engine": "grok", "error": str(exc)[:500]}
        return _fallback(result, prompt, cwd, tools) if fallback else result

    duration = round(time() - started, 1)
    if proc.returncode != 0:
        result = {
            "ok": False,
            "engine": "grok",
            "duration_s": duration,
            "error": (proc.stderr or proc.stdout or "nonzero exit")[-500:],
        }
        return _fallback(result, prompt, cwd, tools) if fallback else result
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {
            "ok": False,
            "engine": "grok",
            "duration_s": duration,
            "error": "bad JSON from grok: " + (proc.stdout or "")[:200],
        }
        return _fallback(result, prompt, cwd, tools) if fallback else result

    text = data.get("text") or ""
    stop_reason = data.get("stopReason")
    ok = completed(stop_reason) and bool(text.strip())
    result = {
        "ok": ok,
        "engine": "grok",
        "model": next(iter(data.get("modelUsage", {})), GROK_MODEL or "grok"),
        "text": text.strip(),
        "stop_reason": stop_reason,
        "duration_s": duration,
        "error": None if ok else f"stopReason={stop_reason}, empty={not bool(text.strip())}",
    }
    return _fallback(result, prompt, cwd, tools) if fallback and not ok else result


def _fallback(result: dict, prompt: str, cwd: Path, tools: tuple[str, ...]) -> dict:
    try:
        from alerts import notify
        from luna_client import run_luna
    except ModuleNotFoundError:
        from scripts.alerts import notify
        from scripts.luna_client import run_luna

    notify(cwd, "Grok", str(result.get("error") or "unknown error"))
    luna = run_luna(prompt, cwd, writable=bool({"Write", "Edit"}.intersection(tools)))
    luna["fallback_from"] = "grok"
    luna["grok_error"] = result.get("error")
    if not luna.get("ok"):
        notify(cwd, "Luna", str(luna.get("error") or "unknown error"))
    return luna
