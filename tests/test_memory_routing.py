from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from path_safety import memory_project
from init_project import init


class MemoryRoutingTests(unittest.TestCase):
    def test_explicit_initializer_can_create_a_nested_project_but_follows_pointers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            init(root)
            child = root / 'independent'
            child.mkdir()
            init(child)
            self.assertTrue((child / 'wiki/index.md').is_file())
            local = root / 'checkout'
            local.mkdir()
            (local / '.cmc-project.json').write_text(json.dumps({'project': str(child)}))
            init(local)
            self.assertFalse((local / 'wiki').exists())

    def test_pointer_routes_nested_checkouts_without_following_memory_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            canonical, local = root / 'canonical', root / 'local'
            for name in ('wiki', 'daily', '.cmc'):
                (canonical / name).mkdir(parents=True, exist_ok=True)
            (local / 'app/public').mkdir(parents=True)
            for name in ('wiki', 'daily', '.cmc'):
                (local / name).symlink_to(canonical / name)
            (local / '.cmc-project.json').write_text(json.dumps({'project': str(canonical)}))
            self.assertEqual(canonical, memory_project(local / 'app/public'))
            self.assertEqual(canonical, memory_project(canonical / 'wiki'))

    def test_invalid_pointers_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            local, target = root / 'local', root / 'target'
            local.mkdir(); target.mkdir()
            pointer = local / '.cmc-project.json'
            for value in ({'project': 'relative'}, {'project': str(target)}, []):
                pointer.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    memory_project(local)
            (target / 'wiki').mkdir(); (target / 'daily').mkdir()
            (target / '.cmc-project.json').write_text(json.dumps({'project': str(local)}))
            pointer.write_text(json.dumps({'project': str(target)}))
            with self.assertRaises(ValueError):
                memory_project(local)
            pointer.unlink(); pointer.symlink_to(target / '.cmc-project.json')
            with self.assertRaises(ValueError):
                memory_project(local)

    def test_local_capture_flush_compile_startup_and_search_share_one_memory(self):
        code = r'''
import asyncio, importlib.util, json, os, sys
from pathlib import Path
from unittest.mock import patch
root, base = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
sys.path.insert(0, str(root / 'scripts'))
canonical, local = base / 'canonical', base / 'local'
canonical.mkdir(); (local / 'app/public').mkdir(parents=True)
os.environ['CMC_PROJECT_DIR'] = str(canonical)
from init_project import init
init(canonical)
(local / '.cmc-project.json').write_text(json.dumps({'project': str(canonical)}))
for name in ('wiki', 'daily', '.cmc'):
    (local / name).symlink_to(canonical / name)
os.environ['CMC_PROJECT_DIR'] = str(local / 'app/public')
os.environ['CODEX_PROJECT_DIR'] = str(local / 'app/public')
os.environ['CODEX_HOME'] = str(base / 'sessions-home')
session = base / 'sessions-home/sessions/session.jsonl'
session.parent.mkdir(parents=True)
session.write_text(json.dumps({'type':'session_meta','payload':{'id':'routing-test','cwd':str(local / 'app/public')}})+'\n')
with session.open('a') as f:
    for kind, text in [('user_message','Use blue widgets for the routing acceptance test.'),('agent_message','Recorded blue widgets.'),('user_message','Keep this decision in memory.')]:
        f.write(json.dumps({'type':'event_msg','payload':{'type':kind,'message':text}})+'\n')
def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, root / filename)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
with patch('sys.stdin') as stdin:
    stdin.read.return_value = json.dumps({'session_id':'routing-test','session_path':str(session)})
    hook = load('routing_stop','hooks/codex-stop.py')
assert hook.PROJECT_DIR == canonical
assert hook._find_latest_session() == (session, 'routing-test')
with patch.object(hook,'claim_flush',return_value=False):
    hook.main()
contexts = list((canonical / '.cmc').glob('codex-flush-*.md'))
assert len(contexts) == 1
assert 'blue widgets' in contexts[0].read_text()
import config, importlib
importlib.reload(config)
assert config.PROJECT_DIR == canonical
import model_client, flush
with patch.object(model_client,'run_text_prompt',return_value={'ok':True,'engine':'fake','text':'**Decisions Made:**\n- Use blue widgets for the routing acceptance test.'}):
    text, result = flush.run_flush(contexts[0].read_text())
assert result['ok']
flush.append_to_daily_log(text, session_id='routing-test')
daily = sorted((canonical / 'daily').glob('*.md'))[-1]
source = f'daily/{daily.name}'
article = f'---\ntitle: Blue widgets\nkind: decision\nstatus: active\nupdated: 2026-10-26\nsources:\n  - {source}\n---\n\n# Blue widgets\n\nUse blue widgets for the routing acceptance test.\n'
proposal = json.dumps({'edits':[],'creates':[{'path':'wiki/concepts/blue-widgets.md','content':article}]})
compiler = load('routing_compile','scripts/compile.py')
state = {}
def reply(*args, **kwargs):
    schema = kwargs.get('json_schema',{}).get('properties',{})
    text = json.dumps({'errors':[],'warnings':[]}) if 'errors' in schema else proposal
    return {'ok':True,'engine':'fake','text':text}
with patch.object(model_client,'run_text_prompt',side_effect=reply):
    asyncio.run(compiler.compile_daily_log(daily,state))
assert state['ingested'][daily.name]['content_verified']
startup = load('routing_start','hooks/session-start.py')
with patch.object(startup,'_maybe_recover_pending'),patch.object(startup,'_maybe_trigger_compilation'):
    context = startup.build_context(local / 'app/public')
assert 'blue-widgets' in context and 'Use blue widgets' in context
import query
context, count, _ = query.build_query_context('blue widgets')
assert count == 1 and 'Use blue widgets' in context
assert not (local / 'sources').exists()
assert (local / 'daily').resolve() == canonical / 'daily'
# Every Claude hook must resolve the same canonical destination too.
os.environ['CLAUDE_PROJECT_DIR'] = str(local / 'app/public')
for name in ('session-end','pre-compact'):
    module = load('routing_'+name.replace('-','_'),f'hooks/{name}.py')
    assert module.PROJECT_DIR == canonical
'''
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            for key in ('CLAUDE_INVOKED_BY', 'CMC_DISABLED', 'CMC_PROJECT_DIR', 'CLAUDE_PROJECT_DIR'):
                env.pop(key, None)
            result = subprocess.run([sys.executable, '-c', code, str(ROOT), tmp],
                                    cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr or result.stdout)


if __name__ == '__main__':
    unittest.main()
