import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import model_client
import luna_client
import pending
import purge


class QueueSafetyTests(unittest.TestCase):
    def test_luna_cli_permissions_match_runtime(self):
        with patch.object(luna_client.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="test")) as run:
            luna_client.run_luna("prompt", Path.cwd())
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertNotEqual(args[args.index("--cd") + 1], str(Path.cwd()))
        disabled = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--disable"]
        self.assertIn("shell_tool", disabled)
        self.assertIn("unified_exec", disabled)

    def test_memory_adapters_reject_tools(self):
        with self.assertRaisesRegex(ValueError, "cannot receive tools"):
            model_client._run("luna", "prompt", Path.cwd(), 1, ("Read",))

    def test_concurrent_flush_only_calls_model_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "wiki").mkdir()
            context = root / ".cmc/context.md"
            context.parent.mkdir()
            context.write_text("one conversation")
            code = '''
import os, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
os.environ["CMC_PROJECT_DIR"] = sys.argv[1]
sys.argv = ["flush.py", sys.argv[2], "session"]
import flush
with patch.object(flush, "run_flush", return_value=("saved", {"ok": True})) as run, patch.object(flush, "maybe_trigger_compilation"):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: flush.main(), range(8)))
    assert run.call_count == 1, run.call_count
assert len(list((Path(os.environ["CMC_PROJECT_DIR"]) / "daily").glob("*.md"))) == 1
'''
            result = subprocess.run([sys.executable, "-c", code, str(root), str(context)], cwd=Path(__file__).parents[1] / "scripts", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_capture_and_claim_are_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            transcript.write_text(json.dumps({"role": "user", "content": "once"}) + "\n")
            with ThreadPoolExecutor(max_workers=8) as pool:
                counts = list(pool.map(lambda _: pending.capture_to_buffer(root, "session", transcript), range(16)))
            self.assertEqual(sum(counts), 1)
            self.assertEqual((root / ".cmc/pending/session.md").read_text().count("once"), 1)
            with patch.object(pending, "_spawn_flush", return_value=None) as spawn:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    claims = list(pool.map(lambda _: pending.flush_session(root, "session"), range(16)))
                self.assertEqual(sum(claims), 1)
                self.assertEqual(pending.recover_orphans(root, 0), 0)
                spawn.assert_called_once()

    def test_capture_recovers_after_append_before_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            transcript.write_text(json.dumps({"role": "user", "content": "once"}) + "\n")
            real_save = pending.save_offset
            with patch.object(pending, "save_offset", side_effect=OSError("synthetic crash")):
                with self.assertRaisesRegex(OSError, "synthetic"):
                    pending.capture_to_buffer(root, "session", transcript)
            with patch.object(pending, "save_offset", side_effect=real_save):
                self.assertEqual(0, pending.capture_to_buffer(root, "session", transcript))
            self.assertEqual(1, (root / ".cmc/pending/session.md").read_text().count("once"))
            self.assertFalse((root / ".cmc/capture-intent.json").exists())

    def test_spawn_failure_is_retained_with_bounded_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".cmc/pending").mkdir(parents=True)
            context = root / ".cmc/pending/session.flushing.md"
            context.write_text("retain")
            with patch.object(pending, "_spawn_flush", side_effect=OSError("unavailable")) as spawn, patch.object(pending, "time", return_value=1000):
                pending.recover_orphans(root, 0)
                pending.recover_orphans(root, 0)
                self.assertEqual(spawn.call_count, 1)
            for now in (2000, 4000, 10000):
                with patch.object(pending, "_spawn_flush", side_effect=OSError()), patch.object(pending, "time", return_value=now):
                    pending.recover_orphans(root, 0)
            self.assertTrue(context.exists())
            self.assertEqual(json.loads(context.with_suffix(".retry.json").read_text())["attempts"], 3)

    def test_concurrent_recovery_only_claims_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".cmc/pending").mkdir(parents=True)
            (root / ".cmc/pending/session.flushing.md").write_text("retained")
            with patch.object(pending, "_spawn_flush", return_value=None) as spawn:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    counts = list(pool.map(lambda _: pending.recover_orphans(root, 0), range(16)))
                self.assertEqual(sum(counts), 1)
                spawn.assert_called_once()

    def test_adapter_exception_timeout_unknown_cost(self):
        for error, kind in [(ValueError("bad"), "adapter_error"), (subprocess.TimeoutExpired("cmd", 1), "timeout")]:
            with patch.object(model_client, "_run", side_effect=error):
                result = model_client._attempt("grok", "prompt", Path.cwd(), 2, ())
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_kind"], kind)
            self.assertIsNone(result["cost_usd"])
        with patch.object(model_client, "_run", return_value={"ok": True, "text": "ok", "cost_usd": float("nan")}):
            result = model_client._attempt("luna", "prompt", Path.cwd(), 2, ())
        self.assertTrue(result["ok"])
        self.assertFalse(result["cost_known"])

    def test_purge_preview_stale_apply_and_exact_ids(self):
        event = "m-20260918-1234567890"
        other = "m-20260918-123456789a"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ("daily", "wiki", ".cmc/pending"):
                (root / directory).mkdir(parents=True)
            daily = root / "daily/2026-09-18.md"
            daily.write_text(f"# Daily\n### {event} | Session\nsecret-body\n## Nested heading\nsecret-tail\n### {other} | Session\nkeep\n")
            article = root / "wiki/index.md"
            article.write_text("derived secret-body")
            (root / ".cmc/pending/session.md").write_text("raw secret-body")
            plan = purge.preview(root, [event])
            self.assertNotIn("secret-body", json.dumps(plan))
            self.assertIn("secret-body", daily.read_text())
            with self.assertRaises(ValueError):
                purge.preview(root, [event[:-1]])
            article.write_text("changed")
            with self.assertRaises(ValueError):
                purge.apply(root, [event], plan["confirmation"])
            plan = purge.preview(root, [event])
            purge.apply(root, [event], plan["confirmation"])
            self.assertNotIn(event, daily.read_text())
            self.assertNotIn("secret-tail", daily.read_text())
            self.assertIn(other, daily.read_text())
            self.assertFalse(article.exists())
            self.assertFalse((root / ".cmc/pending/session.md").exists())
            self.assertTrue((root / ".cmc/purge-paused").exists())


if __name__ == "__main__":
    unittest.main()
