from __future__ import annotations

import sys
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

    def test_reports_nonzero_exit(self) -> None:
        with patch.object(
            grok_client.subprocess,
            "run",
            return_value=CompletedProcess([], 1, stdout="", stderr="not signed in"),
        ), patch.object(grok_client, "_fallback", side_effect=lambda result, *_: result):
            result = grok_client.run_text_prompt("prompt", Path.cwd())
        self.assertFalse(result["ok"])
        self.assertIn("not signed in", result["error"])


if __name__ == "__main__":
    unittest.main()
