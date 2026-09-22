from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("manage_test", ROOT / "scripts" / "manage.py")
MANAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANAGE)


class ManageTests(unittest.TestCase):
    def test_legacy_metadata_preview_apply_repeat_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "daily").mkdir()
            (project / "daily/2026-01-01.md").write_text("Evidence\n")
            article = project / "wiki/concepts/old.md"
            article.parent.mkdir(parents=True)
            original = "---\ntitle: Legacy\nupdated: 2026-01-01\nsources:\n  - daily/2026-01-01.md\n---\n\nExact old text.\n"
            article.write_text(original)
            preview = MANAGE.migrate_articles(project)
            self.assertEqual("PREVIEW", preview["status"])
            self.assertFalse((project / ".cmc").exists())
            self.assertEqual(original, article.read_text())
            result = MANAGE.migrate_articles(project, apply=True)
            migrated = article.read_text()
            self.assertIn("kind: hypothesis\nstatus: proposed\n", migrated)
            self.assertEqual(original, migrated.replace("kind: hypothesis\nstatus: proposed\n", "", 1))
            self.assertEqual("NOOP", MANAGE.migrate_articles(project, apply=True)["status"])
            MANAGE.rollback(project, Path(result["backup"]))
            self.assertEqual(original, article.read_text())

    def test_legacy_metadata_invalid_source_refuses_entire_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            article = project / "wiki/concepts/old.md"
            article.parent.mkdir(parents=True)
            original = "---\ntitle: Legacy\nupdated: 2026-01-01\nsources:\n  - daily/missing.md\n---\nbody\n"
            article.write_text(original)
            result = MANAGE.migrate_articles(project, apply=True)
            self.assertEqual("BLOCKED", result["status"])
            self.assertIn("source does not exist", result["rejected"][0]["reason"])
            self.assertEqual(original, article.read_text())
            self.assertFalse((project / ".cmc").exists())

    def test_upgrade_and_rollback_restore_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".cmc").mkdir()
            (project / "wiki").mkdir()
            version = project / ".cmc/version.json"
            version.write_text('{"product_version":"old","schema_version":0}\n', encoding="utf-8")
            result = MANAGE.upgrade(project)
            self.assertEqual(1, json.loads(version.read_text())["schema_version"])
            MANAGE.rollback(project, Path(result["backup"]))
            self.assertEqual("old", json.loads(version.read_text())["product_version"])
            self.assertFalse((project / "CLAUDE.md").exists())

    def test_uninstall_removes_only_own_hook_and_preserves_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            hooks = project / ".codex/hooks.json"
            hooks.parent.mkdir()
            wiki = project / "wiki/index.md"
            wiki.parent.mkdir()
            wiki.write_text("memory", encoding="utf-8")
            own = f"uv run --directory {MANAGE.TOOL_ROOT} python hooks/codex-stop.py"
            hooks.write_text(json.dumps({"hooks":{"Stop":[{"hooks":[{"command":own},{"command":"same-entry"}]},{"hooks":[{"command":"other"}]}]}}), encoding="utf-8")
            result = MANAGE.uninstall_project(project)
            data = json.loads(hooks.read_text())
            self.assertEqual("UNINSTALLED", result["status"])
            self.assertEqual(["same-entry", "other"], [entry["hooks"][0]["command"] for entry in data["hooks"]["Stop"]])
            self.assertTrue(wiki.exists())

    def test_pause_resume_use_tool_local_marker(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(MANAGE, "DISABLED", Path(tmp) / "disabled"):
            self.assertEqual("PAUSED", MANAGE.pause()["status"])
            self.assertEqual("ACTIVE", MANAGE.resume()["status"])

    def test_uninstall_removes_stale_hook_from_old_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            hooks = project / ".codex/hooks.json"
            hooks.parent.mkdir()
            hooks.write_text(json.dumps({"hooks":{"Stop":[{"hooks":[{"command":"uv run --directory /old/copy python hooks/codex-stop.py"}]}]}}))
            MANAGE.uninstall_project(project)
            self.assertEqual([], json.loads(hooks.read_text())["hooks"]["Stop"])

    def test_global_uninstall_removes_only_memory_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
                {"command": "uv run --directory /old python hooks/session-start.py"},
                {"command": "unrelated"},
            ]}], "PreCompact": [{"hooks": [{"command": "x hooks/pre-compact.py"}]}],
                "SessionEnd": [{"hooks": [{"command": "x hooks/session-end.py"}]}]}}), encoding="utf-8")
            result = MANAGE.uninstall_claude(settings)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(3, result["removed"])
            self.assertEqual("unrelated", data["hooks"]["SessionStart"][0]["hooks"][0]["command"])

    def test_doctor_points_codex_desktop_to_hook_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = MANAGE.doctor(Path(tmp))

        trust = next(check for check in result["checks"] if check["name"] == "codex_hook_trust")
        self.assertIn("Settings > Hooks", trust["fix"])
        self.assertNotIn("/hooks", trust["fix"])


if __name__ == "__main__":
    unittest.main()
