"""Offline tests for the image MCP bridge and its isolated Codex config edit."""
import base64
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import relay_image_mcp as mcp
from relay_core import RelayError, RelayManager, _parse


@unittest.skipUnless(__import__('os').name == 'nt', 'registration locking uses Windows msvcrt')
class MCPRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        self.script = self.root / 'relay_image_mcp.py'
        self.script.write_text('# synthetic service path\n', encoding='utf-8')
        self.python = self.root / 'python.exe'
        self.python.write_text('', encoding='utf-8')
        self.original = (
            '# keep comment\nmodel = "main-model"\n'
            '[mcp_servers.existing]\ncommand = "existing-tool"\n'
            '[other]\napi_key = "synthetic-config-only-secret"\n'
        ).encode('utf-8')
        (self.home / 'config.toml').write_bytes(self.original)
        self.no_lock = patch.object(RelayManager, '_locked', autospec=True,
                                    side_effect=lambda owner: _NoLock())
        self.no_lock.start()
        self.addCleanup(self.no_lock.stop)
        self.crypt = patch('relay_image_mcp._crypt', side_effect=lambda data, encrypt: data)
        self.crypt.start()
        self.addCleanup(self.crypt.stop)

    def register(self):
        return mcp.register_mcp(self.home, self.script, self.python)

    def test_register_edits_only_dedicated_server_and_keeps_credentials_out(self):
        self.assertTrue(self.register()['changed'])
        config_path = self.home / 'config.toml'
        after = _parse(config_path.read_bytes())
        before = _parse(self.original)
        self.assertEqual(after['model'], before['model'])
        self.assertEqual(after['other'], before['other'])
        self.assertEqual(after['mcp_servers']['existing'], before['mcp_servers']['existing'])
        self.assertEqual(after['mcp_servers']['relay_switcher_image'], {
            'enabled': True, 'command': str(self.python),
            'args': [str(self.script), '--home', str(self.home)],
            'tool_timeout_sec': 300,
        })
        self.assertNotIn('synthetic-config-only-secret', json.dumps(after['mcp_servers']['relay_switcher_image']))
        self.assertEqual(config_path.read_bytes().count(b'synthetic-config-only-secret'), 1)
        self.assertTrue((self.home / 'relay-switcher' / 'image-mcp-backup.dpapi').exists())

    def test_registration_is_idempotent_and_preserves_original_backup(self):
        self.register()
        config_path = self.home / 'config.toml'
        backup_path = self.home / 'relay-switcher' / 'image-mcp-backup.dpapi'
        config_before = config_path.read_bytes()
        backup_before = backup_path.read_bytes()
        outcome = self.register()
        self.assertFalse(outcome['changed'])
        self.assertEqual(config_path.read_bytes(), config_before)
        self.assertEqual(backup_path.read_bytes(), backup_before)

    def test_preexisting_original_backup_is_not_overwritten(self):
        backup_path = self.home / 'relay-switcher' / 'image-mcp-backup.dpapi'
        backup_path.parent.mkdir()
        original_backup = b'preexisting-protected-backup'
        backup_path.write_bytes(original_backup)
        self.register()
        self.assertEqual(backup_path.read_bytes(), original_backup)

    def test_registration_does_not_initialize_language_manager_or_read_auth(self):
        with patch.object(RelayManager, '__init__', side_effect=AssertionError('must not import auth')):
            self.assertTrue(self.register()['changed'])

    def test_existing_dedicated_server_is_never_overwritten(self):
        config = self.home / 'config.toml'
        original = self.original + b'\n[mcp_servers.relay_switcher_image]\ncommand = "user-tool"\n'
        config.write_bytes(original)
        with self.assertRaises(RelayError):
            self.register()
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((self.home / 'relay-switcher' / 'image-mcp-backup.dpapi').exists())

    def test_own_registration_preserves_added_settings_and_custom_timeout(self):
        self.register()
        config = self.home / 'config.toml'
        original = config.read_bytes().replace(b'enabled = true', b'enabled = false')
        original = original.replace(b'tool_timeout_sec = 300', b'tool_timeout_sec = 450')
        original += (
            b'# keep client settings\nenabled_tools = ["generate_image"]\n'
            b'[mcp_servers.relay_switcher_image.env]\nEXAMPLE = "synthetic"\n'
        )
        config.write_bytes(original)
        self.assertTrue(self.register()['changed'])
        entry = _parse(config.read_bytes())['mcp_servers'][mcp.SERVER_NAME]
        self.assertTrue(entry['enabled'])
        self.assertEqual(entry['tool_timeout_sec'], 450)
        self.assertEqual(entry['enabled_tools'], ['generate_image'])
        self.assertEqual(entry['env'], {'EXAMPLE': 'synthetic'})
        self.assertIn(b'# keep client settings', config.read_bytes())
        self.assertFalse(self.register()['changed'])

    def test_own_registration_can_update_python_and_moved_script_paths(self):
        self.register()
        config = self.home / 'config.toml'
        before = _parse(config.read_bytes())
        backup = self.home / 'relay-switcher' / 'image-mcp-backup.dpapi'
        original_backup = backup.read_bytes()
        moved = self.root / 'new location'
        moved.mkdir()
        script = moved / self.script.name
        script.write_text('# moved synthetic service\n', encoding='utf-8')
        python = moved / self.python.name
        python.write_text('', encoding='utf-8')
        self.assertTrue(mcp.register_mcp(self.home, script, python)['changed'])
        after = _parse(config.read_bytes())
        entry = after['mcp_servers'][mcp.SERVER_NAME]
        self.assertEqual(entry['command'], str(python))
        self.assertEqual(entry['args'], [str(script), '--home', str(self.home)])
        self.assertEqual(mcp._without_managed_server(before), mcp._without_managed_server(after))
        self.assertEqual(backup.read_bytes(), original_backup)

    def test_own_registration_with_missing_defaults_can_be_upgraded(self):
        self.register()
        config = self.home / 'config.toml'
        original = config.read_bytes().replace(b'enabled = true\n', b'')
        original = original.replace(b'tool_timeout_sec = 300\n', b'')
        config.write_bytes(original)
        self.assertTrue(self.register()['changed'])
        entry = _parse(config.read_bytes())['mcp_servers'][mcp.SERVER_NAME]
        self.assertTrue(entry['enabled'])
        self.assertEqual(entry['tool_timeout_sec'], 300)
        self.assertFalse(self.register()['changed'])

    def test_similar_registration_with_other_home_or_script_is_not_overwritten(self):
        self.register()
        config = self.home / 'config.toml'
        registered = config.read_text(encoding='utf-8')
        for old, new in (
            (str(self.home), str(self.root / 'another-home')),
            (str(self.script), str(self.root / 'other_service.py')),
            (str(self.python), str(self.root / 'other-tool.exe')),
        ):
            with self.subTest(changed=Path(old).name):
                original = registered.replace(json.dumps(old), json.dumps(new)).encode('utf-8')
                self.assertNotEqual(original, registered.encode('utf-8'))
                config.write_bytes(original)
                with self.assertRaises(RelayError):
                    self.register()
                self.assertEqual(config.read_bytes(), original)

    def test_concurrent_config_change_fails_closed_and_restores_backup(self):
        config = self.home / 'config.toml'
        backup = self.home / 'relay-switcher' / 'image-mcp-backup.dpapi'
        backup.parent.mkdir()
        concurrent = self.original + b'\n[concurrent]\nchanged = true\n'
        original_atomic = mcp._atomic

        def modify_during_backup(path, data):
            original_atomic(path, data)
            if path == backup:
                config.write_bytes(concurrent)

        with patch('relay_image_mcp._atomic', side_effect=modify_during_backup):
            with self.assertRaises(RelayError):
                self.register()
        self.assertEqual(config.read_bytes(), concurrent)
        self.assertFalse(backup.exists())

    def test_pythonw_path_is_normalized_to_console_python(self):
        pythonw = self.root / 'pythonw.exe'
        pythonw.write_text('', encoding='utf-8')
        self.assertEqual(mcp._console_python(pythonw), self.python.resolve())

    def test_inline_or_dotted_server_conflicts_are_rejected(self):
        for config in (
            b'mcp_servers = { relay_switcher_image = { command = "x" } }\n',
            b'mcp_servers.relay_switcher_image.command = "x"\n',
            b'[mcp_servers]\nrelay_switcher_image = { command = "x" }\n',
        ):
            with self.subTest(config=config):
                with self.assertRaises(RelayError):
                    mcp._registered_config(config, {
                        'enabled': True, 'command': str(self.python),
                        'args': [str(self.script), '--home', str(self.home)],
                        'tool_timeout_sec': 300,
                    })


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.home = Path('C:/synthetic-codex-home')
        self.image = b'\x89PNG\r\n\x1a\nsynthetic-image-bytes'
        self.saved = Path('D:/codex-/image/test image.png')
        self.manager_calls = []
        self.generator_calls = []

        def manager_factory(home):
            manager = object()
            self.manager_calls.append((home, manager))
            return manager

        def generator(manager, prompt, *, size, quality, **options):
            self.generator_calls.append((manager, prompt, size, quality, options))
            return {
                'path': self.saved, 'mime_type': 'image/png', 'width': 1254,
                'height': 1254, 'data': self.image, 'model': 'test-image-model',
            }

        self.server = mcp.ImageMCPServer(self.home, manager_factory=manager_factory,
                                         generator=generator)

    def test_newline_transport_handles_initialize_tools_and_notification(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'},
        ]
        output = io.StringIO()
        self.server.serve(io.StringIO(''.join(json.dumps(item) + '\n' for item in messages)), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item['id'] for item in responses], [1, 2, 3])
        self.assertEqual(responses[0]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(responses[1]['result'], {})
        tool = responses[2]['result']['tools'][0]
        self.assertEqual(tool['name'], 'generate_image')
        self.assertIn('explicitly asks to generate an image or edit an image', tool['description'])
        self.assertEqual(tool['inputSchema']['required'], ['prompt'])
        self.assertNotIn('n', tool['inputSchema']['properties'])
        properties = tool['inputSchema']['properties']
        self.assertEqual(properties['input_images']['type'], 'array')
        self.assertEqual(properties['mask']['type'], 'string')
        self.assertEqual(properties['model']['type'], 'string')
        self.assertNotIn('default', properties['size'])
        self.assertNotIn('default', properties['quality'])
        self.assertIn('Canva', tool['description'])
        self.assertIn('never automatically', tool['description'])
        self.assertIn('image-edit requests', responses[0]['result']['instructions'])

    def test_malformed_message_does_not_stop_following_requests(self):
        output = io.StringIO()
        self.server.serve(io.StringIO('{invalid}\n' + json.dumps({
            'jsonrpc': '2.0', 'id': 'next', 'method': 'ping',
        }) + '\n'), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]['error']['code'], -32700)
        self.assertEqual(responses[1], {'jsonrpc': '2.0', 'id': 'next', 'result': {}})

    def test_tools_call_notification_never_generates_or_responds(self):
        self.assertIsNone(self.server.handle({
            'jsonrpc': '2.0', 'method': 'tools/call',
            'params': {'name': 'generate_image', 'arguments': {'prompt': 'one'}},
        }))
        self.assertEqual(self.generator_calls, [])

    def test_call_returns_inline_image_and_absolute_local_markdown_link(self):
        result = self.server.handle({
            'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
            'params': {'name': 'generate_image', 'arguments': {'prompt': 'A test image'}},
        })['result']
        self.assertFalse(result['isError'])
        self.assertEqual([item['type'] for item in result['content']], ['text', 'image'])
        self.assertEqual(base64.b64decode(result['content'][1]['data']), self.image)
        self.assertEqual(result['content'][1]['mimeType'], 'image/png')
        self.assertEqual(result['structuredContent']['width'], 1254)
        self.assertIn('![生成的图片](<D:/codex-/image/test image.png>)', result['content'][0]['text'])
        self.assertEqual(self.generator_calls[0][1:4], ('A test image', None, None))
        self.assertEqual(self.generator_calls[0][4], {})

    def test_call_forwards_explicit_edit_inputs_and_per_call_options(self):
        result = self.server.handle({
            'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
            'params': {'name': 'generate_image', 'arguments': {
                'prompt': 'Edit the supplied image', 'input_images': ['D:/reference.png'],
                'mask': 'D:/mask.png', 'model': 'cached-image-model',
                'quality': 'xhigh', 'size': '2048x1024',
            }},
        })['result']
        self.assertFalse(result['isError'])
        self.assertEqual(self.generator_calls[0][1:4],
                         ('Edit the supplied image', '2048x1024', 'xhigh'))
        self.assertEqual(self.generator_calls[0][4], {
            'input_images': ['D:/reference.png'], 'mask': 'D:/mask.png',
            'model': 'cached-image-model',
        })

    def test_each_call_reloads_manager_so_enable_switch_is_live(self):
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'generate_image', 'arguments': {'prompt': 'one'}}}
        self.server.handle(message)
        self.server.handle(dict(message, id=2))
        self.assertEqual(len(self.manager_calls), 2)
        self.assertIsNot(self.manager_calls[0][1], self.manager_calls[1][1])

    def test_closed_backend_error_is_explicit_and_does_not_retry(self):
        calls = []

        def closed_generator(*args, **kwargs):
            calls.append(1)
            raise RelayError('生图功能当前关闭；本次请求未发送到图片中转站。')

        server = mcp.ImageMCPServer(self.home, manager_factory=lambda home: object(),
                                    generator=closed_generator)
        result = server.handle({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'generate_image', 'arguments': {'prompt': 'test'}},
        })['result']
        self.assertTrue(result['isError'])
        self.assertIn('生图功能当前关闭', result['content'][0]['text'])
        self.assertEqual(calls, [1])

    def test_invalid_arguments_never_call_backend(self):
        result = self.server.handle({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'generate_image', 'arguments': {
                'prompt': 'test', 'n': 12,
            }},
        })['result']
        self.assertTrue(result['isError'])
        self.assertEqual(self.manager_calls, [])
        self.assertEqual(self.generator_calls, [])

    def test_invalid_edit_argument_shapes_never_call_backend(self):
        for options in (
                {'input_images': 'D:/reference.png'},
                {'input_images': []},
                {'input_images': ['relative.png']},
                {'mask': 'D:/mask.png'},
                {'input_images': ['D:/reference.png'], 'mask': 7},
                {'model': []},
                {'size': '9' * 5000 + 'x16'},
        ):
            with self.subTest(options=options):
                result = self.server.handle({
                    'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call',
                    'params': {'name': 'generate_image',
                               'arguments': {'prompt': 'Edit', **options}},
                })['result']
                self.assertTrue(result['isError'])
        self.assertEqual(self.manager_calls, [])
        self.assertEqual(self.generator_calls, [])

    def test_unverified_quality_values_and_bounded_custom_size_reach_backend(self):
        for quality in ('xhigh', 'max'):
            result = self.server.handle({
                'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                'params': {'name': 'generate_image',
                           'arguments': {'prompt': 'one', 'quality': quality,
                                         'size': '4096x4096'}},
            })['result']
            self.assertFalse(result['isError'])
            self.assertEqual(self.generator_calls[-1][2:], ('4096x4096', quality, {}))

    def test_unexpected_backend_error_is_sanitized(self):
        def failing_generator(*args, **kwargs):
            raise RuntimeError('secret-key=https://relay.example/key')

        server = mcp.ImageMCPServer(self.home, manager_factory=lambda home: object(),
                                    generator=failing_generator)
        result = server.handle({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'generate_image', 'arguments': {'prompt': 'test'}},
        })['result']
        self.assertTrue(result['isError'])
        self.assertNotIn('secret-key', result['content'][0]['text'])
        self.assertNotIn('relay.example', result['content'][0]['text'])


