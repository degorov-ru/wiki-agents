from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import grok_client


class GrokClientTests(unittest.TestCase):
    def test_normalizes_success(self) -> None:
        output = '{"text":"FLUSH_OK","stopReason":"end_turn","modelUsage":{"grok-4.6":{}}}'
        with patch.object(
            grok_client.subprocess,
            "run",
            return_value=CompletedProcess([], 0, stdout=output, stderr=""),
        ):
            result = grok_client.run_text_prompt("prompt", Path.cwd())
        self.assertTrue(result["ok"])
        self.assertEqual("FLUSH_OK", result["text"])

    def test_removes_explicit_allowlist_and_never_autoapproves_tools(self) -> None:
        output = '{"text":"FLUSH_OK","stopReason":"end_turn"}'
        with patch.object(grok_client.subprocess, "run", return_value=CompletedProcess([], 0, stdout=output, stderr="")) as run:
            grok_client.run_text_prompt("prompt", Path.cwd())
        args = run.call_args.args[0]
        self.assertNotIn("--always-approve", args)
        self.assertIn("--verbatim", args)
        self.assertIn("--system-prompt-override", args)
        self.assertIn("--no-plan", args)
        self.assertEqual("off" if sys.platform == "darwin" else "strict", args[args.index("--sandbox") + 1])
        if sys.platform == "darwin":
            self.assertEqual("/usr/bin/sandbox-exec", args[0])
        self.assertEqual("0", run.call_args.kwargs["env"]["GROK_MEMORY_ENABLED"])
        self.assertEqual("dontAsk", args[args.index("--permission-mode") + 1])
        self.assertEqual("read_file", args[args.index("--tools") + 1])
        self.assertEqual("read_file", args[args.index("--disallowed-tools") + 1])
        self.assertNotEqual(str(Path.cwd()), args[args.index("--cwd") + 1])
        for tool in ("Bash", "Read", "Grep", "Edit", "Write", "MCPTool"):
            self.assertIn(tool, [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--deny"])

    @unittest.skipUnless(sys.platform == "darwin", "macOS kernel isolation")
    def test_kernel_blocks_outside_files_and_child_commands(self) -> None:
        with tempfile.TemporaryDirectory() as isolated, tempfile.TemporaryDirectory() as outside:
            local = Path(isolated) / "inside"
            foreign = Path(outside) / "outside"
            local.write_text("inside")
            foreign.write_text("outside")
            for target, allowed in ((local, True), (foreign, False)):
                result = subprocess.run(grok_client.sandboxed_command(["/bin/cat", str(target)], isolated),
                                        capture_output=True, text=True)
                self.assertEqual(allowed, result.returncode == 0, result.stderr)
            result = subprocess.run(grok_client.sandboxed_command(["/bin/sh", "-c", "/bin/cat /etc/hosts"], isolated),
                                    capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)

    def test_reports_nonzero_exit(self) -> None:
        with patch.object(
            grok_client.subprocess,
            "run",
            return_value=CompletedProcess([], 1, stdout="", stderr="not signed in"),
        ), patch.object(grok_client, "_fallback", side_effect=lambda result, *_: result):
            result = grok_client.run_text_prompt("prompt", Path.cwd())
        self.assertFalse(result["ok"])
        self.assertIn("not signed in", result["error"])

    def test_schema_is_sent_only_when_requested(self) -> None:
        schema = {"type": "object", "properties": {"result": {"type": "string"}}}
        output = '{"text":"{}","stopReason":"end_turn"}'
        with patch.object(grok_client.subprocess, "run", return_value=CompletedProcess([], 0, stdout=output, stderr="")) as run:
            grok_client.run_text_prompt("prompt", Path.cwd(), json_schema=schema)
        args = run.call_args.args[0]
        import json
        self.assertEqual(schema, json.loads(args[args.index('--json-schema') + 1]))


if __name__ == "__main__":
    unittest.main()
