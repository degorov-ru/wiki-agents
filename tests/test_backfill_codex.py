from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_codex.py"
spec = importlib.util.spec_from_file_location("backfill_codex_test", SCRIPT)
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


class BackfillTests(unittest.TestCase):
    def test_chunks_preserve_all_turns(self) -> None:
        turns = [f"turn-{index}" for index in range(65)]
        result = backfill.chunks(turns, max_turns=30, max_chars=100_000)
        self.assertEqual([30, 30, 5], [len(chunk.splitlines()) for chunk in result])
        self.assertEqual(65, sum(chunk.count("turn-") for chunk in result))

    def test_secret_detection(self) -> None:
        self.assertTrue(backfill.has_secret("API_KEY=super-secret-value"))
        self.assertTrue(backfill.has_secret("-----BEGIN PRIVATE KEY-----"))
        self.assertFalse(backfill.has_secret("Use environment variables for credentials"))

    def test_reads_only_conversation_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            records = [
                {"payload": {"type": "user_message", "message": "question"}},
                {"payload": {"type": "agent_message", "message": "answer"}},
                {"payload": {"type": "tool_output", "message": "secret output"}},
            ]
            path.write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")
            turns = backfill.session_turns(path)
            self.assertEqual(2, len(turns))
            self.assertNotIn("secret output", "\n".join(turns))

    def test_nonconsecutive_repeat_is_preserved_and_session_id_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            records = [
                {"payload": {"type": "user_message", "message": "same"}},
                {"payload": {"type": "agent_message", "message": "between"}},
                {"payload": {"type": "user_message", "message": "same"}},
            ]
            path.write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")
            self.assertEqual(3, len(backfill.session_turns(path)))
        self.assertFalse(backfill.valid_session_id("../../outside"))


if __name__ == "__main__":
    unittest.main()
