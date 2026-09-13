import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
model_client = importlib.import_module("model_client")


class ModelClientTests(unittest.TestCase):
    def test_falls_back_to_configured_engine(self):
        failed = {"ok": False, "engine": "grok", "error": "unavailable"}
        passed = {"ok": True, "engine": "luna", "text": "ok"}
        with patch.object(model_client, "load_engines", return_value=("grok", "luna")), \
             patch.object(model_client, "_run", side_effect=[failed, passed]) as run, \
             patch("alerts.notify"):
            result = model_client.run_text_prompt("prompt", Path.cwd())

        self.assertTrue(result["ok"])
        self.assertEqual(result["fallback_from"], "grok")
        self.assertEqual([call.args[0] for call in run.call_args_list], ["grok", "luna"])


if __name__ == "__main__":
    unittest.main()
