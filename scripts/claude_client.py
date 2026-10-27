"""Claude CLI adapter for memory processing with Haiku."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import time


CLAUDE_BIN = os.environ.get("CMC_CLAUDE_BIN") or shutil.which("claude") or "claude"
HAIKU_MODEL = os.environ.get("CMC_HAIKU_MODEL", "haiku")
TIMEOUT = int(os.environ.get("CMC_HAIKU_TIMEOUT", "600"))


def run_haiku(prompt: str, cwd: Path, *, writable: bool = False, max_turns: int = 2,
              json_schema: dict | None = None) -> dict:
    if writable:
        raise ValueError("memory adapter is tool-free")
    cmd = [
        CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--model", HAIKU_MODEL,
        # --bare skips subscription (OAuth) login; isolate via empty settings and MCP instead.
        "--max-turns", str(max_turns), "--no-session-persistence",
        "--setting-sources", "", "--strict-mcp-config",
        "--tools", "", "--allowedTools", "",
    ]
    if json_schema:
        # Structured output spends one extra turn on its internal answer tool.
        cmd[cmd.index("--max-turns") + 1] = str(max(max_turns, 3))
        cmd += ["--json-schema", json.dumps(json_schema)]
    started = time()
    try:
        with tempfile.TemporaryDirectory(prefix="cmc-haiku-") as isolated:
            proc = subprocess.run(cmd, cwd=isolated, capture_output=True, text=True, timeout=TIMEOUT,
                                  env=dict(os.environ, CLAUDE_INVOKED_BY="memory_flush"))
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
        structured = data.get("structured_output")
        text = (json.dumps(structured, ensure_ascii=False) if json_schema and structured is not None
                else str(data.get("result") or data.get("text") or "").strip())
        ok = proc.returncode == 0 and not data.get("is_error") and bool(text)
        return {
            "ok": ok, "engine": "haiku", "model": HAIKU_MODEL, "text": text,
            "duration_s": round(time() - started, 1), "cost_usd": data.get("total_cost_usd"),
            "error": None if ok else str(data.get("result") or proc.stderr or proc.stdout or "empty Haiku response")[-500:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "engine": "haiku", "model": HAIKU_MODEL, "error": f"timeout after {TIMEOUT}s"}
    except Exception as exc:
        return {"ok": False, "engine": "haiku", "model": HAIKU_MODEL, "error": str(exc)[-500:]}
