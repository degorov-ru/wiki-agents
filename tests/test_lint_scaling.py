from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lint
import utils


class OrphanPageScalingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.wiki = self.project / "wiki"
        self.concepts = self.wiki / "concepts"
        self.connections = self.wiki / "connections"
        self.qa = self.wiki / "qa"
        for path in (self.concepts, self.connections, self.qa):
            path.mkdir(parents=True)
        self.patchers = [
            patch.object(lint, "KNOWLEDGE_DIR", self.wiki),
            patch.object(utils, "KNOWLEDGE_DIR", self.wiki),
            patch.object(utils, "PROJECT_DIR", self.project),
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

    def _write_chain(self, count: int) -> list[Path]:
        articles = []
        for number in range(count):
            article = self.concepts / f"article-{number}.md"
            if number + 1 < count - 1:
                link = f"[[concepts/article-{number + 1}]]"
            elif number == count - 2:
                link = f"[[concepts/article-{number}]]"
            else:
                link = ""
            article.write_text(f"# Article {number}\\n{link}\\n", encoding="utf-8")
            articles.append(article)
        return articles

    def _legacy_orphans(self, articles: list[Path]) -> list[str]:
        issues = []
        for article in articles:
            target = str(article.relative_to(self.wiki).with_suffix(""))
            if utils.count_inbound_links(target) == 0:
                issues.append(str(article.relative_to(self.wiki)))
        return issues

    def test_matches_legacy_results_with_linear_link_resolution(self) -> None:
        articles = self._write_chain(30)
        expected = self._legacy_orphans(articles)
        resolve_calls = 0
        list_calls = 0
        original_resolve = lint.resolve_wikilink
        original_list = lint.list_wiki_articles

        def count_resolve(*args, **kwargs):
            nonlocal resolve_calls
            resolve_calls += 1
            return original_resolve(*args, **kwargs)

        def count_list():
            nonlocal list_calls
            list_calls += 1
            return original_list()

        with patch.object(lint, "resolve_wikilink", count_resolve), patch.object(
            lint, "list_wiki_articles", count_list
        ):
            actual = [issue["file"] for issue in lint.check_orphan_pages()]

        self.assertEqual(actual, expected)
        self.assertEqual(resolve_calls, len(articles) - 1)
        self.assertEqual(list_calls, 1)

    def test_stale_import_keeps_sources_path(self):
        source = self.project / "sources/example.md"
        with patch.object(lint, "load_state", return_value={"ingested": {"sources/example.md": {"hash": "old"}}}), \
                patch.object(lint, "list_raw_files", return_value=[]), \
                patch.object(lint, "list_source_files", return_value=[source]), \
                patch.object(lint, "file_hash", return_value="new"):
            self.assertEqual("sources/example.md", lint.check_stale_articles()[0]["file"])


if __name__ == "__main__":
    unittest.main()
