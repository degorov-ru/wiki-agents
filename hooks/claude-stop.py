"""Stop hook - capture the just-finished turn(s) to a per-session buffer.

Fires after every assistant response. Does CHEAP LOCAL I/O ONLY: appends the new
transcript turns (since the shared cursor) to .cmc/pending/<session>.md. It NEVER
calls Grok — summarization is batched at SessionEnd / PreCompact
and recovered at SessionStart. Because the hot path spawns no inner Claude
session, there is no nested Claude session and no recursive hook loop.

Opt-in: no-op unless the project has a wiki/ directory.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Recursion guard FIRST: memory jobs inherit this marker and must not recurse
# inherits CLAUDE_INVOKED_BY (see scripts/pending.py:_spawn_flush). Its own Stop
# must no-op, or capture would re-fire forever.
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

TOOL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = TOOL_ROOT / "scripts"

if (TOOL_ROOT / ".disabled-by-codex").exists() or os.environ.get("CMC_DISABLED") == "1":
    sys.exit(0)

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _discover_project_dir() -> Path:
    val = os.environ.get("CLAUDE_PROJECT_DIR")
    if val:
        p = Path(val).expanduser().resolve()
        if p.exists():
            return p
    return Path(os.getcwd()).resolve()


def main() -> int:
    project_dir = _discover_project_dir()
    if not (project_dir / "wiki").exists():
        return 0  # memory is opt-in

    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(hook_input, dict):
        return 0

    # NOTE: deliberately ignore hook_input.get("stop_hook_active"). This hook never
    # blocks/continues the agent, so bailing on stop_hook_active=True could drop a
    # legitimate final answer produced after another tool's Stop hook.
    session_id = str(hook_input.get("session_id") or "unknown")
    transcript_str = hook_input.get("transcript_path") or ""
    if not transcript_str or not isinstance(transcript_str, str):
        return 0
    if not Path(transcript_str).exists():
        return 0

    state_dir = project_dir / ".cmc"
    state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(state_dir / "flush.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [claude-stop] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        from pending import capture_to_buffer

        n = capture_to_buffer(project_dir, session_id, transcript_str)
        if n:
            logging.info("captured %d new turn(s) for session %s", n, session_id)
    except Exception as e:  # never surface a capture error to the agent
        logging.error("capture failed for session %s: %s", session_id, e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
