from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SyntheticProductE2ETests(unittest.TestCase):
    def test_append_during_compile_stays_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            script = textwrap.dedent(
                f"""
                import asyncio, importlib.util, os, sys
                from pathlib import Path
                from unittest.mock import patch
                root = Path({str(ROOT)!r}); project = Path({str(project)!r})
                sys.path.insert(0, str(root / 'scripts')); os.environ['CMC_PROJECT_DIR'] = str(project)
                from init_project import init
                init(project)
                daily = sorted((project / 'daily').glob('*.md'))[-1]
                spec = importlib.util.spec_from_file_location('compiler_race', root / 'scripts/compile.py')
                compiler = importlib.util.module_from_spec(spec); spec.loader.exec_module(compiler)
                import model_client
                def append(*args, **kwargs):
                    daily.write_text(daily.read_text() + '\\nNEW_UNSEEN', encoding='utf-8')
                    return {{'ok': True, 'engine': 'fake', 'text': '{{"result":"noop","reason":"nothing durable","changes":[]}}'}}
                state = {{}}
                with patch.object(model_client, 'run_text_prompt', side_effect=append):
                    asyncio.run(compiler.compile_daily_log(daily, state))
                assert state['ingested'][daily.name]['stage'] == 'source_changed', state
                """
            )
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    def test_capture_compile_and_startup_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            article_template = "---\ntitle: Blue widgets\nkind: decision\nstatus: active\nupdated: 2026-09-18\nsources:\n  - {source}\n---\n\n# Blue widgets\n\nUse blue widgets in test.\n"
            script = textwrap.dedent(
                f"""
                import asyncio, importlib.util, json, os, sys
                from pathlib import Path
                from unittest.mock import patch
                root = Path({str(ROOT)!r})
                project = Path({str(project)!r})
                sys.path.insert(0, str(root / 'scripts'))
                os.environ['CMC_PROJECT_DIR'] = str(project)
                from init_project import init
                init(project)
                import model_client, flush
                with patch.object(model_client, 'run_text_prompt', return_value={{'ok': True, 'engine': 'fake', 'text': '**Decisions Made:**\\n- Use blue widgets in test.'}}):
                    text, result = flush.run_flush('User decided API_KEY=synthetic-not-real and blue widgets.')
                assert result['redacted'] and 'synthetic-not-real' not in text
                flush.append_to_daily_log(text)
                daily = sorted((project / 'daily').glob('*.md'))[-1]
                source = f'daily/{{daily.name}}'
                article = {article_template!r}.format(source=source)
                proposal = json.dumps({{'result':'changes','changes':[
                    {{'path':'wiki/concepts/blue-widgets.md','content':article}},
                    {{'path':'wiki/index.md','content':'# Wiki Index\\n\\n| Article | Summary | Source | Updated |\\n|---|---|---|---|\\n| [[concepts/blue-widgets]] | Blue | '+source+' | 2026-09-18 |\\n'}},
                    {{'path':'wiki/log.md','content':'# Log\\n\\ncompiled blue widgets\\n'}}
                ]}})
                spec = importlib.util.spec_from_file_location('compiler_e2e', root / 'scripts/compile.py')
                compiler = importlib.util.module_from_spec(spec); spec.loader.exec_module(compiler)
                state = {{}}
                with patch.object(model_client, 'run_text_prompt', return_value={{'ok': True, 'engine':'fake', 'text':proposal}}):
                    asyncio.run(compiler.compile_daily_log(daily, state))
                assert daily.name in state['ingested']
                assert (project / 'wiki/concepts/blue-widgets.md').exists()
                hook_spec = importlib.util.spec_from_file_location('session_start_e2e', root / 'hooks/session-start.py')
                hook = importlib.util.module_from_spec(hook_spec); hook_spec.loader.exec_module(hook)
                with patch.object(hook, '_maybe_recover_pending'), patch.object(hook, '_maybe_trigger_compilation'):
                    context = hook.build_context(project)
                assert 'blue-widgets' in context and 'Use blue widgets' in context
                """
            )
            env = os.environ.copy()
            env.pop("CLAUDE_INVOKED_BY", None)
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
