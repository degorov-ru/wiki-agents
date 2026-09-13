from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HOOK = Path(__file__).resolve().parents[1] / "hooks" / "codex-stop.py"


def _session(path: Path, session_id: str, cwd: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(cwd)},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _load_hook(project: Path, codex_home: Path, **extra_env: str):
    env = {
        "CODEX_PROJECT_DIR": str(project),
        "CODEX_HOME": str(codex_home),
        **extra_env,
    }
    spec = importlib.util.spec_from_file_location("codex_stop_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, env, clear=False):
        spec.loader.exec_module(module)
    module._test_env = env
    return module


def _find(module):
    with patch.dict(os.environ, module._test_env, clear=False):
        return module._find_latest_session()


class FindLatestSessionTests(unittest.TestCase):
    def test_selects_newest_session_for_current_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            other = root / "other"
            project.mkdir()
            other.mkdir()
            old = root / "codex" / "sessions" / "old.jsonl"
            new_other = root / "codex" / "sessions" / "new-other.jsonl"
            _session(old, "project-session", project)
            _session(new_other, "other-session", other)
            os.utime(old, (1, 1))
            os.utime(new_other, (2, 2))

            module = _load_hook(project, root / "codex")

            self.assertEqual(_find(module), (old, "project-session"))

    def test_returns_none_instead_of_cross_project_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            other = root / "other"
            project.mkdir()
            other.mkdir()
            unrelated = root / "codex" / "sessions" / "unrelated.jsonl"
            _session(unrelated, "other-session", other)

            module = _load_hook(project, root / "codex")

            self.assertEqual(_find(module), (None, "unknown"))

    def test_rejects_explicit_path_from_another_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            other = root / "other"
            project.mkdir()
            other.mkdir()
            unrelated = root / "codex" / "sessions" / "unrelated.jsonl"
            _session(unrelated, "other-session", other)

            module = _load_hook(
                project,
                root / "codex",
                CODEX_SESSION_PATH=str(unrelated),
                CODEX_SESSION_ID="other-session",
            )

            self.assertEqual(_find(module), (None, "unknown"))


if __name__ == "__main__":
    unittest.main()
