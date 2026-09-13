from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import utils


class WikilinkResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wiki = Path(self.tmp.name) / "wiki"
        self.concepts = self.wiki / "concepts"
        self.connections = self.wiki / "connections"
        self.qa = self.wiki / "qa"
        for path in (self.concepts, self.connections, self.qa):
            path.mkdir(parents=True)
        self.article = self.concepts / "target.md"
        self.article.write_text("# Target\n", encoding="utf-8")
        self.patchers = [
            patch.object(utils, "KNOWLEDGE_DIR", self.wiki),
            patch.object(utils, "PROJECT_DIR", self.wiki.parent),
            patch.object(utils, "CONCEPTS_DIR", self.concepts),
            patch.object(utils, "CONNECTIONS_DIR", self.connections),
            patch.object(utils, "QA_DIR", self.qa),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_resolves_bare_link_beside_source(self) -> None:
        source = self.concepts / "source.md"
        self.assertEqual(utils.resolve_wikilink("target", source), self.article.resolve())

    def test_resolves_rooted_link_with_alias_and_heading(self) -> None:
        self.assertEqual(
            utils.resolve_wikilink("concepts/target#Details|label"),
            self.article.resolve(),
        )

    def test_counts_alias_as_inbound_link(self) -> None:
        source = self.concepts / "source.md"
        source.write_text("See [[concepts/target|Target]].\n", encoding="utf-8")
        self.assertEqual(utils.count_inbound_links("concepts/target"), 1)

    def test_resolves_project_root_note(self) -> None:
        evidence = self.wiki.parent / "evidence" / "case.md"
        evidence.parent.mkdir()
        evidence.write_text("# Evidence\n", encoding="utf-8")
        self.assertEqual(utils.resolve_wikilink("evidence/case"), evidence.resolve())


if __name__ == "__main__":
    unittest.main()
