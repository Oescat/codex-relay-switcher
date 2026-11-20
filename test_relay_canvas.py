"""Offline connection handoff checks; all credentials and homes are synthetic."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from relay_canvas import main
from relay_core import RelayManager
from relay_images import ImageRelayManager


@contextmanager
def _no_lock(self):
    yield


class CanvasConnectionTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parent / '.verification'
        folder.mkdir(exist_ok=True)
        self.temporary = TemporaryDirectory(prefix='canvas-connection-', dir=folder)
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.crypto = patch('relay_images._crypt', side_effect=lambda data, encrypt: data)
        self.crypto.start()
        self.addCleanup(self.crypto.stop)
        self.lock = patch.object(RelayManager, '_locked', _no_lock)
        self.lock.start()
        self.addCleanup(self.lock.stop)

    def read_pipe(self, *mode):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(['--home', str(self.home), *mode])
        return code, output.getvalue(), error.getvalue()

    def test_missing_source_is_read_only_and_has_no_connection(self):
        code, output, error = self.read_pipe('--connection-json')
        self.assertEqual((code, json.loads(output), error), (0, None, ''))
        self.assertFalse((self.home / 'relay-switcher').exists())
        code, output, error = self.read_pipe('--source-path')
        self.assertEqual(json.loads(output), str(self.home / 'relay-switcher' / 'image-profiles.dpapi'))
        self.assertFalse((self.home / 'relay-switcher').exists())

    def test_only_applied_credentials_follow_switch_without_copying_model(self):
        manager = ImageRelayManager(self.home)
        first = manager.save_profile('first', 'https://first.example/v1', 'synthetic-first', model='one')
        second = manager.save_profile('second', 'https://second.example/v1', 'synthetic-second', model='two')
        self.assertIsNone(manager.canvas_connection())
        for profile_id, name, key in ((first, 'first', 'synthetic-first'), (second, 'second', 'synthetic-second')):
            manager.apply(profile_id, 'independent-model', str(self.home / 'images'))
            manager.set_enabled(False)
            before = manager.profiles_file.read_bytes()
            code, output, error = self.read_pipe('--connection-json')
            self.assertEqual((code, error), (0, ''))
            self.assertEqual(json.loads(output), dict(name=name, baseUrl=f'https://{name}.example/v1', apiKey=key))
            self.assertEqual(manager.profiles_file.read_bytes(), before)

    def test_failure_never_leaks_backend_exception_or_credentials(self):
        manager = ImageRelayManager(self.home)
        with patch.object(ImageRelayManager, 'canvas_connection', side_effect=RuntimeError('synthetic-secret')):
            code, output, error = self.read_pipe('--connection-json')
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertNotIn('synthetic-secret', error)
        self.assertIn('Cannot read', error)


if __name__ == '__main__':
    unittest.main()
