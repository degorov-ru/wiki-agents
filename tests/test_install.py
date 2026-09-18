import importlib.util
import json
import tempfile
import unittest
import io
import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
SPEC = importlib.util.spec_from_file_location("install", Path(__file__).parents[1] / "scripts" / "install.py")
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class InstallTests(unittest.TestCase):
    def test_claude_hook_install_is_idempotent_and_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text('{"theme":"dark","hooks":{"SessionStart":[{"hooks":[{"command":"uv run --directory /old python hooks/session-start.py"},{"command":"unrelated"}]}]}}', encoding="utf-8")
            INSTALL.install_claude_hooks(path)
            INSTALL.install_claude_hooks(path)
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["theme"], "dark")
        commands = [hook["command"] for entry in data["hooks"]["SessionStart"] for hook in entry["hooks"]]
        self.assertIn("unrelated", commands)
        self.assertFalse(any("/old" in command for command in commands))
        for event, (script, _) in INSTALL.HOOKS.items():
            own = [hook for entry in data["hooks"][event] for hook in entry["hooks"]
                   if f"hooks/{script}" in hook.get("command", "")]
            self.assertEqual(1, len(own))

    def test_engine_check_explains_missing_cli(self):
        with patch.object(INSTALL.shutil, "which", return_value=None):
            error = INSTALL._test_engine("luna")

        self.assertIn("codex", error)
        self.assertIn("повтори установку", error)

    def test_dry_run_has_no_writes_or_provider_calls(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(INSTALL, "TOOL_ROOT", Path(tmp) / "tool"), \
             patch.object(INSTALL, "install_claude_hooks") as hooks, \
             patch.object(INSTALL, "_test_engine") as engine, \
             patch.object(sys, "argv", ["install.py", "--engine", "luna", "--fallback", "none", "--project", tmp, "--dry-run"]), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(0, INSTALL.main())
        hooks.assert_not_called()
        engine.assert_not_called()
        self.assertIn('"status": "DRY_RUN"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
