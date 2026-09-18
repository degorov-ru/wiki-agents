"""Codex CLI fallback using gpt-5.6-luna at medium reasoning."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import time


CODEX_BIN = os.environ.get("CMC_CODEX_BIN") or shutil.which("codex") or "codex"
LUNA_MODEL = os.environ.get("CMC_LUNA_MODEL", "gpt-5.6-luna")
LUNA_EFFORT = os.environ.get("CMC_LUNA_EFFORT", "medium")
LUNA_TIMEOUT = int(os.environ.get("CMC_LUNA_TIMEOUT", "600"))


def run_luna(
    prompt: str,
    cwd: Path,
    *,
    writable: bool = False,
) -> dict:
    if writable:
        raise ValueError("memory adapter is tool-free")
    started = time()
    try:
        with tempfile.TemporaryDirectory(prefix="cmc-luna-") as tmp:
            isolated = Path(tmp)
            output_path = isolated / "output.txt"
            cmd = [
                CODEX_BIN,
                "exec",
                "--strict-config",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--disable", "shell_tool",
                "--disable", "unified_exec",
                "--disable", "multi_agent",
                "--disable", "apps",
                "--disable", "remote_plugin",
                "--config", 'web_search="disabled"',
                "--model", LUNA_MODEL,
                "--config", f'model_reasoning_effort="{LUNA_EFFORT}"',
                "--sandbox", "read-only",
                "--cd", str(isolated),
                "--output-last-message", str(output_path),
                "-",
            ]
            env = os.environ.copy()
            env["CMC_DISABLED"] = "1"
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=LUNA_TIMEOUT,
                env=env,
            )
            text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
        ok = proc.returncode == 0 and bool(text)
        return {
            "ok": ok,
            "engine": "luna",
            "model": LUNA_MODEL,
            "text": text,
            "duration_s": round(time() - started, 1),
            "error": None if ok else (proc.stderr or proc.stdout or "empty Luna response")[-500:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "engine": "luna", "model": LUNA_MODEL,
                "error": f"timeout after {LUNA_TIMEOUT}s"}
    except Exception as exc:
        return {"ok": False, "engine": "luna", "model": LUNA_MODEL, "error": str(exc)[-500:]}
