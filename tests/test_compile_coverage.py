import importlib.util
import asyncio
import os
import tempfile
import subprocess
from unittest.mock import patch, AsyncMock
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('coverage_compiler', ROOT / 'scripts/compile.py')
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


class CompileCoverageTests(unittest.TestCase):
    def test_project_chunk_size_and_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / '.cmc-config.json').write_text(json.dumps({'chunk_chars': 12000}))
            env = dict(os.environ, CMC_PROJECT_DIR=str(project), PYTHONDONTWRITEBYTECODE='1')
            env.pop('CMC_CHUNK_CHARS', None)
            for override, expected in ((None, '12000'), ('6000', '6000')):
                if override is not None:
                    env['CMC_CHUNK_CHARS'] = override
                result = subprocess.run([sys.executable, '-c',
                    "import sys;sys.path.insert(0, 'scripts');import config;print(config.CHUNK_CHARS)"],
                    cwd=ROOT, env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, result.stdout.strip())

    def _legacy_content(self, body):
        return '---\ntitle: Recorded choice\nsources:\n  - daily/day.md\n---\n\n' + body


    def test_compiler_retries_recent_permission_failure_after_consent(self):
        import io
        from contextlib import redirect_stdout
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'day.md'
            source.write_text('decision')
            state = {'ingested': {'day.md': {'hash': compiler.file_hash(source), 'stage': 'permission',
                'failed_at': datetime.now(timezone.utc).isoformat()}}}
            with patch.object(compiler, 'DAILY_DIR', root), patch.object(compiler, 'load_state', return_value=state), \
                    patch.object(compiler, 'list_raw_files', return_value=[source]), \
                    patch.object(sys, 'argv', ['compile.py', '--daily-only', '--dry-run']):
                for allowed in (False, True):
                    out = io.StringIO()
                    with patch('model_client.wiki_context_allowed', return_value=allowed), redirect_stdout(out):
                        compiler.main()
                    self.assertEqual(allowed, 'Files to compile (1)' in out.getvalue())


    def test_compiler_catalog_is_owned_by_publication_code(self):
        proposal = {'result': 'changes', 'changes': [{'path': 'wiki/index.md',
            'content': '[[connections/invented]]'}, {'path': 'wiki/log.md', 'content': 'replaces build history'}]}
        normalized = compiler._finalize_metadata(proposal, {}, 'daily/day.md')
        self.assertEqual([], normalized['changes'])


    def test_repair_cannot_recreate_a_supplied_unpublished_draft(self):
        path = 'wiki/concepts/new.md'
        with self.assertRaisesRegex(ValueError, 'supplied draft'):
            compiler._apply_article_edits(json.dumps({'edits': [], 'creates': [{'path': path, 'content': 'Lost older draft facts'}]}),
                {path: '---\ntitle: Test\n---\nAlready collected facts'}, {})
        self.assertEqual({path: '---\ntitle: Test\n---\nAlready collected facts. New fact.'}, compiler._apply_article_edits(
            json.dumps({'edits': [{'path': path, 'old': 'Already collected facts', 'new': 'Already collected facts. New fact.'}], 'creates': []}),
            {path: '---\ntitle: Test\n---\nAlready collected facts'}, {}))


    def test_live_old_lock_is_not_stolen_and_foreign_lock_is_not_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.cmc').mkdir()
            lock = root / '.cmc/compile.lock'
            lock.write_text(f'pid={os.getpid()}\n')
            os.utime(lock, (1, 1))
            with patch.object(compiler, 'PROJECT_DIR', root), patch.dict(os.environ, {'CMC_COMPILE_LOCK': ''}):
                self.assertFalse(compiler._claim_compile_lock())
                lock.write_text('pid=999999999\n')
                compiler._release_compile_lock()
                self.assertTrue(lock.exists())


    def test_two_processes_recovering_a_dead_lock_have_one_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / '.cmc').mkdir()
            (root / '.cmc/compile.lock').write_text('pid=999999999\n')
            code = '''
import importlib.util, os, sys, time
from pathlib import Path
root, project = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / 'scripts'))
os.environ['CMC_PROJECT_DIR'] = str(project)
os.environ.pop('CMC_COMPILE_LOCK', None)
spec = importlib.util.spec_from_file_location('claim_test', root / 'scripts/compile.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
while not (project / 'start').exists(): time.sleep(.005)
won = c._claim_compile_lock()
print(int(won), flush=True)
if won:
    end = time.monotonic() + 5
    while not (project / 'release').exists() and time.monotonic() < end: time.sleep(.005)
    c._release_compile_lock()
'''
            processes = [subprocess.Popen([sys.executable, '-B', '-c', code, str(ROOT), str(root)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
            try:
                (root / 'start').touch()
                winners = [int(p.stdout.readline().strip()) for p in processes]
                self.assertEqual(1, sum(winners))
            finally:
                (root / 'release').touch()
                for process in processes:
                    _, errors = process.communicate(timeout=10)
                    self.assertEqual(0, process.returncode, errors)


    def test_background_job_limits_work_and_prioritizes_a_new_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            daily = root / 'daily'
            daily.mkdir()
            old, new = daily / '2026-01-01.md', daily / '2026-01-02.md'
            old.write_text('already compiled, not audited')
            new.write_text('new decisions')
            state = {'ingested': {old.name: {'hash': compiler.file_hash(old), 'compiled_at': '2026-01-01T12:00:00Z'}}}
            async def complete(path, state, **kwargs):
                state['ingested'][path.name] = {'coverage_verified': True, 'cost_usd': 0}
                return 0
            with patch.object(compiler, 'DAILY_DIR', daily), patch.object(compiler, 'load_state', return_value=state), \
                    patch.object(compiler, 'list_raw_files', return_value=[old, new]), \
                    patch.object(compiler, 'list_wiki_articles', return_value=[]), \
                    patch.object(compiler, '_claim_compile_lock', return_value=True), patch.object(compiler, '_release_compile_lock'), \
                    patch.object(compiler, 'compile_daily_log', new=AsyncMock(side_effect=complete)) as run, \
                    patch.object(sys, 'argv', ['compile.py', '--daily-only']), patch.dict(os.environ, {'CMC_QUEUE_COMPILE': '1'}):
                self.assertEqual(0, compiler.main())
            self.assertEqual(1, run.await_count)
            self.assertEqual(new, run.await_args.args[0])


    def test_normal_compile_skips_unchanged_archive_but_keeps_pending_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            daily = Path(temp) / 'daily'
            daily.mkdir()
            paths = [daily / name for name in ('archive.md', 'audit-failed.md', 'changed.md', 'new.md', 'retry.md')]
            for path in paths:
                path.write_text('decision')
            state = {'ingested': {path.name: {'hash': compiler.file_hash(path),
                     'compiled_at': '2026-01-01T12:00:00Z'} for path in paths[:3]}}
            state['ingested']['audit-failed.md'].update(failed_at='2026-01-02T12:00:00Z', stage='coverage')
            state['ingested']['retry.md'] = {'hash': compiler.file_hash(paths[4]),
                                           'failed_at': '2026-01-02T12:00:00Z'}
            paths[2].write_text('changed decision')
            async def complete(path, state):
                state['ingested'][path.name] = {'compiled_at': '2026-10-15', 'cost_usd': 0}
                return 0
            with patch.object(compiler, 'DAILY_DIR', daily), patch.object(compiler, 'load_state', return_value=state), \
                    patch.object(compiler, 'list_raw_files', return_value=paths), \
                    patch.object(compiler, 'list_wiki_articles', return_value=[]), \
                    patch.object(compiler, '_claim_compile_lock', return_value=True), patch.object(compiler, '_release_compile_lock'), \
                    patch.object(compiler, 'compile_daily_log', new=AsyncMock(side_effect=complete)) as run, \
                    patch.object(sys, 'argv', ['compile.py', '--daily-only']), patch.dict(os.environ, {'CMC_QUEUE_COMPILE': '0'}):
                self.assertEqual(0, compiler.main())
            self.assertEqual(paths[2:], [call.args[0] for call in run.await_args_list])


    def test_code_fills_legacy_metadata_without_translating_or_dropping_sources(self):
        previous = self._legacy_content('Keep English history.').replace('daily/day.md', 'daily/2026-01-01.md')
        updated = self._legacy_content('Keep English history. New decision documented.')
        proposal = {'result': 'changes', 'changes': [{'path': 'wiki/concepts/rule.md', 'content': updated}]}
        ready = compiler._finalize_metadata(proposal, {'concepts/rule.md': previous}, 'daily/2026-01-02.md')
        text = ready['changes'][0]['content']
        fields = compiler.article_metadata(text)
        self.assertEqual(['fact'], fields['kind'])
        self.assertEqual(['active'], fields['status'])
        self.assertIn('daily/2026-01-01.md', fields['sources'])
        self.assertIn('daily/2026-01-02.md', fields['sources'])
        self.assertTrue(text.endswith('Keep English history. New decision documented.'))


    def test_legacy_report_sources_remain_explicitly_unvalidated(self):
        previous = self._legacy_content('History.').replace('  - daily/day.md', '  - daily/day.md\n  - reports/inventory.md')
        proposal = {'result': 'changes', 'changes': [{'path': 'wiki/concepts/rule.md', 'content': previous}]}
        ready = compiler._finalize_metadata(proposal, {'concepts/rule.md': previous}, 'daily/day.md')
        fields = compiler.article_metadata(ready['changes'][0]['content'])
        self.assertEqual(['reports/inventory.md'], fields['legacy_sources'])
        self.assertEqual(['daily/day.md'], fields['sources'])
        compiler.validate_article(ready['changes'][0]['content'])
        fresh = compiler._finalize_metadata(proposal, {}, 'daily/day.md')['changes'][0]['content']
        self.assertNotIn('reports/inventory.md', fresh)
        self.assertEqual(['daily/day.md'], compiler.article_metadata(fresh)['sources'])


    def test_legacy_metadata_completion_preserves_explicit_terminal_status(self):
        previous = self._legacy_content('Cancelled rule.').replace('title: Recorded choice', 'title: Recorded choice\nstatus: cancelled')
        proposal = {'result': 'changes', 'changes': [{'path': 'wiki/concepts/rule.md', 'content': self._legacy_content('Cancelled rule.')} ]}
        ready = compiler._finalize_metadata(proposal, {'concepts/rule.md': previous}, 'daily/day.md')
        self.assertEqual(['cancelled'], compiler.article_metadata(ready['changes'][0]['content'])['status'])


    def test_exact_edits_preserve_every_unmentioned_old_fact(self):
        original = self._legacy_content('Old rule: blue.\n\nUnrelated rare setting: 4729.\n\nHistorical source and caveat remain.')
        response = {'edits': [{'path': 'wiki/concepts/rule.md', 'old': 'Old rule: blue.',
                    'new': 'Historical rule: blue. Current rule: orange.'}], 'creates': []}
        result = compiler._apply_article_edits(json.dumps(response), {'wiki/concepts/rule.md': original}, {'concepts/rule.md': original})
        self.assertEqual(original.replace('Old rule: blue.', 'Historical rule: blue. Current rule: orange.'), result['wiki/concepts/rule.md'])
        self.assertIn('Unrelated rare setting: 4729.', result['wiki/concepts/rule.md'])


    def test_ambiguous_and_whole_article_replacements_are_rejected(self):
        for original, old in [('same same', 'same'), ('a' * 2100, 'a' * 2100),
                              (self._legacy_content('Old fact. Caveat. position field.'),
                               self._legacy_content('Old fact. Caveat. position field.'))]:
            with self.subTest(size=len(old)), self.assertRaises(ValueError):
                compiler._apply_article_edits(json.dumps({'edits': [{'path': 'wiki/concepts/rule.md',
                    'old': old, 'new': 'shortened'}], 'creates': []}), {'wiki/concepts/rule.md': original}, {})


    def _reply(self, value):
        return {'ok': True, 'engine': 'fake', 'text': json.dumps(value)}

    def test_selection_rejects_invalid_and_over_budget_paths(self):
        for paths in ([True], 'concepts/a.md'):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                compiler._select_articles('source', {'concepts/a.md': 'old'}, lambda *a: self._reply({'articles': paths}))
        with patch.object(compiler, 'WIKI_CTX_MAX_CHARS', 1), self.assertRaisesRegex(ValueError, 'budget'):
            compiler._select_articles('source', {'concepts/a.md': 'old'}, lambda *a: self._reply({'articles': []}))

    def test_same_format_retry_uses_original_bases_and_stops_after_two_calls(self):
        base = self._legacy_content('Old fact. Rare setting: 4729.')
        path = 'wiki/concepts/a.md'
        prompts = []
        def call(prompt, phase, schema):
            prompts.append((prompt, phase, schema))
            return self._reply({'edits': [{'path': path, 'old': 'misquoted' if len(prompts) == 1 else 'Old fact.',
                                         'new': 'Historical fact. New fact.'}], 'creates': []})
        result = compiler._request_article_edits('Original bases: '+base, 'edit', {path: base},
            {'concepts/a.md': base}, call, 'daily/day.md')
        self.assertIn('Rare setting: 4729.', result['changes'][0]['content'])
        self.assertEqual(['edit', 'edit-correction'], [p[1] for p in prompts])
        self.assertTrue(all(p[2] == compiler.EDIT_SCHEMA for p in prompts))
        calls = []
        def invalid(*args):
            calls.append(args)
            return self._reply({'edits': [], 'creates': [{'path': path, 'content': base}]})
        with self.assertRaisesRegex(ValueError, 'existing article'):
            compiler._request_article_edits('base', 'edit', {path: base}, {'concepts/a.md': base}, invalid, 'daily/day.md')
        self.assertEqual(2, len(calls))

    def test_unread_article_cannot_be_edited_or_recreated(self):
        for reply in ({'edits': [{'path': 'wiki/concepts/a.md', 'old': 'Old', 'new': 'New'}], 'creates': []},
                      {'edits': [], 'creates': [{'path': 'wiki/concepts/a.md', 'content': 'New'}]}):
            with self.assertRaises(ValueError):
                compiler._apply_article_edits(json.dumps(reply), {}, {'concepts/a.md': 'Old'})

    def test_duplicate_creation_and_invalid_audit_are_rejected(self):
        item = {'path': 'wiki/concepts/a.md', 'content': self._legacy_content('Fact')}
        with self.assertRaises(ValueError):
            compiler._apply_article_edits(json.dumps({'edits': [], 'creates': [item, item]}), {}, {})
        for value in ({}, {'errors': [], 'warnings': 'none'}, {'errors': [True], 'warnings': []}):
            with self.assertRaises(ValueError):
                compiler._audit_proposal({'changes': []}, {}, 'daily/day.md', 'source', '', lambda *a: self._reply(value))

    def _run(self, root, provider, state, chunk_size=40000):
        from contextlib import ExitStack
        (root/'daily').mkdir(exist_ok=True); (root/'wiki/concepts').mkdir(parents=True, exist_ok=True)
        agents = root/'AGENTS.md'; agents.write_text('# Memory schema')
        with ExitStack() as stack:
            for key, value in {'PROJECT_DIR': root, 'ROOT_DIR': root, 'DAILY_DIR': root/'daily',
                'SOURCES_DIR': root/'sources', 'KNOWLEDGE_DIR': root/'wiki', 'AGENTS_FILE': agents,
                'CHUNK_CHARS': chunk_size}.items():
                stack.enter_context(patch.object(compiler, key, value))
            stack.enter_context(patch.object(compiler, 'list_wiki_articles', side_effect=lambda: list((root/'wiki/concepts').glob('*.md'))))
            stack.enter_context(patch.object(compiler, 'read_wiki_index', return_value=''))
            stack.enter_context(patch.object(compiler, 'save_state'))
            stack.enter_context(patch.object(compiler, '_append_run'))
            stack.enter_context(patch('model_client.wiki_context_allowed', return_value=True))
            stack.enter_context(patch('model_client.run_text_prompt', side_effect=provider))
            asyncio.run(compiler.compile_daily_log(root/'daily/day.md', state))

    def test_selection_precedes_editing_full_text_and_warnings_do_not_block(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'daily').mkdir(); (root/'daily/day.md').write_text('Final choice: orange.')
            (root/'wiki/concepts').mkdir(parents=True)
            old = self._legacy_content('Old choice: blue. Rare setting: 4729.')
            (root/'wiki/concepts/a.md').write_text(old)
            phases = []
            def provider(prompt, *_args, **kwargs):
                props = kwargs['json_schema']['properties']; phases.append(next(iter(props)))
                if 'articles' in props:
                    self.assertNotIn('Rare setting: 4729.', prompt)
                    return self._reply({'articles': ['concepts/a.md']})
                if 'edits' in props:
                    self.assertIn(json.dumps(old, ensure_ascii=False), prompt)
                    return self._reply({'edits': [{'path': 'wiki/concepts/a.md', 'old': 'Old choice: blue.',
                        'new': 'Historical choice: blue. Final choice: orange.'}], 'creates': []})
                return self._reply({'errors': [], 'warnings': ['Optional extra detail omitted']})
            state = {}; self._run(root, provider, state)
            self.assertEqual(['articles', 'edits', 'errors'], phases)
            self.assertIn('Rare setting: 4729.', (root/'wiki/concepts/a.md').read_text())
            self.assertTrue(state['ingested']['day.md']['content_verified'])
            self.assertEqual(['Optional extra detail omitted'], state['ingested']['day.md']['coverage_warnings'])

    def test_content_errors_block_after_one_repair_and_leave_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'daily').mkdir(); (root/'daily/day.md').write_text('New fact.')
            counts = {'edit': 0, 'audit': 0}
            def provider(prompt, *_args, **kwargs):
                if 'errors' in kwargs['json_schema']['properties']:
                    counts['audit'] += 1
                    return self._reply({'errors': ['Invented claim'], 'warnings': []})
                counts['edit'] += 1
                return self._reply({'edits': [], 'creates': [{'path': 'wiki/concepts/a.md', 'content': self._legacy_content('Invented claim')}]}
                    if counts['edit'] == 1 else {'edits': [], 'creates': []})
            state = {}; self._run(root, provider, state)
            self.assertEqual({'edit': 2, 'audit': 2}, counts)
            self.assertFalse((root/'wiki/concepts/a.md').exists())
            self.assertNotIn('compiled_at', state['ingested']['day.md'])
            self.assertTrue(list((root/'.cmc').glob('compile-draft-*.json')))

    def test_later_chunk_failure_is_atomic_and_verified_chunk_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'daily').mkdir()
            (root/'daily/day.md').write_text('First durable fact.\n\nSecond durable fact.')
            edits = 0
            def initial(prompt, *_args, **kwargs):
                nonlocal edits
                if 'errors' in kwargs['json_schema']['properties']:
                    return self._reply({'errors': [], 'warnings': []})
                edits += 1
                if edits == 2: return {'ok': False, 'error': 'interrupted'}
                self.assertIn('Second durable fact.', prompt)
                return self._reply({'edits': [], 'creates': [{'path': 'wiki/concepts/a.md', 'content': self._legacy_content('First durable fact.')}]})
            state = {}; self._run(root, initial, state, 25)
            self.assertFalse((root/'wiki/concepts/a.md').exists())
            self.assertTrue(any(json.loads(p.read_text()).get('verified') for p in (root/'.cmc').glob('compile-draft-*.json')))
            def resume(prompt, *_args, **kwargs):
                props = kwargs['json_schema']['properties']
                if 'articles' in props: return self._reply({'articles': ['concepts/a.md']})
                if 'errors' in props: return self._reply({'errors': [], 'warnings': []})
                self.assertIn('(part 2 of 2)', prompt)
                return self._reply({'edits': [{'path': 'wiki/concepts/a.md', 'old': 'First durable fact.',
                    'new': 'First durable fact. Second durable fact.'}], 'creates': []})
            self._run(root, resume, state, 25)
            self.assertTrue(state['ingested']['day.md']['content_verified'])
            self.assertIn('Second durable fact.', (root/'wiki/concepts/a.md').read_text())
            self.assertFalse(list((root/'.cmc').glob('compile-draft-*.json')))

    def test_concurrent_source_and_wiki_changes_prevent_publication(self):
        for target in ('source', 'wiki'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); (root/'daily').mkdir(); (root/'daily/day.md').write_text('New fact.')
                def provider(prompt, *_args, **kwargs):
                    if 'errors' in kwargs['json_schema']['properties']:
                        if target == 'wiki': (root/'wiki/concepts/user.md').write_text(self._legacy_content('User change.'))
                        return self._reply({'errors': [], 'warnings': []})
                    if target == 'source': (root/'daily/day.md').write_text('Changed source.')
                    return self._reply({'edits': [], 'creates': [{'path': 'wiki/concepts/a.md', 'content': self._legacy_content('New fact.')}]})
                state = {}; self._run(root, provider, state)
                self.assertFalse((root/'wiki/concepts/a.md').exists())
                self.assertEqual(target+'_changed', state['ingested']['day.md']['stage'])
    def test_audit_includes_old_facts_and_numbered_late_evidence(self):
        seen = []
        base = self._legacy_content('Old rare fact: 4729. Current choice: blue.')
        def call(prompt, *_):
            seen.append(prompt)
            return self._reply({'errors': [], 'warnings': []})
        compiler._audit_proposal({'changes': []}, {'wiki/concepts/a.md': base}, 'daily/day.md',
            'Earlier blue.', 'Chunk 2 of 2:\nFinal orange.', call)
        self.assertIn(json.dumps(base), seen[0])
        self.assertIn('Chunk 2 of 2:\nFinal orange.', seen[0])

    def test_changed_source_or_compiler_invalidates_verified_checkpoint(self):
        for changed in ('source', 'compiler'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); (root/'daily').mkdir()
                daily = root/'daily/day.md'; daily.write_text('First durable fact.\n\nSecond durable fact.')
                edits = 0
                def fail_late(prompt, *_args, **kwargs):
                    nonlocal edits
                    if 'errors' in kwargs['json_schema']['properties']:
                        return self._reply({'errors': [], 'warnings': []})
                    edits += 1
                    if edits == 2: return {'ok': False, 'error': 'interrupted'}
                    return self._reply({'edits': [], 'creates': [{'path': 'wiki/concepts/a.md', 'content': self._legacy_content('First durable fact.')}]})
                state = {}; self._run(root, fail_late, state, 25)
                if changed == 'source':daily.write_text(daily.read_text().replace('First', 'Other'))
                else:
                    for path in (root/'.cmc').glob('compile-draft-*.json'):
                        saved=json.loads(path.read_text());saved['compiler_signature']='old';path.write_text(json.dumps(saved))
                def stop_first(prompt, *_args, **kwargs):
                    self.assertIn('(part 1 of 2)', prompt)
                    return {'ok': False, 'error': 'stop on regenerated first chunk'}
                self._run(root, stop_first, state, 25)
                self.assertEqual('edit', state['ingested']['day.md']['stage'])
    def test_every_provider_receives_schema_and_edit_audit_prefer_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'daily').mkdir();(root/'daily/day.md').write_text('Use orange.')
            seen=[]
            def provider(prompt,*args,**kwargs):
                seen.append(kwargs)
                self.assertIn(json.dumps(kwargs['json_schema']),prompt)
                self.assertTrue(kwargs['prefer_fallback'])
                if 'errors' in kwargs['json_schema']['properties']:
                    return self._reply({'errors':[],'warnings':[]})
                return self._reply({'edits':[],'creates':[]})
            state={};self._run(root,provider,state)
            self.assertEqual(2,len(seen))
            self.assertTrue(state['ingested']['day.md']['content_verified'])
    def test_correction_reports_all_non_exact_fragments_together(self):
        base=self._legacy_content('Heading A. Heading B.')
        reply={'edits':[{'path':'wiki/concepts/a.md','old':old,'new':'new'} for old in ('wrong A','wrong B')],'creates':[]}
        with self.assertRaises(ValueError) as captured:
            compiler._apply_article_edits(json.dumps(reply),{'wiki/concepts/a.md':base},{})
        self.assertIn('wrong A',str(captured.exception))
        self.assertIn('wrong B',str(captured.exception))

    def test_misquoted_words_and_numbers_are_rejected(self):
        fact = 'Обмен каталога разрешён только после проверки резервной копии; текущий лимит товаров: 10.'
        original = self._legacy_content(fact + '\n' + fact.replace('10', '20'))
        supplied = {'wiki/concepts/a.md': original}
        for old in (fact.replace('резервной', 'резервнои'), fact.replace('10', '30')):
            reply = {'edits': [{'path': 'wiki/concepts/a.md', 'old': old,
                               'new': old.replace('товаров:', 'записей:')}], 'creates': []}
            with self.subTest(old=old), self.assertRaisesRegex(ValueError, 'one unique exact old fragment'):
                compiler._apply_article_edits(json.dumps(reply), supplied, {})
            self.assertEqual(original, supplied['wiki/concepts/a.md'])

    def test_selection_ignores_unknown_and_repeated_paths(self):
        existing = {'concepts/a.md': 'A', 'concepts/b.md': 'B'}
        reply = {'text': json.dumps({'articles': ['concepts/a.md', 'concepts/ghost.md', 'concepts/a.md']})}
        with patch.object(compiler, '_build_wiki_context', return_value=('catalog', 'index')):
            selected = compiler._select_articles('source', existing, lambda *args: reply)
        self.assertEqual({'wiki/concepts/a.md': 'A'}, selected)

    def test_article_paths_without_wiki_prefix_are_normalized(self):
        content = self._legacy_content('Fresh fact.')
        reply = {'edits': [], 'creates': [{'path': 'concepts/new.md', 'content': content}]}
        changed = compiler._apply_article_edits(json.dumps(reply), {}, {})
        self.assertEqual(['wiki/concepts/new.md'], list(changed))
