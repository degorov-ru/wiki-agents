from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from migrate_agent_guides import migrate


class MigrationTests(unittest.TestCase):
    def test_preserves_rules_and_removes_legacy_files(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "AGENTS.md").write_text("Read `AGENT_GUIDE.md` in this project.\n")
            (project / "AGENT_GUIDE.md").write_text("# Rules\n\nKeep this.\n")
            (project / "CLAUDE.md").write_text("# Extra\n\nKeep that.\n")
            migrate(project, False)
            self.assertTrue((project / "AGENT_GUIDE.md").exists())
            migrate(project, True)
            agents = (project / "AGENTS.md").read_text()
            self.assertIn("Keep this.", agents)
            self.assertIn("Keep that.", agents)
            self.assertFalse((project / "AGENT_GUIDE.md").exists())
            self.assertFalse((project / "CLAUDE.md").exists())
            self.assertEqual("skip", migrate(project, True))

    def test_short_project_specific_entry_is_preserved(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "AGENTS.md").write_text("# Project\n\nSee AGENT_GUIDE.md.\n\n## Access\n\nUse host X.\n")
            (project / "AGENT_GUIDE.md").write_text("# Rules\n\nKeep this.\n")
            migrate(project, True)
            agents = (project / "AGENTS.md").read_text()
            self.assertIn("Use host X.", agents)
            self.assertIn("Keep this.", agents)
