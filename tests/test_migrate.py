from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("migrate_test", ROOT / "scripts/migrate_ai_memory.py")
MIGRATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATE)


class MigrationTests(unittest.TestCase):
    def test_nested_same_basename_keeps_both_with_legacy_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for folder, body in (("one", "first"), ("two", "second")):
                path = project / "ai-memory" / folder / "note.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            result = MIGRATE.migrate(project)
            self.assertEqual(2, result["migrated_files"])
            self.assertTrue((project / "wiki/concepts/one-note.md").exists())
            self.assertTrue((project / "wiki/concepts/two-note.md").exists())
            self.assertTrue((project / "sources/legacy-ai-memory/one/note.md").exists())
            self.assertIn("status: proposed", (project / "wiki/concepts/one-note.md").read_text())

    def test_existing_frontmatter_gets_contract_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "ai-memory/note.md"
            source.parent.mkdir()
            source.write_text("---\ntitle: Existing\n---\n\nBody\n", encoding="utf-8")
            MIGRATE.migrate(project)
            article = (project / "wiki/concepts/note.md").read_text()
            self.assertIn("kind: fact", article)
            self.assertIn("status: proposed", article)
            self.assertIn("sources/legacy-ai-memory/note.md", article)

    def test_interrupted_publish_finishes_source_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "ai-memory").mkdir()
            (project / "ai-memory/note.md").write_text("body")
            (project / "wiki").mkdir()
            (project / ".cmc").mkdir()
            (project / ".cmc/migration.json").write_text('{"schema_from":0,"schema_to":1}')
            result = MIGRATE.migrate(project)
            self.assertTrue(result["recovered"])
            self.assertTrue((project / "ai-memory.migrated/note.md").exists())
            self.assertFalse((project / ".cmc/migration.json").exists())


if __name__ == "__main__":
    unittest.main()
