import importlib
import sys
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
model_client = importlib.import_module("model_client")


class ModelClientTests(unittest.TestCase):
    def test_complete_wiki_context_requires_registered_project(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'engine.json'
            project = Path(temp) / 'project'
            with patch.object(model_client, 'CONFIG_PATH', config):
                self.assertFalse(model_client.wiki_context_allowed(project))
                config.write_text(json.dumps({'engine': 'grok'}))
                self.assertFalse(model_client.wiki_context_allowed(project))
                config.write_text(json.dumps({'engine': 'grok', 'wiki_context_projects': [str(project.resolve())],
                                              'wiki_context_engines': ['grok', 'luna']}))
                self.assertTrue(model_client.wiki_context_allowed(project))
                self.assertFalse(model_client.wiki_context_allowed(Path(temp) / 'other'))
                config.write_text(json.dumps({'engine': 'haiku', 'wiki_context_projects': [str(project.resolve())],
                                              'wiki_context_engines': ['grok', 'luna']}))
                self.assertFalse(model_client.wiki_context_allowed(project))

    def test_complete_wiki_consent_can_cover_project_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / 'projects'
            root.mkdir()
            config = Path(temp) / 'engine.json'
            config.write_text(json.dumps({'engine': 'grok', 'fallback': 'luna',
                'wiki_context_roots': [str(root)], 'wiki_context_engines': ['grok', 'luna']}))
            with patch.object(model_client, 'CONFIG_PATH', config):
                self.assertTrue(model_client.wiki_context_allowed(root / 'new-project'))
                self.assertFalse(model_client.wiki_context_allowed(root))
                self.assertFalse(model_client.wiki_context_allowed(root.parent / 'outside'))
                (root / 'external-link').symlink_to(root.parent, target_is_directory=True)
                self.assertFalse(model_client.wiki_context_allowed(root / 'external-link' / 'outside'))
                with patch.dict('os.environ', {'CMC_ENGINE': 'haiku'}):
                    self.assertFalse(model_client.wiki_context_allowed(root / 'new-project'))

    def test_falls_back_to_configured_engine(self):
        failed = {"ok": False, "engine": "grok", "error": "unavailable"}
        passed = {"ok": True, "engine": "luna", "text": "ok"}
        with patch.object(model_client, "load_engines", return_value=("grok", "luna")), \
             patch.object(model_client, "_run", side_effect=[failed, passed]) as run, \
             patch("alerts.notify"):
            result = model_client.run_text_prompt("prompt", Path.cwd())

        self.assertTrue(result["ok"])
        self.assertEqual(result["fallback_from"], "grok")
        self.assertEqual([call.args[0] for call in run.call_args_list], ["grok", "luna"])

    def test_mechanical_phase_can_prefer_only_the_configured_fallback(self):
        with patch.object(model_client, 'load_engines', return_value=('grok', 'luna')), \
                patch.object(model_client, '_run', return_value={'ok': True, 'text': 'proposal'}) as run:
            self.assertTrue(model_client.run_text_prompt('merge', Path.cwd(), prefer_fallback=True)['ok'])
        self.assertEqual('luna', run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
