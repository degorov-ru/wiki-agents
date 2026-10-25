import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import model_client
import claude_client
import install
import utils
import query
from import_source import import_source
import memory_contract
from memory_contract import apply_proposal, parse_proposal, redact_sensitive, recover_pending_commit


class MemoryContractTests(unittest.TestCase):
    def _article(self, title, *, status="active", sources="  - daily/2026-09-17.md#e1", extra="", kind="decision"):
        return f"---\ntitle: {title}\nkind: {kind}\nstatus: {status}\nupdated: 2026-09-17\nsources:\n{sources}\n{extra}---\n\n# {title}\n"

    def _daily(self, project, content="### e1 | Session\n"):
        path = project / "daily" / "2026-09-17.md"
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")

    def test_publication_rejects_changed_article_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "wiki/concepts/example.md"
            path.parent.mkdir(parents=True)
            path.write_text(self._article("User edit"))
            proposal = {"changes": [{"path": "wiki/concepts/example.md", "content": self._article("Model edit")}]}
            with self.assertRaisesRegex(ValueError, "changed during model run"):
                apply_proposal(root, proposal, expected_versions={"wiki/concepts/example.md": "stale"})
            self.assertEqual(self._article("User edit"), path.read_text())

    def test_legacy_missing_source_is_preserved_but_new_missing_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._daily(root)
            path = root / "wiki/concepts/example.md"
            path.parent.mkdir(parents=True)
            old = self._article("Old", sources="  - daily/missing.md")
            path.write_text(old)
            updated = self._article("Updated", sources="  - daily/missing.md\n  - daily/2026-09-17.md#e1")
            self.assertEqual(1, apply_proposal(root, {"changes": [{"path": "wiki/concepts/example.md", "content": updated}]}))
            with self.assertRaisesRegex(ValueError, "source does not exist"):
                memory_contract.validate_article_semantics(root, path, path.read_text())
            invalid = updated.replace("  - daily/missing.md", "  - daily/missing.md\n  - daily/invented.md")
            with self.assertRaisesRegex(ValueError, "source does not exist"):
                apply_proposal(root, {"changes": [{"path": "wiki/concepts/example.md", "content": invalid}]})
            self.assertEqual(updated, path.read_text())

    def test_generated_index_obeys_model_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._daily(root)
            index = root / "wiki/index.md"
            index.parent.mkdir()
            index.write_text("Human catalog edit")
            with self.assertRaisesRegex(ValueError, "changed during model run"):
                apply_proposal(root, {"changes": [{"path": "wiki/concepts/new.md", "content": self._article("New")}]},
                               expected_versions={"wiki/index.md": None})
            self.assertEqual("Human catalog edit", index.read_text())
            self.assertFalse((root / "wiki/concepts/new.md").exists())

    def test_missing_engine_never_selects_provider(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(model_client, "CONFIG_PATH", Path(tmp) / "missing"):
            with self.assertRaisesRegex(ValueError, "not configured"):
                model_client.load_engines()

    def test_model_boundary_rejects_write_tools(self):
        with self.assertRaisesRegex(ValueError, "cannot receive tools"):
            model_client._run("luna", "x", Path.cwd(), 1, ("Write",))

    def test_dispatcher_redacts_all_prompt_sources(self):
        with patch.object(model_client, "load_engines", return_value=("luna", None)), patch.object(model_client, "_attempt", return_value={"ok": True, "text": "ok"}) as attempt:
            result = model_client.run_text_prompt("PASSWORD=boundary-secret", Path.cwd())
        self.assertNotIn("boundary-secret", attempt.call_args.args[1])
        self.assertTrue(result["redacted"])

    def test_secret_is_redacted_before_prompt(self):
        text, changed = redact_sensitive("API key: sk-1234567890abcdef\nAuthorization: Bearer abcdefghijk\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
        self.assertTrue(changed)
        self.assertNotIn("sk-123", text)
        self.assertNotIn("Bearer", text)
        self.assertNotIn("PRIVATE KEY", text)

    def test_contact_redaction_preserves_dates_order_ids_and_next_json_line(self):
        text = 'Email: private@example.test\nPhone: +7 (999) 123-45-67\nДата 2026-10-14, заказы 10065 и 10066.'
        safe, changed = redact_sensitive(text)
        self.assertTrue(changed)
        self.assertNotIn('private@example.test', safe)
        self.assertNotIn('123-45-67', safe)
        self.assertIn('2026-10-14', safe)
        self.assertIn('10065 и 10066', safe)
        safe_json, _ = redact_sensitive('ФИО: Private Person\\nImportant decision: keep facts')
        self.assertIn('Important decision: keep facts', safe_json)

    def test_rejects_escape_and_accepts_short_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            content = "---\ntitle: Decision\nkind: decision\nstatus: active\nupdated: 2026-09-17\nsources:\n  - daily/2026-09-17.md#e1\n---\n\n# Decision\n\nUse private wiki.\n"
            self.assertEqual(1, apply_proposal(project, parse_proposal('{"result":"changes","changes":[{"path":"wiki/concepts/private.md","content":' + __import__("json").dumps(content) + '}]}')))
            self.assertTrue((project / "wiki/concepts/private.md").exists())
            with self.assertRaisesRegex(ValueError, "unsafe|outside"):
                apply_proposal(project, {"result": "changes", "changes": [{"path": "../outside.md", "content": "x"}]})

    def test_article_update_refreshes_catalog_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            article = self._article('Foundation price') + '\nThickness has no effect.\n'
            apply_proposal(project, {'result': 'changes', 'changes': [{'path': 'wiki/concepts/price.md', 'content': article}]})
            translated = article.replace('Foundation price', 'Цена основания').replace('Thickness has no effect.', 'Толщина не влияет на цену.')
            apply_proposal(project, {'result': 'changes', 'changes': [{'path': 'wiki/concepts/price.md', 'content': translated}]})
            index = (project / 'wiki/index.md').read_text()
            self.assertIn('Толщина не влияет на цену.', index)
            self.assertNotIn('Thickness has no effect.', index)
            self.assertEqual(1, index.count('[[concepts/price]]'))

    def test_interrupted_commit_recovers_without_overwriting_user_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            first = self._article("First")
            second = self._article("Second")
            proposal = {"result": "changes", "changes": [
                {"path": "wiki/concepts/first.md", "content": first},
                {"path": "wiki/concepts/second.md", "content": second},
            ]}
            real_replace = memory_contract.os.replace
            writes = 0
            def interrupted(source, target):
                nonlocal writes
                if "transactions" in str(source):
                    writes += 1
                    if writes == 2:
                        raise OSError("synthetic crash")
                return real_replace(source, target)
            with patch.object(memory_contract.os, "replace", side_effect=interrupted):
                with self.assertRaisesRegex(OSError, "synthetic"):
                    apply_proposal(project, proposal)
            self.assertTrue((project / ".cmc/pending-commit.json").exists())
            self.assertEqual(4, recover_pending_commit(project))
            self.assertTrue((project / "wiki/concepts/second.md").exists())

    def test_semantic_acceptance_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            old = self._article("Old", status="superseded", extra="superseded_by: [[concepts/new]]\n")
            new = self._article("New", extra="scope:\n  - environment:production\nsupersedes: [[concepts/old]]\n")
            proposal = {"result": "changes", "changes": [
                {"path": "wiki/concepts/old.md", "content": old},
                {"path": "wiki/concepts/new.md", "content": new},
            ]}
            self.assertEqual(2, apply_proposal(project, proposal))  # superseded decision + env scope
            cancelled = self._article("Cancelled", status="cancelled")
            self.assertEqual(1, apply_proposal(project, {"result": "changes", "changes": [{"path": "wiki/concepts/cancelled.md", "content": cancelled}]}))
            cancelled_with_replacement = self._article("Cancelled wrong", status="cancelled", extra="superseded_by: [[concepts/new]]\n")
            with self.assertRaisesRegex(ValueError, "cancelled"):
                apply_proposal(project, {"result": "changes", "changes": [{"path": "wiki/concepts/cancelled-wrong.md", "content": cancelled_with_replacement}]})
            rejected = {"result": "changes", "changes": [{"path": "wiki/concepts/bad.md", "content": self._article("Bad", status="rejected")}]}
            with self.assertRaisesRegex(ValueError, "status"):
                apply_proposal(project, rejected)  # rejected proposal
            missing = {"result": "changes", "changes": [{"path": "wiki/concepts/missing.md", "content": self._article("Missing", sources="  - daily/2026-09-17.md#absent")}]}
            with self.assertRaisesRegex(ValueError, "anchor"):
                apply_proposal(project, missing)
            duplicate = {"result": "changes", "changes": [{"path": "wiki/concepts/duplicate.md", "content": self._article("Duplicate", sources="  - daily/2026-09-17.md#e1\n  - daily/2026-09-17.md#e1")}]}
            with self.assertRaisesRegex(ValueError, "duplicate"):
                apply_proposal(project, duplicate)
            stale_qa = self._article("Q", kind="fact", extra="derived_from:\n  - [[concepts/old]]\n")
            with self.assertRaisesRegex(ValueError, "stale"):
                apply_proposal(project, {"result": "changes", "changes": [{"path": "wiki/qa/q.md", "content": stale_qa}]})

    def test_duplicate_frontmatter_and_history_loss_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            duplicate = self._article("Dup").replace("status: active", "status: active\nstatus: cancelled")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                memory_contract.validate_article(duplicate)
            original = self._article("Durable", sources="  - daily/2026-09-17.md#e1\n  - daily/2026-09-17.md")
            apply_proposal(project, {"result": "changes", "changes": [{"path": "wiki/concepts/durable.md", "content": original}]})
            with self.assertRaisesRegex(ValueError, "drop provenance"):
                apply_proposal(project, {"result": "changes", "changes": [{"path": "wiki/concepts/durable.md", "content": self._article("Durable")}]})

    def test_generated_catalog_does_not_truncate_a_wikilink_into_a_fake_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            content = self._article('Rule') + '\n' + 'a' * 185 + ' [[concepts/a-long-article-reference]] is context.\n'
            self.assertEqual(1, apply_proposal(project, {'result': 'changes', 'changes': [
                {'path': 'wiki/concepts/rule.md', 'content': content}]}))
            index = (project / 'wiki/index.md').read_text()
            self.assertIn('[[concepts/rule]]', index)
            self.assertNotIn('[[concepts/a-long', index)

    def test_legacy_provenance_cannot_be_invented_or_hide_canonical_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            path = project / 'wiki/concepts/old.md'
            path.parent.mkdir(parents=True)
            old = self._article('Old', sources='  - daily/2026-09-17.md#e1\n  - reports/old.md')
            path.write_text(old)
            fixed = self._article('Old', extra='legacy_sources:\n  - reports/old.md\n')
            proposal = {'result': 'changes', 'changes': [{'path': 'wiki/concepts/old.md', 'content': fixed}]}
            self.assertEqual(1, apply_proposal(project, proposal))
            with self.assertRaisesRegex(ValueError, 'unvalidated legacy'):
                memory_contract.validate_article_semantics(project, path, fixed)
            for ref in ('reports/invented.md', 'daily/2026-09-17.md#e1'):
                proposal['changes'][0]['content'] = self._article('Old', extra='legacy_sources:\n  - reports/old.md\n  - ' + ref + '\n')
                with self.assertRaisesRegex(ValueError, 'introduce unvalidated'):
                    apply_proposal(project, proposal)

    def test_query_labels_legacy_primary_and_excludes_legacy_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            wiki = project / 'wiki'
            content = self._article('Legacy', extra='legacy_sources:\n  - reports/old.md\n')
            for folder in ('concepts', 'qa'):
                path = wiki / folder / 'old.md'
                path.parent.mkdir(parents=True)
                path.write_text(content)
            with patch.object(query, 'KNOWLEDGE_DIR', wiki), patch.object(query, 'read_wiki_index', return_value=''):
                entries = query._current_index_entries()
            self.assertEqual(1, len(entries))
            self.assertIn('[status: unvalidated]', entries[0][0])
            with self.assertRaisesRegex(ValueError, 'QA cannot inherit'):
                apply_proposal(project, {'result': 'changes', 'changes': [
                    {'path': 'wiki/qa/old.md', 'content': content}]})

    def test_query_excludes_qa_when_derived_article_becomes_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            wiki = project / "wiki"
            concepts = wiki / "concepts"
            qa = wiki / "qa"
            concepts.mkdir(parents=True)
            qa.mkdir()
            (concepts / "source.md").write_text(self._article("Source", status="cancelled"), encoding="utf-8")
            derived = self._article("Answer", kind="fact", extra="derived_from:\n  - [[concepts/source]]\n")
            (qa / "answer.md").write_text(derived, encoding="utf-8")
            index = "| [[qa/answer]] | current answer | daily/x.md | 2026-09-18 |"
            with patch.object(query, "KNOWLEDGE_DIR", wiki), patch.object(query, "read_wiki_index", return_value=index):
                context, selected, candidates = query.build_query_context("current answer")
            self.assertEqual((0, 0), (selected, candidates))
            self.assertNotIn("# Answer", context)

    def test_commit_journal_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            state = project / ".cmc"
            state.mkdir()
            (state / "pending-commit.json").write_text(__import__("json").dumps({
                "transaction": "a" * 32,
                "files": [{"path": "wiki/index.md", "staged": "../outside",
                           "old_hash": None, "new_hash": "b" * 64}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "staged"):
                recover_pending_commit(project)

    def test_query_uses_index_matches_and_reports_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            wiki = project / "wiki"
            concepts = wiki / "concepts"
            concepts.mkdir(parents=True)
            rows = ["# Knowledge Base Index", ""]
            for n in range(7):
                slug = f"auth-{n}"
                (concepts / f"{slug}.md").write_text(self._article(slug), encoding="utf-8")
                rows.append(f"| [[concepts/{slug}]] | auth pattern {n} | daily/x.md | 2026-09-17 |")
            (wiki / "index.md").write_text("\n".join(rows), encoding="utf-8")
            with patch.object(query, "KNOWLEDGE_DIR", wiki), patch.object(query, "read_wiki_index", return_value="\n".join(rows)), patch.object(query, "increment_state_counter"), patch.object(__import__("model_client"), "run_text_prompt", return_value={"ok": True, "text": "answer"}):
                answer = query.run_query("auth pattern")
            self.assertTrue(answer.startswith("Retrieval truncated: selected 6 of 7"))

    def test_large_index_cannot_crowd_out_article_bodies(self):
        body = self._article("Work scenarios") + "\nDestination: /sko2/documents/\n"
        entries = [("| work-scenarios " + "x" * 60_000, "concepts/scenarios", body)]
        entries.append(("| unrelated index-only claim", "concepts/other", "unrelated body"))
        with patch.object(query, "_current_index_entries", return_value=entries):
            context, selected, candidates = query.build_query_context("work-scenarios")
        self.assertEqual((selected, candidates), (1, 1))
        self.assertIn("Destination: /sko2/documents/", context)
        self.assertNotIn("index-only claim", context)
        self.assertLessEqual(len(context), query.MAX_QUERY_CONTEXT_CHARS)

    def test_mixed_language_hyphenated_query_retrieves_evidence(self):
        entries = [("Cookie Consent Metrika Gate", "concepts/consent", "cookie evidence"),
                   ("Contacts metrics-id", "concepts/contacts", "contact evidence")]
        with patch.object(query, "_current_index_entries", return_value=entries):
            context, selected, candidates = query.build_query_context("Кнопки cookie-баннера, metrics-id пуст?")
        self.assertEqual((selected, candidates), (2, 2))
        self.assertIn("cookie evidence", context)

    def test_query_reads_unindexed_article_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / 'wiki'
            (wiki / 'concepts').mkdir(parents=True)
            (wiki / 'concepts/price.md').write_text(
                self._article('Foundation price') + '\nТолщина основания не меняет цену.\n')
            with patch.object(query, 'KNOWLEDGE_DIR', wiki), patch.object(query, 'read_wiki_index', return_value=''):
                context, selected, _ = query.build_query_context('Влияет ли толщина основания на цену?')
            self.assertEqual(1, selected)
            self.assertIn('не меняет цену', context)

    def test_question_filler_does_not_block_fallback(self):
        with patch.object(query, '_current_index_entries', return_value=[
                ('Как сейчас', 'concepts/irrelevant', 'Nothing relevant')]):
            _, selected, candidates = query.build_query_context('Как сейчас считается полировка?')
        self.assertEqual((0, 0), (selected, candidates))

    def test_explicit_fallback_cannot_be_displaced_by_keyword_matches(self):
        entries = [('needle', f'concepts/noise-{n}', 'needle') for n in range(7)]
        entries.append(('different language', 'concepts/answer', 'precise answer'))
        with patch.object(query, '_current_index_entries', return_value=entries):
            context, _, _ = query.build_query_context('needle', fallback_links=('concepts/answer',))
        self.assertIn('precise answer', context)

    def test_query_budget_counts_separators_and_skips_oversized_articles(self):
        entries = [("needle", "concepts/oversized", "x" * 1000),
                   ("needle", "concepts/a", "a" * 50),
                   ("needle", "concepts/b", "b" * 50)]
        with patch.object(query, "_current_index_entries", return_value=entries), \
                patch.object(query, "MAX_QUERY_CONTEXT_CHARS", 130):
            context, selected, candidates = query.build_query_context("needle")
        self.assertEqual((selected, candidates), (1, 3))
        self.assertLessEqual(len(context), 130)

    def test_query_includes_labelled_cancellation_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / "wiki"
            concepts = wiki / "concepts"
            concepts.mkdir(parents=True)
            active = self._article("Active service")
            cancelled = self._article("Cancelled service", status="cancelled")
            (concepts / "active.md").write_text(active, encoding="utf-8")
            (concepts / "cancelled.md").write_text(cancelled, encoding="utf-8")
            index = "\n".join((
                "| [[concepts/active]] | service current | daily/x.md | 2026-09-18 |",
                "| [[concepts/cancelled]] | service old | daily/x.md | 2026-09-18 |",
            ))
            with patch.object(query, "KNOWLEDGE_DIR", wiki), patch.object(query, "read_wiki_index", return_value=index):
                context, selected, candidates = query.build_query_context("service")
            self.assertEqual((2, 2), (selected, candidates))
            self.assertIn("Active service", context)
            self.assertIn("Cancelled service", context)
            self.assertIn("Article status: cancelled", context)
            self.assertIn("Article status: active", context)

    def test_russian_query_uses_bounded_cross_language_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            wiki = project / "wiki"
            concepts = wiki / "concepts"
            concepts.mkdir(parents=True)
            (concepts / "deploy.md").write_text(self._article("Blue-green deployment"), encoding="utf-8")
            index = "| [[concepts/deploy]] | blue-green release process | daily/x.md | 2026-09-18 |"
            responses = [
                {"ok": True, "text": '{"queries":["blue-green deployment release process"]}'},
                {"ok": True, "text": '{"articles":["concepts/deploy"]}'},
                {"ok": True, "text": "Используй blue-green [[concepts/deploy]]."},
                {"ok": True, "text": '{"supported":true,"issues":[]}'},
            ]
            with patch.object(query, "PROJECT_DIR", project), patch.object(query, "ROOT_DIR", project), patch.object(query, "KNOWLEDGE_DIR", wiki), patch.object(query, "read_wiki_index", return_value=index), patch.object(query, "increment_state_counter"), patch.object(model_client, "run_text_prompt", side_effect=responses):
                answer = query.run_query("Как выкатывать релиз без простоя?")
            self.assertIn("Cross-language full-text search selected 1", answer)
            self.assertIn("[[concepts/deploy]]", answer)

    def test_expansion_finds_body_when_catalog_omits_the_subject(self):
        entries = [('Overview', 'concepts/a', 'Article status: active\n\nInstallation labor is charged by covered area.'),
                   ('Layout algorithm', 'concepts/b', 'Geometry coordinates and spatial layout only.')]
        with patch.object(query, '_current_index_entries', return_value=entries), patch.object(model_client, 'run_text_prompt', return_value={
                'ok': True, 'text': '{"queries":["installation labor charged covered area"]}'}) as provider:
            links = query._select_fallback_links('Как рассчитывается стоимость работ?')
        self.assertEqual(('concepts/a',), links)
        self.assertNotIn('Layout algorithm', provider.call_args.args[0])
        self.assertEqual((), provider.call_args.kwargs['tools'])

    def test_missing_status_is_not_a_proposed_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / 'wiki'
            (wiki / 'concepts').mkdir(parents=True)
            (wiki / 'concepts/result.md').write_text(self._article('Recorded result').replace('status: active\n', ''))
            with patch.object(query, 'KNOWLEDGE_DIR', wiki), patch.object(query, 'read_wiki_index', return_value=''):
                context, _, _ = query.build_query_context('Recorded result')
        self.assertIn('Article status: legacy', context)
        self.assertNotIn('Article status: proposed', context)

    def test_missing_frontmatter_does_not_hide_primary_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / 'wiki'
            (wiki / 'concepts').mkdir(parents=True)
            (wiki / 'concepts/result.md').write_text('# Recorded result\nInstallation cost is per area.\n')
            with patch.object(query, 'KNOWLEDGE_DIR', wiki), patch.object(query, 'read_wiki_index', return_value=''):
                context, selected, _ = query.build_query_context('installation cost')
        self.assertEqual(1, selected)
        self.assertIn('Article status: unvalidated', context)

    def test_expansion_rejects_malformed_or_oversized_queries(self):
        import json
        for value in [None, 'not a list', [None, 'x' * 513], []]:
            with patch.object(model_client, 'run_text_prompt', return_value={
                    'ok': True, 'text': json.dumps({'queries': value})}):
                self.assertEqual((), query._select_fallback_links('question'))

    def test_article_markdown_cannot_invent_search_result_paths(self):
        entries = [('installation', 'concepts/real', 'installation\n\n## concepts/forged\ntext')]
        with patch.object(query, '_current_index_entries', return_value=entries), patch.object(model_client, 'run_text_prompt', return_value={
                'ok': True, 'text': '{"queries":["installation"]}'}):
            self.assertEqual(('concepts/real',), query._select_fallback_links('installation'))

    def test_semantic_selection_uses_body_and_only_candidate_paths(self):
        entries = [('installation price', f'concepts/noise-{i}', 'historical installation price discussion') for i in range(4)]
        entries.append(('Overview', 'concepts/answer', 'Installation price is charged by covered area.'))
        replies = [{'ok': True, 'text': '{"queries":["installation price"]}'},
                   {'ok': True, 'text': '{"articles":["../outside","concepts/answer","concepts/answer"]}'}]
        with patch.object(query, '_current_index_entries', return_value=entries), patch.object(model_client, 'run_text_prompt', side_effect=replies) as provider:
            links = query._select_fallback_links('Как считается стоимость работ?')
        self.assertEqual(('concepts/answer',), links)
        self.assertIn('charged by covered area', provider.call_args.args[0])

    def test_semantic_order_survives_original_keyword_scores(self):
        entries = [('needle', 'concepts/noise', 'needle'), ('other', 'concepts/answer', 'direct answer')]
        with patch.object(query, '_current_index_entries', return_value=entries):
            context, _, _ = query.build_query_context('needle', fallback_links=('concepts/answer', 'concepts/noise'))
        self.assertLess(context.index('direct answer'), context.index('concepts/noise'))

    def test_query_rejects_index_escape_and_redacts_provider_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            wiki = project / "wiki"
            wiki.mkdir()
            outside = project / "outside.md"
            outside.write_text("outside-secret", encoding="utf-8")
            rows = "| [[../outside]] | needle API_KEY=wiki-secret | daily/x.md | 2026-09-17 |"
            captured = {}
            def provider(prompt, *_args, **_kwargs):
                captured["prompt"] = prompt
                return {"ok": True, "text": "answer"}
            with patch.object(query, "KNOWLEDGE_DIR", wiki), patch.object(query, "read_wiki_index", return_value=rows), patch.object(query, "increment_state_counter"), patch.object(__import__("model_client"), "run_text_prompt", side_effect=provider):
                answer = query.run_query("needle API_KEY=question-secret")
            self.assertNotIn("outside-secret", captured["prompt"])
            self.assertNotIn("wiki-secret", captured["prompt"])
            self.assertNotIn("question-secret", captured["prompt"])
            self.assertIn("Sensitive values were redacted", answer)

    def test_query_file_back_saves_only_source_validated_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            wiki = project / "wiki"
            concepts = wiki / "concepts"
            concepts.mkdir(parents=True)
            article = self._article("Deploy")
            (concepts / "deploy.md").write_text(article, encoding="utf-8")
            index = "# Wiki Index\n\n| Article | Summary | Source | Updated |\n|---|---|---|---|\n| [[concepts/deploy]] | deploy procedure | daily/2026-09-17.md#e1 | 2026-09-17 |\n"
            (wiki / "index.md").write_text(index, encoding="utf-8")
            (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
            provider = {"ok": True, "text": "Use the current procedure [[concepts/deploy]]."}
            with patch.object(query, "PROJECT_DIR", project), patch.object(query, "ROOT_DIR", project), patch.object(query, "KNOWLEDGE_DIR", wiki), patch.object(query, "read_wiki_index", return_value=index), patch.object(query, "increment_state_counter"), patch.object(model_client, "run_text_prompt", side_effect=lambda *a, **k: {"ok": True, "text": '{"supported":true,"issues":[]}'} if k.get("json_schema", {}).get("properties", {}).get("supported") else provider):
                answer = query.run_query("Which deploy procedure?", file_back=True)
            qa_files = list((wiki / "qa").glob("*.md"))
            self.assertEqual(1, len(qa_files))
            saved = qa_files[0].read_text(encoding="utf-8")
            self.assertIn("derived_from:\n  - [[concepts/deploy]]", saved)
            self.assertIn("daily/2026-09-17.md#e1", saved)
            self.assertIn("Filed derived Q&A", answer)

    def test_rejects_source_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "daily").mkdir()
            (project / "outside.md").write_text("### e1 | outside\n", encoding="utf-8")
            content = self._article("Escape", sources="  - daily/../outside.md#e1")
            with self.assertRaisesRegex(ValueError, "unsafe source"):
                apply_proposal(project, {"result": "changes", "changes": [{"path": "wiki/concepts/escape.md", "content": content}]})

    def test_legacy_source_annotations_remain_traceable_when_updating(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._daily(project)
            old = self._article('Legacy', sources='  - daily/2026-09-17.md (Session 19:45)')
            apply_proposal(project, {'result': 'changes', 'changes': [{'path': 'wiki/concepts/legacy.md', 'content': old}]})
            canonical = self._article('Legacy updated', sources='  - daily/2026-09-17.md')
            self.assertEqual(1, apply_proposal(project, {'result': 'changes', 'changes': [
                {'path': 'wiki/concepts/legacy.md', 'content': canonical}]}))
            invalid = self._article('Invalid', sources='  - daily/missing.md (Session 19:45)')
            with self.assertRaisesRegex(ValueError, 'source does not exist'):
                apply_proposal(project, {'result': 'changes', 'changes': [{'path': 'wiki/concepts/invalid.md', 'content': invalid}]})

    def test_rejects_mixed_valid_and_unsafe_sources(self):
        content = self._article("Mixed", sources="  - daily/2026-09-17.md#e1\n  - /outside.md")
        with self.assertRaisesRegex(ValueError, "every article source"):
            memory_contract.validate_article(content)

    def test_latest_daily_and_source_import_are_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            daily = project / "daily"
            daily.mkdir()
            (daily / "2026-09-16.md").write_text("old", encoding="utf-8")
            (daily / "2026-09-17.md").write_text("new", encoding="utf-8")
            with patch.object(utils, "DAILY_DIR", daily):
                self.assertEqual("2026-09-17.md", utils.latest_daily_log().name)
            source = project / "note.txt"
            source.write_text("source", encoding="utf-8")
            self.assertTrue(import_source(source, project)["imported"])
            self.assertFalse(import_source(source, project)["imported"])
            unsupported = project / "note.pdf"
            unsupported.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "only"):
                import_source(unsupported, project)

    def test_noop_has_no_writes(self):
        self.assertEqual(0, apply_proposal(Path(tempfile.gettempdir()), parse_proposal('{"result":"noop","reason":"no durable knowledge","changes":[]}')))

    def test_plain_done_is_not_a_compile_result(self):
        with self.assertRaisesRegex(ValueError, "not JSON"):
            parse_proposal("Done")

    def test_state_merge_keeps_concurrent_ingest(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(utils, "STATE_FILE", Path(tmp) / "state.json"):
            utils.save_state({"ingested": {"new.md": {"hash": "abc"}}, "query_count": 0})
            stale = utils.load_state()
            utils.save_state({"ingested": {"later.md": {"hash": "def"}}, "query_count": 0})
            stale["query_count"] += 1
            utils.save_state(stale)
            self.assertIn("later.md", utils.load_state()["ingested"])

    def test_state_counter_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(utils, "STATE_FILE", Path(tmp) / "state.json"):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: utils.increment_state_counter("query_count"), range(40)))
            self.assertEqual(40, utils.load_state()["query_count"])

    def test_wiki_listing_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            concepts = project / "wiki/concepts"
            concepts.mkdir(parents=True)
            outside = project / "outside.md"
            outside.write_text("secret", encoding="utf-8")
            (concepts / "link.md").symlink_to(outside)
            with patch.object(utils, "CONCEPTS_DIR", concepts), patch.object(utils, "CONNECTIONS_DIR", project / "wiki/connections"), patch.object(utils, "QA_DIR", project / "wiki/qa"):
                self.assertEqual([], utils.list_wiki_articles())

    def test_installer_quotes_shell_metacharacters(self):
        with patch.object(install, "TOOL_ROOT", Path("/tmp/clone-$(bad)")):
            command = install._command("session-start.py")
        self.assertIn("'", command)
        self.assertIn("$(bad)", command)

    def test_haiku_error_is_not_success_and_cost_is_preserved(self):
        response = __import__("subprocess").CompletedProcess([], 0, '{"result":"Done","is_error":true,"total_cost_usd":1.23}', "")
        with patch.object(claude_client.subprocess, "run", return_value=response):
            result = claude_client.run_haiku("Synthetic", Path.cwd())
        self.assertFalse(result["ok"])
        self.assertEqual(1.23, result["cost_usd"])
        self.assertEqual("Done", result["error"])


if __name__ == "__main__":
    unittest.main()
