"""Path constants and configuration for the personal knowledge base.

All paths are derived from a per-project directory determined at import time:

  1. $CMC_PROJECT_DIR  (set explicitly by hooks/flush.py)
  2. $CLAUDE_PROJECT_DIR  (set by Claude Code in hook context)
  3. os.getcwd()  (fallback)

This makes every spawned script write into the CURRENT project's wiki/
rather than a single global directory.

A per-project .cmc-config.json may override token-budget knobs:

  {
    "max_index_chars": 10000,
    "max_log_lines": 30,
    "max_total_chars": 15000,
    "min_turns_to_flush": 3
  }
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


# ── Project root discovery ─────────────────────────────────────────────

def _discover_project_dir() -> Path:
    for env_var in ("CMC_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        val = os.environ.get(env_var)
        if val:
            p = Path(val).expanduser().resolve()
            if p.exists():
                return p
    return Path(os.getcwd()).resolve()


PROJECT_DIR = _discover_project_dir()

# Where the central scripts/hooks live (this repo). Used only for things
# that belong to the tool itself (not to user projects).
TOOL_ROOT = Path(__file__).resolve().parent.parent

# ── Per-project paths (the "wiki") ─────────────────────────────────────
WIKI_DIR = PROJECT_DIR / "wiki"
CONCEPTS_DIR = WIKI_DIR / "concepts"
CONNECTIONS_DIR = WIKI_DIR / "connections"
QA_DIR = WIKI_DIR / "qa"
DAILY_DIR = PROJECT_DIR / "daily"
SOURCES_DIR = PROJECT_DIR / "sources"
REPORTS_DIR = PROJECT_DIR / ".cmc" / "reports"
STATE_DIR = PROJECT_DIR / ".cmc"

INDEX_FILE = WIKI_DIR / "index.md"
LOG_FILE = WIKI_DIR / "log.md"
STATE_FILE = STATE_DIR / "state.json"
FLUSH_STATE_FILE = STATE_DIR / "last-flush.json"
FLUSH_LOG_FILE = STATE_DIR / "flush.log"
COMPILE_LOG_FILE = STATE_DIR / "compile.log"
# One JSON line per compile attempt (engine, tokens, cost, success/failure).
# Read this to answer "how is the compiler doing lately".
COMPILE_RUNS_FILE = STATE_DIR / "compile-runs.jsonl"
USER_CONFIG_FILE = PROJECT_DIR / ".cmc-config.json"

AGENTS_FILE = PROJECT_DIR / "AGENTS.md"
if not AGENTS_FILE.exists():
    AGENTS_FILE = TOOL_ROOT / "AGENTS.md"

# Backwards-compatibility alias — older code referenced KNOWLEDGE_DIR.
KNOWLEDGE_DIR = WIKI_DIR


# ── Tunable defaults (the "context-size regulator") ────────────────────
_DEFAULTS = {
    "max_index_chars": 10_000,
    "max_log_lines": 30,
    "max_total_chars": 15_000,
    "min_turns_to_flush": 3,
    "max_extract_turns": 30,
    "max_extract_chars": 15_000,
    "compile_after_hour": 4,
}


def _load_user_overrides() -> dict:
    if not USER_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(USER_CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _config_value(key: str) -> int:
    env_key = f"CMC_{key.upper()}"
    if env_key in os.environ:
        try:
            return int(os.environ[env_key])
        except ValueError:
            pass
    overrides = _load_user_overrides()
    if key in overrides:
        return int(overrides[key])
    return _DEFAULTS[key]


MAX_INDEX_CHARS = _config_value("max_index_chars")
MAX_LOG_LINES = _config_value("max_log_lines")
MAX_TOTAL_CHARS = _config_value("max_total_chars")
MIN_TURNS_TO_FLUSH = _config_value("min_turns_to_flush")
MAX_EXTRACT_TURNS = _config_value("max_extract_turns")
MAX_EXTRACT_CHARS = _config_value("max_extract_chars")
COMPILE_AFTER_HOUR = _config_value("compile_after_hour")


# ── Timezone / clock helpers ───────────────────────────────────────────
TIMEZONE = "America/Chicago"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def memory_enabled() -> bool:
    """A project is opted-in once it has a wiki/ directory.

    Hooks should be silent on projects without one — that way the global
    hooks are safe to leave installed even when working in throw-away
    directories.
    """
    from path_safety import managed_dir
    try:
        return managed_dir(PROJECT_DIR, "wiki").is_dir()
    except ValueError:
        return False


def ensure_state_dir() -> None:
    from path_safety import validate_layout
    validate_layout(PROJECT_DIR, create_state=True)
