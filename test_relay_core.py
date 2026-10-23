"""Regression checks for global relay switching, using synthetic configuration."""
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from relay_core import REASONING_EFFORTS, RelayManager, _global_provider_ids, _new_config, _parse


@contextmanager
def _no_lock():
    yield


class GlobalRelayConfigTests(unittest.TestCase):
    profile = dict(name='36524', base_url='https://new.example/v1',
                   api_key='synthetic-test-token', model='test-model', reasoning_effort='high')

    def test_existing_legacy_id_uses_new_station(self):
        original = b'''# keep this comment
model_provider = "relay_switcher"
model = "old-model"
conversationDetailMode = "OLD_MODE"
openai_base_url = "http://127.0.0.1:57321/v1"
[model_providers.custom]
name = "old relay"
base_url = "https://old.example/v1"
wire_api = "responses"
env_key = "OLD_RELAY_KEY"
requires_openai_auth = true
[model_providers.custom.auth]
command = "old-credential-helper"
[model_providers.relay_switcher]
name = "current relay"
base_url = "https://current.example/v1"
[model_providers.azure]
base_url = "https://other.example/v1"
env_key = "OTHER_PROVIDER_KEY"
[agents]
max_threads = 4
default_subagent_reasoning_effort = "medium"
[mcp_servers.example]
command = "example-tool"
'''
        result = _new_config(original, self.profile)
        after = _parse(result)
        self.assertEqual(after['model_provider'], 'relay_switcher')
        self.assertEqual(after['model'], 'test-model')
        self.assertEqual(after['model_reasoning_effort'], 'high')
        self.assertEqual(after['conversationDetailMode'], 'STEPS_COMMANDS')
        self.assertEqual(after['agents'], dict(max_threads=4, default_subagent_reasoning_effort='max'))
        for provider_id in ('custom', 'relay_switcher'):
            self.assertEqual(after['model_providers'][provider_id], dict(
                name='36524', base_url='https://new.example/v1', wire_api='responses',
                requires_openai_auth=False, experimental_bearer_token='synthetic-test-token'))
        before = _parse(original)
        self.assertEqual(after['model_providers']['azure'], before['model_providers']['azure'])
        self.assertEqual(after['mcp_servers'], before['mcp_servers'])
        self.assertEqual(after['openai_base_url'], before['openai_base_url'])
        self.assertIn(b'# keep this comment', result)
        self.assertEqual(_parse(_new_config(result, self.profile)), after)

    def test_saved_station_aliases_and_active_provider_are_synchronized(self):
        original = b'''model_provider = "active"
[model_providers.active]
base_url = "https://current.example/v1"
[model_providers."old.relay"]
base_url = "https://saved.example/v1/"
[model_providers.other]
base_url = "https://other.example/v1"
'''
        aliases = _global_provider_ids(_parse(original), [dict(base_url='https://saved.example/v1')])
        self.assertEqual(set(aliases), {'relay_switcher', 'active', 'old.relay'})
        after = _parse(_new_config(original, self.profile, provider_ids=aliases))
        for provider_id in aliases:
            self.assertEqual(after['model_providers'][provider_id]['base_url'], self.profile['base_url'])
        self.assertEqual(after['model_providers']['other']['base_url'], 'https://other.example/v1')

    def test_builtin_ids_are_not_replaced(self):
        before = dict(model_provider='openai', model_providers={
            provider_id: dict(base_url=self.profile['base_url'])
            for provider_id in ('openai', 'ollama', 'lmstudio', 'amazon-bedrock')})
        aliases = _global_provider_ids(before, [self.profile])
        self.assertEqual(aliases, ('relay_switcher',))

    def test_reasoning_choices_and_catalog_remain_global_defaults(self):
        for effort in REASONING_EFFORTS:
            with self.subTest(effort=effort):
                profile = dict(self.profile, reasoning_effort=effort)
                after = _parse(_new_config(b'', profile, 'catalog.json'))
                self.assertEqual(after['model_reasoning_effort'], 'low' if effort == 'light' else effort)
                self.assertEqual(after['model_catalog_json'], 'catalog.json')
                self.assertEqual(after['conversationDetailMode'], 'STEPS_COMMANDS')
                self.assertEqual(after['agents']['default_subagent_reasoning_effort'], 'max')
        profile = {key: value for key, value in self.profile.items() if key != 'reasoning_effort'}
        self.assertEqual(_parse(_new_config(b'', profile))['model_reasoning_effort'], 'high')

    def test_conversation_detail_mode_is_written_without_catalog(self):
        after = _parse(_new_config(b'', self.profile))
        self.assertEqual(after['conversationDetailMode'], 'STEPS_COMMANDS')

    def test_auto_review_model_is_saved_per_station_and_cleared_on_connection_change(self):
        with (TemporaryDirectory() as temporary,
              patch('relay_core._crypt', side_effect=lambda data, encrypt: data),
              patch.object(RelayManager, '_locked', autospec=True,
                           side_effect=lambda self: _no_lock())):
            manager = RelayManager(Path(temporary))
            profile_id = manager.save_profile(
                'station', 'https://relay.example/v1', 'synthetic-test-token',
                model='main-model', auto_review_model='review-model'
            )
            self.assertEqual(manager.get_profile(profile_id)['auto_review_model'], 'review-model')

            manager.save_profile(
                'station', 'https://relay.example/v1', 'synthetic-test-token',
                model='main-model', profile_id=profile_id
            )
            self.assertEqual(manager.get_profile(profile_id)['auto_review_model'], 'review-model')

            manager.save_profile(
                'station', 'https://another.example/v1', 'synthetic-test-token',
                model='main-model', profile_id=profile_id
            )
            self.assertEqual(manager.get_profile(profile_id)['auto_review_model'], '')

    def test_switch_writes_auto_review_override_to_codex_catalog(self):
        with (TemporaryDirectory() as temporary,
              patch('relay_core._crypt', side_effect=lambda data, encrypt: data),
              patch.object(RelayManager, '_locked', autospec=True,
                           side_effect=lambda self: _no_lock())):
            manager = RelayManager(Path(temporary))
            profile_id = manager.save_profile(
                'station', 'https://relay.example/v1', 'synthetic-test-token',
                model='main-model', models=['main-model', 'second-model'],
                auto_review_model='review-model'
            )
            manager.switch(profile_id)

            config = _parse(manager.config.read_bytes())
            catalog_path = Path(config['model_catalog_json'])
            catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
            self.assertEqual(config['model'], 'main-model')
            self.assertEqual({item['auto_review_model_override'] for item in catalog['models']},
                             {'review-model'})


if __name__ == '__main__':
    unittest.main()
