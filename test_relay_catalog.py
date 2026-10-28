"""Regression checks for reviewer model metadata in generated catalogs."""
import json
import tempfile
import unittest
from pathlib import Path

from relay_core import RelayError, _catalog_auto_review_model
from relay_catalog import refresh_official_model_catalog, write_model_catalog
from unittest.mock import patch


class RelayCatalogAutoReviewTests(unittest.TestCase):
    def test_override_applies_to_every_relay_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = write_model_catalog(home, {}, ['main-model', 'other-model'], 'main-model',
                                       auto_review_model='review-model')
            catalog = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual([item['slug'] for item in catalog['models']],
                             ['main-model', 'other-model'])
            self.assertEqual([item['auto_review_model_override'] for item in catalog['models']],
                             ['review-model', 'review-model'])

    def test_empty_selection_clears_existing_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = write_model_catalog(home, {}, ['main-model'], 'main-model')
            catalog = json.loads(path.read_text(encoding='utf-8'))
            self.assertIsNone(catalog['models'][0]['auto_review_model_override'])

    def test_current_override_is_read_only_from_codex_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = write_model_catalog(home, {}, ['main-model'], 'main-model', 'review-model')
            config = {'model_catalog_json': str(path), 'model': 'main-model'}
            self.assertEqual(_catalog_auto_review_model(home, config), 'review-model')
            self.assertEqual(_catalog_auto_review_model(home, {
                'model_catalog_json': str(home.parent / path.name), 'model': 'main-model'
            }), '')

    def test_refresh_official_catalog_validates_and_saves_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            payload = json.dumps({'models': [{'slug': 'gpt-6.1-terra'}]}).encode()
            class Response:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def read(self, size):
                    return payload
            target = home / 'official.json'
            with patch('relay_catalog.urllib.request.urlopen', return_value=Response()):
                count = refresh_official_model_catalog(target)
            self.assertEqual(count, 1)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['models'][0]['slug'],
                             'gpt-6.1-terra')

    def test_official_catalog_is_used_before_codex_cache_and_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / 'relay-switcher').mkdir()
            (home / 'relay-switcher' / 'official_model_catalog.json').write_text(
                json.dumps({'models': [{
                    'slug': 'gpt-6.1-terra', 'display_name': 'GPT-6.1 Terra',
                    'supported_reasoning_levels': [
                        {'effort': level, 'description': level}
                        for level in ('none', 'low', 'medium', 'high', 'xhigh', 'max')],
                    'default_reasoning_level': 'high', 'shell_type': 'shell_command',
                    'visibility': 'list', 'supported_in_api': True, 'priority': 1,
                    'support_verbosity': False, 'truncation_policy': {'mode': 'tokens', 'limit': 10000},
                    'experimental_supported_tools': [], 'base_instructions': 'synthetic'}]}),
                encoding='utf-8')
            path = write_model_catalog(home, {}, ['gpt-6.1-terra'], 'gpt-6.1-terra')
            entry = json.loads(path.read_text(encoding='utf-8'))['models'][0]
            self.assertEqual(entry['display_name'], 'GPT-6.1 Terra')
            self.assertEqual([item['effort'] for item in entry['supported_reasoning_levels']],
                             ['none', 'low', 'medium', 'high', 'xhigh', 'max'])


if __name__ == '__main__':
    unittest.main()
