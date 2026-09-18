from __future__ import annotations

import importlib.util
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "init_project.py"
spec = importlib.util.spec_from_file_location("init_project_test", SCRIPT)
initializer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(initializer)


class InitProjectTests(unittest.TestCase):
    def test_fresh_init_is_byte_identical_on_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            initializer.init(project)
            before = {str(p.relative_to(project)): p.read_bytes() for p in project.rglob("*") if p.is_file()}
            initializer.init(project)
            after = {str(p.relative_to(project)): p.read_bytes() for p in project.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_agents_is_canonical_and_claude_points_to_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            initializer.init(project)
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("single source of truth", agents)
            self.assertIn("Read `AGENTS.md`", claude)
            self.assertFalse((project / "AGENT_GUIDE.md").exists())
            ignored = (project / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("wiki/", ignored)
            self.assertIn("ACCESS.md", ignored)
            self.assertIn(".cmc-config.json", ignored)
            self.assertFalse((project / ".obsidian").exists())

    def test_existing_agents_is_preserved_and_gets_one_managed_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            agents = project / "AGENTS.md"
            agents.write_text("# User rules\n\nKeep this.\n", encoding="utf-8")
            initializer.init(project)
            initializer.init(project)
            content = agents.read_text(encoding="utf-8")
            self.assertIn("Keep this.", content)
            self.assertEqual(1, content.count(initializer.AGENTS_BLOCK_START))

    def test_codex_hook_command_shell_quotes_tool_path(self) -> None:
        with patch.object(initializer.Path, "resolve", return_value=Path("/tmp/tool-$(bad)/scripts/init_project.py")):
            command = initializer._codex_stop_command()
        self.assertIn("'", command)
        self.assertIn("$(bad)", command)

    def test_existing_legacy_guides_keep_rules_and_get_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "CLAUDE.md").write_text("Claude user rule\n", encoding="utf-8")
            (project / "AGENT_GUIDE.md").write_text("Legacy user rule\n", encoding="utf-8")
            initializer.init(project)
            initializer.init(project)
            for name, rule in (("CLAUDE.md", "Claude user rule"), ("AGENT_GUIDE.md", "Legacy user rule")):
                content = (project / name).read_text(encoding="utf-8")
                self.assertIn(rule, content)
                self.assertEqual(1, content.count(initializer.POINTER_BLOCK_START))

    def test_init_replaces_stale_codex_hook_and_preserves_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            hooks = project / ".codex/hooks.json"
            hooks.parent.mkdir()
            hooks.write_text('{"hooks":{"Stop":[{"hooks":[{"command":"uv run --directory /old python hooks/codex-stop.py"},{"command":"keep"}]}]}}')
            initializer.init(project)
            commands = [hook["command"] for entry in __import__("json").loads(hooks.read_text())["hooks"]["Stop"] for hook in entry["hooks"]]
            self.assertEqual(1, sum("hooks/codex-stop.py" in command for command in commands))
            self.assertIn("keep", commands)


if __name__ == "__main__":
    unittest.main()
