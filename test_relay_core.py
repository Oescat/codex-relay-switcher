"""Regression checks for global relay switching, using synthetic configuration."""
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from relay_core import REASONING_EFFORTS, RelayError, RelayManager, _global_provider_ids, _new_config, _parse


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


class SubagentReasoningTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parent / '.verification'
        folder.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(prefix='subagent-effort-', dir=folder)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        encryption = patch('relay_core._crypt', side_effect=lambda data, encrypt: data)
        encryption.start()
        self.addCleanup(encryption.stop)
        locking = patch.object(RelayManager, '_locked', autospec=True,
                               side_effect=lambda self: _no_lock())
        locking.start()
        self.addCleanup(locking.stop)

    def test_all_subagent_choices_are_independent_and_preserve_other_agent_settings(self):
        originals = (b'', chr(10).join([
            '# retained comment', '[agents]', 'max_threads = 4',
            'default_subagent_reasoning_effort = "medium"',
            '[agents.worker]', 'config_file = "worker.toml"', '',
        ]).encode())
        for effort in REASONING_EFFORTS:
            for original in originals:
                with self.subTest(effort=effort, existing_agents=bool(original)):
                    profile = dict(GlobalRelayConfigTests.profile,
                                   subagent_reasoning_effort=effort)
                    result = _new_config(original, profile)
                    config = _parse(result)
                    self.assertEqual(config['model_reasoning_effort'], 'high')
                    self.assertEqual(config['agents']['default_subagent_reasoning_effort'],
                                     'low' if effort == 'light' else effort)
                    if original:
                        self.assertEqual(config['agents']['max_threads'], 4)
                        self.assertEqual(config['agents']['worker'],
                                         {'config_file': 'worker.toml'})
                        self.assertIn(b'# retained comment', result)
                    self.assertEqual(_parse(_new_config(result, profile)), config)

    def test_new_profiles_default_to_max_and_updates_preserve_each_station_choice(self):
        manager = RelayManager(self.home)
        first = manager.save_profile('first', 'https://first.example/v1',
                                     'synthetic-first-key', model='main-model')
        second = manager.save_profile('second', 'https://second.example/v1',
                                      'synthetic-second-key', model='main-model',
                                      subagent_reasoning_effort='medium')
        self.assertEqual(manager.get_profile(first)['subagent_reasoning_effort'], 'max')
        self.assertEqual(manager.get_profile(first)['subagent_model'], '')
        manager.save_profile('first', 'https://first.example/v1', 'synthetic-first-key',
                             model='main-model', profile_id=first,
                             subagent_reasoning_effort='ultra')
        manager.save_profile('renamed', 'https://first.example/v1', 'synthetic-first-key',
                             model='other-model', profile_id=first)
        reopened = RelayManager(self.home)
        self.assertEqual(reopened.get_profile(first)['subagent_reasoning_effort'], 'ultra')
        self.assertEqual(reopened.get_profile(second)['subagent_reasoning_effort'], 'medium')
        self.assertEqual(reopened.get_profile(first)['reasoning_effort'], 'high')
        self.assertEqual(reopened.get_profile(first)['subagent_model'], '')

    def test_subagent_model_is_optional_and_written_without_changing_main_model(self):
        manager = RelayManager(self.home)
        profile_id = manager.save_profile(
            'station', 'https://relay.example/v1', 'synthetic-model-key',
            model='main-model', subagent_model='child-model',
            subagent_reasoning_effort='medium')
        self.assertEqual(manager.get_profile(profile_id)['subagent_model'], 'child-model')
        manager.switch(profile_id)
        config = _parse(manager.config.read_bytes())
        self.assertEqual(config['model'], 'main-model')
        self.assertEqual(config['agents']['default_subagent_model'], 'child-model')
        self.assertEqual(config['agents']['default_subagent_reasoning_effort'], 'medium')
        manager.save_profile(
            'station', 'https://relay.example/v1', 'synthetic-model-key',
            model='main-model', profile_id=profile_id, subagent_model='')
        manager.switch(profile_id)
        self.assertNotIn('default_subagent_model', _parse(manager.config.read_bytes())['agents'])

    def test_invalid_subagent_model_does_not_modify_saved_profiles_or_config(self):
        manager = RelayManager(self.home)
        profile_id = manager.save_profile(
            'station', 'https://relay.example/v1', 'synthetic-model-key', model='main-model')
        saved = manager.profiles_file.read_bytes()
        with self.assertRaises(RelayError):
            manager.save_profile(
                'station', 'https://relay.example/v1', 'synthetic-model-key',
                model='main-model', profile_id=profile_id, subagent_model='bad\nmodel')
        self.assertEqual(manager.profiles_file.read_bytes(), saved)

    def test_legacy_profile_without_subagent_choice_loads_and_applies_max(self):
        manager = RelayManager(self.home)
        profile_id = manager.save_profile('legacy', 'https://legacy.example/v1',
                                          'synthetic-legacy-key', model='main-model')
        stored = json.loads(manager.profiles_file.read_text(encoding='utf-8'))
        stored['profiles'][0].pop('subagent_reasoning_effort', None)
        manager.profiles_file.write_text(json.dumps(stored), encoding='utf-8')
        reopened = RelayManager(self.home)
        self.assertEqual(reopened.list_profiles()[0]['subagent_reasoning_effort'], 'max')
        self.assertEqual(reopened.get_profile(profile_id)['subagent_reasoning_effort'], 'max')
        reopened.switch(profile_id)
        self.assertEqual(_parse(reopened.config.read_bytes())['agents']
                         ['default_subagent_reasoning_effort'], 'max')

    def test_initial_import_and_current_config_preserve_supported_subagent_strength(self):
        for effort in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            with self.subTest(effort=effort):
                directory = self.home / effort
                directory.mkdir()
                original = chr(10).join([
                    'model_provider = "custom"', 'model = "main-model"',
                    'model_reasoning_effort = "high"', '[agents]',
                    f'default_subagent_reasoning_effort = "{effort}"',
                    '[model_providers.custom]', 'name = "imported"',
                    'base_url = "https://import.example/v1"',
                    'experimental_bearer_token = "synthetic-import-key"', '',
                ]).encode()
                (directory / 'config.toml').write_bytes(original)
                manager = RelayManager(directory)
                expected = 'light' if effort == 'low' else effort
                self.assertEqual(manager.current()['subagent_reasoning_effort'], expected)
                self.assertEqual(manager.list_profiles()[0]['subagent_reasoning_effort'],
                                 expected)
                self.assertEqual(manager.config.read_bytes(), original)

    def test_switch_uses_saved_subagent_choice_and_restore_recovers_exact_config(self):
        original = chr(10).join([
            '# original settings', 'model = "original-model"',
            '[agents]', 'max_threads = 3',
            'default_subagent_reasoning_effort = "medium"',
            '[mcp_servers.existing]', 'command = "keep-existing"', '',
        ]).encode()
        (self.home / 'config.toml').write_bytes(original)
        manager = RelayManager(self.home)
        profile_id = manager.save_profile('station', 'https://relay.example/v1',
                                          'synthetic-switch-key', model='main-model',
                                          subagent_reasoning_effort='light')
        manager.switch(profile_id)
        config = _parse(manager.config.read_bytes())
        self.assertEqual(config['agents'],
                         {'max_threads': 3, 'default_subagent_reasoning_effort': 'low'})
        self.assertEqual(config['model_reasoning_effort'], 'high')
        self.assertEqual(config['mcp_servers'], _parse(original)['mcp_servers'])
        self.assertEqual(manager.current()['subagent_reasoning_effort'], 'light')
        manager.restore()
        self.assertEqual(manager.config.read_bytes(), original)
        self.assertEqual(manager.current()['subagent_reasoning_effort'], 'medium')

    def test_invalid_subagent_choice_does_not_modify_saved_profiles_or_config(self):
        manager = RelayManager(self.home)
        profile_id = manager.save_profile('station', 'https://relay.example/v1',
                                          'synthetic-invalid-key', model='main-model')
        saved = manager.profiles_file.read_bytes()
        for effort in ('invalid', 'low', '', None):
            with self.subTest(effort=effort):
                with self.assertRaises(RelayError):
                    _new_config(b'', dict(GlobalRelayConfigTests.profile,
                                          subagent_reasoning_effort=effort))
        with self.assertRaises(RelayError):
            manager.save_profile('station', 'https://relay.example/v1',
                                 'synthetic-invalid-key', model='main-model',
                                 profile_id=profile_id, subagent_reasoning_effort='invalid')
        self.assertEqual(manager.profiles_file.read_bytes(), saved)
        self.assertFalse(manager.config.exists())


if __name__ == '__main__':
    unittest.main()
