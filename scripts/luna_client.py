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
    started = time()
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cmc-luna-", suffix=".txt", delete=False) as output:
            output_path = Path(output.name)
        cmd = [
            CODEX_BIN,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--model", LUNA_MODEL,
            "--config", f'model_reasoning_effort="{LUNA_EFFORT}"',
            "--sandbox", "workspace-write" if writable else "read-only",
            "--cd", str(cwd),
            "--output-last-message", str(output_path),
            "-",
        ]
        if writable:
            cmd.insert(cmd.index("--cd"), "--approve-for-me")
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
    finally:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
