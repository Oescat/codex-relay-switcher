"""Regression checks for reviewer model metadata in generated catalogs."""
import json
import tempfile
import unittest
from pathlib import Path

from relay_core import RelayError, _catalog_auto_review_model
import relay_catalog
from relay_catalog import refresh_official_model_catalog, write_model_catalog
from unittest.mock import patch


class RelayCatalogAutoReviewTests(unittest.TestCase):
    def test_reasoning_choices_follow_the_same_catalog_as_written_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            entry = relay_catalog._fallback('limited-model')
            entry.update(supported_reasoning_levels=[
                {'effort': 'low', 'description': 'light'},
                {'effort': 'medium', 'description': 'medium'}],
                default_reasoning_level='medium')
            directory = home / 'relay-switcher'
            directory.mkdir()
            (directory / 'official_model_catalog.json').write_text(
                json.dumps({'models': [entry]}), encoding='utf-8')
            options = relay_catalog.reasoning_options(home, {}, 'limited-model')
            self.assertTrue(options['known'])
            self.assertEqual(options['values'], ('light', 'medium'))
            self.assertEqual(options['default'], 'medium')
            unknown = relay_catalog.reasoning_options(home, {}, 'unknown-model')
            self.assertFalse(unknown['known'])

    def test_known_empty_levels_are_distinct_from_unknown_capabilities(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            source = home / 'custom.json'
            entry = relay_catalog._fallback('no-reasoning')
            source.write_text(json.dumps({'models': [entry]}), encoding='utf-8')
            options = relay_catalog.reasoning_options(
                home, {'model_catalog_json': str(source)}, 'no-reasoning')
            self.assertTrue(options['known'])
            self.assertEqual(options['values'], ('auto',))
            self.assertEqual(options['default'], 'auto')

    def test_explicit_none_and_minimal_levels_are_not_lost(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            source = home / 'custom.json'
            entry = relay_catalog._fallback('special-levels')
            entry.update(supported_reasoning_levels=[
                {'effort': 'none', 'description': 'none'},
                {'effort': 'minimal', 'description': 'minimal'}],
                default_reasoning_level='minimal')
            source.write_text(json.dumps({'models': [entry]}), encoding='utf-8')
            options = relay_catalog.reasoning_options(
                home, {'model_catalog_json': str(source)}, 'special-levels')
            self.assertEqual(options['values'], ('none', 'minimal'))
            self.assertEqual(options['default'], 'minimal')

    def test_generated_unknown_fallback_does_not_become_known_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            generated = write_model_catalog(home, {}, ['unlisted-model'], 'unlisted-model')
            options = relay_catalog.reasoning_options(
                home, {'model_catalog_json': str(generated)}, 'unlisted-model')
            self.assertFalse(options['known'])

    def test_explicit_catalog_remains_a_source_after_switching(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            source = home / 'custom.json'
            entry = relay_catalog._fallback('external-only-model')
            entry.update(supported_reasoning_levels=[{'effort': 'high', 'description': 'high'}],
                         default_reasoning_level='high')
            source.write_text(json.dumps({'models': [entry]}), encoding='utf-8')
            generated = write_model_catalog(home, {'model_catalog_json': str(source)},
                                            ['external-only-model'], 'external-only-model')
            options = relay_catalog.reasoning_options(
                home, {'model_catalog_json': str(generated)}, 'external-only-model')
            self.assertTrue(options['known'])
            self.assertEqual(options['values'], ('high',))
            entry['supported_reasoning_levels'] = []
            source.write_text(json.dumps({'models': [entry]}), encoding='utf-8')
            options = relay_catalog.reasoning_options(
                home, {'model_catalog_json': str(generated)}, 'external-only-model')
            self.assertEqual(options['values'], ('auto',))

    def test_explicit_catalog_precedence_and_malformed_reasoning_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            explicit = home / 'custom.json'
            entry = relay_catalog._fallback('custom-model')
            entry.update(supported_reasoning_levels=[{'effort': 'high', 'description': 'high'}],
                         default_reasoning_level='high')
            explicit.write_text(json.dumps({'models': [entry]}), encoding='utf-8')
            directory = home / 'relay-switcher'
            directory.mkdir()
            invalid = dict(entry, supported_reasoning_levels=[{'effort': {}}])
            (directory / 'official_model_catalog.json').write_text(
                json.dumps({'models': [invalid]}), encoding='utf-8')
            options = relay_catalog.reasoning_options(
                home, {'model_catalog_json': str(explicit)}, 'custom-model')
            self.assertEqual(options['values'], ('high',))
            self.assertFalse(relay_catalog.reasoning_options(home, {}, 'custom-model')['known'])

    def test_identical_external_catalogs_keep_distinct_source_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            paths = []
            for filename in ('first.json', 'second.json'):
                source = home / filename
                source.write_text(json.dumps({'models': [relay_catalog._fallback('custom-model')]}),
                                  encoding='utf-8')
                paths.append(write_model_catalog(home, {'model_catalog_json': str(source)},
                                                  [], 'custom-model'))
            self.assertNotEqual(paths[0], paths[1])
            paths[0].with_suffix('.source.json').write_text('{}', encoding='utf-8')
            with self.assertRaises(RelayError):
                relay_catalog.reasoning_options(home, {'model_catalog_json': str(paths[0])},
                                                'custom-model')

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
