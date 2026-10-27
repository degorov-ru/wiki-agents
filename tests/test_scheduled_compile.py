import sys
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import maybe_compile as trigger
import scheduled_compile as scheduler


class ScheduleTests(unittest.TestCase):
    def test_new_daily_precedes_older_archive_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = [Path(temp).resolve() / name for name in ('archive', 'fresh')]
            for root in roots:
                (root / 'wiki').mkdir(parents=True)
                (root / 'daily').mkdir()
                (root / '.cmc').mkdir()
                (root / 'daily/day.md').write_text('decision')
            (roots[0] / '.cmc/state.json').write_text(json.dumps({'ingested': {'day.md': {
                'compiled_at': '2026-01-01', 'hash': trigger._file_hash(roots[0] / 'daily/day.md')}}}))
            (roots[1] / '.cmc/last-compile-spawn').touch()
            result = type('Result', (), {'stdout': 'spawned: compile.py', 'returncode': 0})()
            with patch.object(scheduler, 'PROJECT_ROOTS', ()), patch.object(scheduler.subprocess, 'run', return_value=result) as run:
                scheduler.check_projects(roots)
            self.assertEqual(str(roots[1]), run.call_args.kwargs['env']['CMC_PROJECT_DIR'])
            self.assertEqual(1, run.call_count)

    def test_recent_project_does_not_starve_other_queues(self):
        with tempfile.TemporaryDirectory() as temp:
            first, waiting = Path(temp).resolve() / 'first', Path(temp).resolve() / 'waiting'
            for root in (first, waiting):
                (root / 'wiki').mkdir(parents=True)
            (first / '.cmc').mkdir()
            (first / '.cmc/last-compile-spawn').touch()
            result = type('Result', (), {'stdout': 'spawned: compile.py', 'returncode': 0})()
            with patch.object(scheduler, 'PROJECT_ROOTS', ()), patch.object(scheduler.subprocess, 'run', return_value=result) as run:
                self.assertEqual(0, scheduler.check_projects([first, waiting]))
                self.assertEqual(str(waiting), run.call_args.kwargs['env']['CMC_PROJECT_DIR'])
                self.assertEqual(1, run.call_count)

    def test_each_queue_run_discovers_new_initialized_projects_under_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / 'projects'
            root.mkdir()
            outside = base / 'outside'
            first = root / 'first'
            paused = root / 'paused'
            for project in (first, outside, paused):
                (project / 'wiki').mkdir(parents=True)
                (project / 'daily').mkdir()
                (project / 'AGENTS.md').touch()
            (paused / '.cmc').mkdir()
            (paused / '.cmc/purge-paused').touch()

            calls = []
            result = type('Result', (), {'stdout': 'skip: no pending daily logs', 'returncode': 0})()
            with patch.object(scheduler, 'PROJECT_ROOTS', (root,)), \
                    patch.object(scheduler.subprocess, 'run', side_effect=lambda *a, **k: calls.append(k['env']['CMC_PROJECT_DIR']) or result):
                self.assertEqual(0, scheduler.check_projects([]))
                later = root / 'later'
                (later / 'wiki').mkdir(parents=True)
                (later / 'daily').mkdir()
                (later / 'AGENTS.md').touch()
                self.assertEqual(0, scheduler.check_projects([outside]))

            self.assertEqual({str(first), str(later), str(outside)}, set(calls))
            self.assertEqual(2, calls.count(str(first)))

    def test_discovery_ignores_symlinks_and_non_projects(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / 'projects'
            root.mkdir()
            external = base / 'external'
            for project in (external, root / 'missing-agent', root / '.worktrees' / 'duplicate'):
                (project / 'wiki').mkdir(parents=True)
                (project / 'daily').mkdir()
                (project / 'AGENTS.md').touch()
            (root / 'missing-agent' / 'AGENTS.md').unlink()
            (root / 'linked').symlink_to(external, target_is_directory=True)

            self.assertEqual([], scheduler.discover_projects((root,)))

    def test_install_preserves_projects_and_sets_periodic_checks(self):
        import plistlib
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            with patch.object(scheduler.sys, 'platform', 'darwin'), patch.object(scheduler.Path, 'home', return_value=home), patch.object(scheduler.subprocess, 'run'):
                path = scheduler.install_schedule([home / 'first'])
                scheduler.install_schedule([home / 'second'])
            config = plistlib.loads(path.read_bytes())
            self.assertEqual(300, config['StartInterval'])
            self.assertTrue(config['RunAtLoad'])
            self.assertEqual(sys.executable, config['ProgramArguments'][0])
            self.assertIn(str(home / 'first'), config['ProgramArguments'])
            self.assertIn(str(home / 'second'), config['ProgramArguments'])

    def test_project_error_does_not_stop_other_projects(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = [Path(temp).resolve() / 'a', Path(temp).resolve() / 'b']
            for root in roots:
                (root / 'wiki').mkdir(parents=True)
            with patch.object(scheduler, 'PROJECT_ROOTS', ()), \
                    patch.object(scheduler.subprocess, 'run', side_effect=[OSError(), type('Result', (), {'stdout': 'skip: no pending daily logs', 'returncode': 0})()]) as run:
                self.assertEqual(1, scheduler.check_projects(roots))
                self.assertEqual(2, run.call_count)

    def test_live_compile_lock_in_any_project_pauses_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            projects = [Path(temp).resolve() / name for name in ('a', 'b')]
            for project in projects:
                (project / 'wiki').mkdir(parents=True)
            (projects[0] / '.cmc').mkdir()
            (projects[0] / '.cmc/compile.lock').write_text(f'pid={os.getpid()}\n')
            with patch.object(scheduler, 'PROJECT_ROOTS', ()), \
                    patch.object(scheduler.subprocess, 'run') as run:
                self.assertEqual(0, scheduler.check_projects(projects))
                self.assertEqual(0, run.call_count)

    def test_dead_compile_lock_does_not_pause_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            projects = [Path(temp).resolve() / name for name in ('a', 'b')]
            for project in projects:
                (project / 'wiki').mkdir(parents=True)
            (projects[0] / '.cmc').mkdir()
            (projects[0] / '.cmc/compile.lock').write_text('pid=999999999\n')
            result = type('Result', (), {'stdout': 'skip: no pending daily logs', 'returncode': 0})()
            with patch.object(scheduler, 'PROJECT_ROOTS', ()), \
                    patch.object(scheduler.os, 'kill', side_effect=ProcessLookupError()), \
                    patch.object(scheduler.subprocess, 'run', return_value=result) as run:
                self.assertEqual(0, scheduler.check_projects(projects))
                self.assertEqual(2, run.call_count)

    def test_successful_spawn_defers_remaining_projects_until_next_queue_run(self):
        with tempfile.TemporaryDirectory() as temp:
            projects = [Path(temp).resolve() / name for name in ('a', 'b')]
            for project in projects:
                (project / 'wiki').mkdir(parents=True)
            result = type('Result', (), {'stdout': 'spawned: compile.py', 'returncode': 0})()
            with patch.object(scheduler, 'PROJECT_ROOTS', ()), \
                    patch.object(scheduler.subprocess, 'run', return_value=result) as run:
                self.assertEqual(0, scheduler.check_projects(projects))
                self.assertEqual(1, run.call_count)

    def test_unverifiable_lock_stops_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            project = base / 'project'
            (project / 'wiki').mkdir(parents=True)
            (project / '.cmc').mkdir()
            target = base / 'external-lock'
            target.write_text(f'pid={os.getpid()}\n')
            (project / '.cmc/compile.lock').symlink_to(target)
            with patch.object(scheduler, 'PROJECT_ROOTS', ()), \
                    patch.object(scheduler.subprocess, 'run') as run:
                self.assertEqual(1, scheduler.check_projects([project]))
                self.assertEqual(0, run.call_count)

    def test_linked_mirror_is_skipped_without_blocking_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            real, mirror = base / 'real', base / 'mirror'
            (real / 'wiki').mkdir(parents=True)
            (real / '.cmc').mkdir()
            mirror.mkdir()
            (mirror / '.cmc').symlink_to(real / '.cmc')
            (mirror / 'wiki').symlink_to(real / 'wiki')
            result = type('Result', (), {'stdout': 'spawned: compile.py', 'returncode': 0})()
            with patch.object(scheduler, 'PROJECT_ROOTS', ()), \
                    patch.object(scheduler.subprocess, 'run', return_value=result) as run:
                self.assertEqual(0, scheduler.check_projects([mirror, real]))
            self.assertEqual(str(real), run.call_args.kwargs['env']['CMC_PROJECT_DIR'])

    def test_changed_daily_stays_pending_and_imports_are_not_scheduled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'daily').mkdir()
            (root / 'sources').mkdir()
            p = root / 'daily/day.md'
            p.write_text('original')
            state = root / 'state.json'
            state.write_text(json.dumps({'ingested': {'day.md': {'hash': trigger._file_hash(p),
                'compiled_at': '2026-01-01T12:00:00Z', 'coverage_verified': True}}}))
            with patch.object(trigger, 'PROJECT_DIR', root), patch.object(trigger, 'DAILY_DIR', root / 'daily'), patch.object(trigger, 'STATE_FILE', state):
                self.assertFalse(trigger._has_pending_work())
                p.write_text('changed')
                self.assertTrue(trigger._has_pending_work())
                p.write_text('original')
                (root / 'sources/new.md').write_text('source')
                self.assertFalse(trigger._has_pending_work())

    def test_granted_permission_retries_without_failure_backoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'daily').mkdir()
            source = root / 'daily/day.md'
            source.write_text('decision')
            state = root / 'state.json'
            state.write_text(json.dumps({'ingested': {'day.md': {'hash': trigger._file_hash(source),
                'stage': 'permission', 'failed_at': datetime.now(timezone.utc).isoformat()}}}))
            with patch.object(trigger, 'PROJECT_DIR', root), patch.object(trigger, 'DAILY_DIR', root / 'daily'), \
                    patch.object(trigger, 'STATE_FILE', state), patch('model_client.wiki_context_allowed', return_value=False):
                self.assertFalse(trigger._has_pending_work())
                with patch('model_client.wiki_context_allowed', return_value=True):
                    self.assertTrue(trigger._has_pending_work())

    def test_valid_coverage_continuation_retries_sooner_than_provider_failure(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'daily').mkdir()
            source = root / 'daily/day.md'
            source.write_text('decision')
            state = root / 'state.json'
            entry = {'hash': trigger._file_hash(source), 'stage': 'coverage',
                     'error': 'source coverage not verified: final decision missing',
                     'failed_at': (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()}
            with patch.object(trigger, 'PROJECT_DIR', root), patch.object(trigger, 'DAILY_DIR', root / 'daily'), \
                    patch.object(trigger, 'STATE_FILE', state):
                state.write_text(json.dumps({'ingested': {'day.md': entry}}))
                self.assertTrue(trigger._has_pending_work())
                entry['error'] = 'coverage: adapter timeout'
                state.write_text(json.dumps({'ingested': {'day.md': entry}}))
                self.assertFalse(trigger._has_pending_work())

    def test_failed_version_waits_then_retries(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'daily').mkdir()
            p = root / 'daily/day.md'
            p.write_text('unchanged')
            state = root / 'state.json'
            entry = {'hash': trigger._file_hash(p), 'failed_at': datetime.now(timezone.utc).isoformat()}
            state.write_text(json.dumps({'ingested': {'day.md': entry}}))
            with patch.object(trigger, 'PROJECT_DIR', root), patch.object(trigger, 'DAILY_DIR', root / 'daily'), patch.object(trigger, 'STATE_FILE', state):
                self.assertFalse(trigger._has_pending_work())
                entry['failed_at'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
                state.write_text(json.dumps({'ingested': {'day.md': entry}}))
                self.assertTrue(trigger._has_pending_work())

    def test_unchanged_archive_is_not_queued_even_after_a_failed_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'daily').mkdir()
            source = root / 'daily/day.md'
            source.write_text('recorded decision')
            state = root / 'state.json'
            entry = {'hash': trigger._file_hash(source), 'compiled_at': '2026-01-01T12:00:00Z'}
            state.write_text(json.dumps({'ingested': {'day.md': entry}}))
            with patch.object(trigger, 'PROJECT_DIR', root), patch.object(trigger, 'DAILY_DIR', root / 'daily'), patch.object(trigger, 'STATE_FILE', state):
                self.assertFalse(trigger._has_pending_work())
                entry.update(failed_at='2026-01-02T12:00:00Z', stage='coverage',
                             error='source coverage not verified: old audit')
                state.write_text(json.dumps({'ingested': {'day.md': entry}}))
                self.assertFalse(trigger._has_pending_work())
                source.write_text('recorded decision\nNew decision.')
                self.assertTrue(trigger._has_pending_work())

    def test_compiler_takes_ownership_of_inherited_lock(self):
        import ast
        source = Path(__file__).resolve().parents[1] / 'scripts/compile.py'
        function = next(n for n in ast.parse(source.read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == '_claim_compile_lock')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / '.cmc').mkdir()
            lock = root / '.cmc/compile.lock'
            lock.write_text('pid=999999\n')
            namespace = {'Path': Path, 'PROJECT_DIR': root, 'os': os}
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
            with patch.dict(os.environ, {'CMC_COMPILE_LOCK': str(lock)}):
                self.assertTrue(namespace['_claim_compile_lock']())
            self.assertEqual(f'pid={os.getpid()}\n', lock.read_text())

    def test_dead_lock_is_recovered_and_live_lock_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / '.cmc').mkdir()
            (root / 'scripts').mkdir()
            (root / 'scripts/compile.py').touch()
            lock = root / '.cmc/compile.lock'
            lock.write_text('pid=999999\n')
            with patch.object(trigger, 'PROJECT_DIR', root), patch.object(trigger, 'TOOL_ROOT', root), patch.object(trigger, 'COMPILE_LOG_FILE', root / '.cmc/compile.log'), patch.object(trigger, 'COMPILE_AFTER_HOUR', 0), patch.object(trigger, 'memory_enabled', return_value=True), patch.object(trigger, '_has_pending_work', return_value=True), patch.object(trigger.os, 'kill', side_effect=ProcessLookupError()), patch.object(trigger.sp, 'Popen') as spawn:
                self.assertTrue(trigger.trigger_if_due().startswith('spawned:'))
                spawn.assert_called_once()
                self.assertIn('--daily-only', spawn.call_args.args[0])
            (root / '.cmc/last-compile-spawn').unlink()
            lock.write_text(f'pid={os.getpid()}\n')
            with patch.object(trigger, 'PROJECT_DIR', root), patch.object(trigger, 'TOOL_ROOT', root), patch.object(trigger, 'COMPILE_AFTER_HOUR', 0), patch.object(trigger, 'memory_enabled', return_value=True), patch.object(trigger, '_has_pending_work', return_value=True):
                self.assertEqual('skip: compile already running', trigger.trigger_if_due())

    def test_night_and_recent_spawn_are_deferred_without_work_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / '.cmc').mkdir()
            (root / 'scripts').mkdir()
            (root / 'scripts/compile.py').touch()
            with patch.object(trigger, 'PROJECT_DIR', root), patch.object(trigger, 'TOOL_ROOT', root), patch.object(trigger, 'memory_enabled', return_value=True), patch.object(trigger, '_has_pending_work', return_value=True), patch.object(trigger, 'COMPILE_AFTER_HOUR', 24):
                self.assertIn('skip: hour', trigger.trigger_if_due())
            (root / '.cmc/last-compile-spawn').touch()
            with patch.object(trigger, 'PROJECT_DIR', root), patch.object(trigger, 'TOOL_ROOT', root), patch.object(trigger, 'memory_enabled', return_value=True), patch.object(trigger, '_has_pending_work', return_value=True), patch.object(trigger, 'COMPILE_AFTER_HOUR', 0):
                self.assertEqual('skip: compile spawned recently', trigger.trigger_if_due())


if __name__ == '__main__':
    unittest.main()
