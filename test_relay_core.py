"""Regression checks for global relay switching, using synthetic configuration."""
import unittest

from relay_core import REASONING_EFFORTS, _global_provider_ids, _new_config, _parse


class GlobalRelayConfigTests(unittest.TestCase):
    profile = dict(name='36524', base_url='https://new.example/v1',
                   api_key='synthetic-test-token', model='test-model', reasoning_effort='high')

    def test_existing_legacy_id_uses_new_station(self):
        original = b'''# keep this comment
model_provider = "relay_switcher"
model = "old-model"
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
                self.assertEqual(after['agents']['default_subagent_reasoning_effort'], 'max')
        profile = {key: value for key, value in self.profile.items() if key != 'reasoning_effort'}
        self.assertEqual(_parse(_new_config(b'', profile))['model_reasoning_effort'], 'high')


if __name__ == '__main__':
    unittest.main()