class AdditionalProtocolTests(unittest.TestCase):
    def test_backend_diagnostics_do_not_appear_in_protocol(self):
        import contextlib
        import sys
        def generator(*args, **kwargs):
            print('synthetic-secret-stdout')
            print('synthetic-secret-stderr', file=sys.stderr)
            raise RuntimeError('synthetic-error')
        server = mcp.ImageMCPServer(Path.cwd(), manager_factory=lambda home: object(), generator=generator)
        diagnostics = io.StringIO()
        output = io.StringIO()
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'generate_image', 'arguments': {'prompt': 'one'}}}
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            server.serve(io.StringIO(json.dumps(message) + chr(10)), output)
        self.assertEqual(diagnostics.getvalue(), '')
        self.assertTrue(json.loads(output.getvalue())['result']['isError'])
        self.assertNotIn('synthetic-secret', output.getvalue())

    def test_container_sizes_and_qualities_return_error_without_backend(self):
        def never(*args, **kwargs):
            raise AssertionError('backend must not be called')
        server = mcp.ImageMCPServer(Path.cwd(), manager_factory=never, generator=never)
        for field in ('size', 'quality'):
            for value in ([], {}, None, 1):
                with self.subTest(field=field, value=value):
                    arguments = {'prompt': 'one', field: value}
                    result = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                            'params': {'name': 'generate_image', 'arguments': arguments}})
                    self.assertTrue(result['result']['isError'])

