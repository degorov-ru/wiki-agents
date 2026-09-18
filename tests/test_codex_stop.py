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
    @staticmethod
    def _turns(path: Path, user: str, assistant: str) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": user}}) + "\n")
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": assistant}}) + "\n")

    def test_context_offset_returns_only_new_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            session = root / "codex/sessions/session.jsonl"
            _session(session, "session-1", project)
            module = _load_hook(project, root / "codex")
            with session.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "first"}}) + "\n")
                fh.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "answer one"}}) + "\n")
            first, count, offset = module.extract_codex_context_since(session)
            self.assertEqual(2, count)
            with session.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "second"}}) + "\n")
                fh.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "answer two"}}) + "\n")
            second, count, _ = module.extract_codex_context_since(session, offset)
            self.assertEqual(2, count)
            self.assertNotIn("first", second)
            self.assertIn("second", second)

    def test_dry_run_never_retries_or_spawns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            (project / "wiki").mkdir(parents=True)
            module = _load_hook(project, root / "codex")
            with patch.object(sys, "argv", ["codex-stop.py", "--dry-run"]), \
                 patch.object(module, "_spawn_flush") as spawn:
                self.assertEqual(0, module.main())
            spawn.assert_not_called()

    def test_short_deltas_accumulate_and_parallel_session_cursors_survive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            (project / "wiki").mkdir(parents=True)
            (project / ".cmc").mkdir()
            a = root / "codex/sessions/a.jsonl"
            b = root / "codex/sessions/b.jsonl"
            _session(a, "session-a", project)
            _session(b, "session-b", project)
            module = _load_hook(project, root / "codex")
            self._turns(a, "a1", "a1-answer")
            with patch.object(module, "claim_flush", return_value=False):
                module._capture_session(a, "session-a")
                self.assertFalse(module.CODEX_STATE_FILE.exists())
                self._turns(a, "a2", "a2-answer")
                module._capture_session(a, "session-a")
                self._turns(b, "b1", "b1-answer")
                self._turns(b, "b2", "b2-answer")
                module._capture_session(b, "session-b")
                self._turns(a, "a3", "a3-answer")
                self._turns(a, "a4", "a4-answer")
                module._capture_session(a, "session-a")
            state = json.loads(module.CODEX_STATE_FILE.read_text(encoding="utf-8"))
            self.assertEqual({"session-a", "session-b"}, set(state["sessions"]))
            contexts = "\n".join(path.read_text(encoding="utf-8") for path in module.STATE_DIR.glob("codex-flush-*.md"))
            self.assertEqual(1, contexts.count("a1-answer"))
            self.assertEqual(1, contexts.count("a4-answer"))

    def test_requires_exact_identity_for_current_project(self) -> None:
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

            self.assertEqual(_find(module), (None, "unknown"))
            module._test_env["CODEX_SESSION_ID"] = "project-session"
            self.assertEqual(_find(module), (old, "project-session"))
            module._test_env["CODEX_SESSION_ID"] = "missing-session"
            self.assertEqual(_find(module), (None, "unknown"))

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
