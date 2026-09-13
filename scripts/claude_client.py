"""Claude CLI adapter for memory processing with Haiku."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from time import time


CLAUDE_BIN = os.environ.get("CMC_CLAUDE_BIN") or shutil.which("claude") or "claude"
HAIKU_MODEL = os.environ.get("CMC_HAIKU_MODEL", "haiku")
TIMEOUT = int(os.environ.get("CMC_HAIKU_TIMEOUT", "600"))


def run_haiku(prompt: str, cwd: Path, *, writable: bool = False, max_turns: int = 2) -> dict:
    cmd = [
        CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--model", HAIKU_MODEL,
        "--max-turns", str(max_turns), "--allowedTools",
        "Read,Write,Edit,Glob,Grep" if writable else "Read,Glob,Grep",
    ]
    if writable:
        cmd += ["--permission-mode", "acceptEdits"]
    started = time()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT)
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
        text = str(data.get("result") or data.get("text") or "").strip()
        ok = proc.returncode == 0 and bool(text)
        return {
            "ok": ok, "engine": "haiku", "model": HAIKU_MODEL, "text": text,
            "duration_s": round(time() - started, 1),
            "error": None if ok else (proc.stderr or proc.stdout or "empty Haiku response")[-500:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "engine": "haiku", "model": HAIKU_MODEL, "error": f"timeout after {TIMEOUT}s"}
    except Exception as exc:
        return {"ok": False, "engine": "haiku", "model": HAIKU_MODEL, "error": str(exc)[-500:]}