class MCPProcessTests(unittest.TestCase):
    def test_console_service_initializes_without_reading_user_config(self):
        import subprocess
        import sys
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / 'never-created-home'
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                 'params': {'protocolVersion': '2025-06-18'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            ]
            result = subprocess.run(
                [sys.executable, str(Path(mcp.__file__).resolve()), '--home', str(home)],
                input=''.join(json.dumps(item) + chr(10) for item in requests),
                capture_output=True, text=True, encoding='utf-8', timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([item['id'] for item in responses], [1, 2])
            self.assertEqual(responses[1]['result']['tools'][0]['name'], 'generate_image')
            self.assertFalse(home.exists())



class MCPBackendIntegrationTests(unittest.TestCase):
    def test_real_backend_saves_inline_result_and_live_disable_blocks_network(self):
        from relay_images import ImageRelayManager
        from test_relay_images import FakeResponse, _png
        folder = Path(__file__).resolve().parent / '.verification'
        folder.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=folder, prefix='mcp-backend-') as temporary:
            home = Path(temporary)
            manager = ImageRelayManager(home)
            profile_id = manager.save_profile('synthetic image', 'https://images.example/v1',
                                              'synthetic-integration-key', 'image-model')
            manager.apply(profile_id, 'image-model', str(home / 'images'))
            manager.set_enabled(True)
            image = _png(3, 2)
            payload = json.dumps({'data': [{'b64_json': base64.b64encode(image).decode()}]}).encode()
            server = mcp.ImageMCPServer(home)
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': 'generate_image', 'arguments': {'prompt': '生成一张测试图片'}}}
            with patch('relay_images._open', return_value=FakeResponse(payload)) as network:
                result = server.handle(request)['result']
                self.assertFalse(result['isError'])
                saved = Path(result['structuredContent']['path'])
                self.assertEqual(saved.parent, home / 'images')
                self.assertEqual(saved.read_bytes(), image)
                self.assertEqual(base64.b64decode(result['content'][1]['data']), image)
                self.assertNotIn('synthetic-integration-key', json.dumps(result))
                manager.set_enabled(False)
                disabled = server.handle(dict(request, id=2))['result']
                self.assertTrue(disabled['isError'])
                self.assertIn('已关闭', disabled['content'][0]['text'])
                self.assertEqual(network.call_count, 1)
                self.assertEqual(len(list((home / 'images').iterdir())), 1)

    def test_registered_console_process_discovers_tool_and_reports_disabled(self):
        import subprocess
        import sys
        folder = Path(__file__).resolve().parent / '.verification'
        folder.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=folder, prefix='mcp-registered-') as temporary:
            home = Path(temporary)
            mcp.register_mcp(home)
            entry = _parse((home / 'config.toml').read_bytes())['mcp_servers']['relay_switcher_image']
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                 'params': {'name': 'generate_image', 'arguments': {'prompt': '测试关闭状态'}}},
            ]
            process = subprocess.run([entry['command'], *entry['args']],
                                     input=chr(10).join(json.dumps(r) for r in requests) + chr(10),
                                     capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stderr, '')
            responses = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual([r['id'] for r in responses], [1, 2, 3])
            self.assertEqual(responses[1]['result']['tools'][0]['name'], 'generate_image')
            self.assertTrue(responses[2]['result']['isError'])
            self.assertIn('已关闭', responses[2]['result']['content'][0]['text'])

if __name__ == '__main__':
    unittest.main()
