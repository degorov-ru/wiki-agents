from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "init_project.py"
spec = importlib.util.spec_from_file_location("init_project_test", SCRIPT)
initializer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(initializer)


class InitProjectTests(unittest.TestCase):
    def test_agents_is_canonical_and_claude_points_to_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            initializer.init(project)
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("single source of truth", agents)
            self.assertIn("Read `AGENTS.md`", claude)
            self.assertFalse((project / "AGENT_GUIDE.md").exists())


if __name__ == "__main__":
    unittest.main()
