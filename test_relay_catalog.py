"""Regression checks for reviewer model metadata in generated catalogs."""
import json
import tempfile
import unittest
from pathlib import Path

from relay_core import _catalog_auto_review_model
from relay_catalog import write_model_catalog


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


if __name__ == '__main__':
    unittest.main()
