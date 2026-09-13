import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("install", Path(__file__).parents[1] / "scripts" / "install.py")
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class InstallTests(unittest.TestCase):
    def test_claude_hook_install_is_idempotent_and_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text('{"theme":"dark","hooks":{}}', encoding="utf-8")
            INSTALL.install_claude_hooks(path)
            INSTALL.install_claude_hooks(path)
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["theme"], "dark")
        self.assertEqual({name: len(data["hooks"][name]) for name in INSTALL.HOOKS}, {name: 1 for name in INSTALL.HOOKS})

    def test_engine_check_explains_missing_cli(self):
        with patch.object(INSTALL.shutil, "which", return_value=None):
            error = INSTALL._test_engine("luna")

        self.assertIn("codex", error)
        self.assertIn("повтори установку", error)


if __name__ == "__main__":
    unittest.main()
