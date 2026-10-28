"""GUI checks with synthetic credentials, isolated homes, and no network."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from relay_core import RelayError, _parse
from relay_models import ModelCatalog

loader = importlib.machinery.SourceFileLoader('relay_switcher_gui_tests', str(Path(__file__).with_name('relay_switcher.pyw')))
spec = importlib.util.spec_from_loader(loader.name, loader)
gui = importlib.util.module_from_spec(spec)
loader.exec_module(gui)

@unittest.skipUnless(os.name == 'nt', 'The application requires Windows DPAPI')
class ImagePageTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parent / '.verification'
        folder.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(prefix='gui-image-', dir=folder)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        self.config = self.home / 'config.toml'
        self.original = chr(10).join(['model = "language-model"', '[mcp_servers.existing]', 'command = "keep-existing"', '']).encode()
        self.config.write_bytes(self.original)
        error_patch = patch.object(gui, 'show_error')
        self.errors = error_patch.start()
        self.addCleanup(error_patch.stop)
        info_patch = patch.object(gui.messagebox, 'showinfo')
        info_patch.start()
        self.addCleanup(info_patch.stop)
        network_patch = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network allowed'))
        network_patch.start()
        self.addCleanup(network_patch.stop)
        self.app = gui.Switcher(self.home)
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self.callback_errors = []
        self.app.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.profile_id = self.app.image_manager.save_profile(
            'synthetic image relay', 'https://images.example/v1', 'synthetic-image-test-key',
            model='image-model', models=['image-model', 'alternate-image-model'])
        self.app.refresh_images(self.profile_id)
        self.app.update()
        self.errors.assert_not_called()

    def tearDown(self):
        self.assertEqual(self.callback_errors, [])

    def test_tabs_defaults_and_model_selection_are_independent(self):
        self.assertEqual([self.app.tabs.tab(item, 'text') for item in self.app.tabs.tabs()], ['语言模型中转', '生图中转'])
        self.assertEqual(self.app.selected_effort.get(), 'high')
        self.assertEqual(self.app.selected_subagent_effort.get(), 'max')
        self.assertEqual(self.app.selected_subagent_model.get(), '')
        self.assertEqual(tuple(self.app.subagent_effort_combo['values']), gui.REASONING_EFFORTS)
        self.assertEqual(str(self.app.subagent_effort_combo['state']), 'disabled')
        self.assertIn('OpenAI 官方模型能力目录', self.app.official_reasoning_status.get())
        self.assertEqual(self.app.selected_image_model.get(), 'image-model')
        self.assertIn('alternate-image-model', self.app.image_model_combo['values'])
        self.assertEqual(self.app.image_output_text.get(), '图片保存位置：' + gui.DEFAULT_OUTPUT_DIR)
        self.assertFalse(self.app.image_enabled.get())
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_subagent_selector_applies_and_preserves_independent_station_settings(self):
        first = self.app.manager.save_profile(
            'first language relay', 'https://first.example/v1', 'synthetic-language-key',
            model='language-model')
        second = self.app.manager.save_profile(
            'second language relay', 'https://second.example/v1', 'synthetic-other-key',
            model='language-model', subagent_reasoning_effort='medium')
        self.app.refresh(first)
        self.app.update()
        self.assertEqual(self.app.selected_subagent_effort.get(), 'max')
        self.assertEqual(str(self.app.subagent_effort_combo['state']), 'readonly')
        self.assertEqual(str(self.app.effort_combo['state']), 'readonly')
        self.app.selected_subagent_model.set('child-model')
        self.app.selected_subagent_effort.set('light')
        self.assertEqual(self.config.read_bytes(), self.original)
        self.app.switch()
        self.errors.assert_not_called()
        config = _parse(self.config.read_bytes())
        self.assertEqual(config['model_reasoning_effort'], 'high')
        self.assertEqual(config['agents']['default_subagent_reasoning_effort'], 'low')
        self.assertEqual(config['agents']['default_subagent_model'], 'child-model')
        self.assertEqual(config['mcp_servers'], _parse(self.original)['mcp_servers'])
        self.assertEqual(self.app.selected_subagent_effort.get(), 'light')
        self.app.tree.selection_set(second)
        self.app.update()
        self.assertEqual(self.app.selected_subagent_model.get(), '')
        self.assertEqual(self.app.selected_subagent_effort.get(), 'medium')
        self.app.tree.selection_set(first)
        self.app.update()
        self.assertEqual(self.app.selected_subagent_effort.get(), 'light')
        profile = self.app.manager.get_profile(first)
        dialog = gui.ProfileDialog(self.app, self.app.manager, profile)
        try:
            dialog.save()
            self.app.refresh(first)
            self.assertEqual(self.app.selected_subagent_effort.get(), 'light')
            self.assertEqual(self.app.manager.get_profile(first)['subagent_reasoning_effort'],
                             'light')
        finally:
            profile.clear()
            if dialog.winfo_exists():
                dialog.destroy()
        self.errors.assert_not_called()

    def test_apply_registers_only_image_tool_and_toggle_is_live(self):
        self.app.apply_image()
        registered_config = self.config.read_bytes()
        existing_manager = self.app.image_manager
        self.assertEqual(existing_manager.generation_profile()['model'], 'image-model')
        self.app.selected_image_model.set('alternate-image-model')
        with patch.object(gui.messagebox, 'showinfo') as notification:
            self.app.apply_image()
        self.assertEqual(self.config.read_bytes(), registered_config)
        self.assertIs(self.app.image_manager, existing_manager)
        self.assertIn('无需重启 Codex', notification.call_args.args[1])
        self.assertEqual(self.app.image_tree.heading('model', 'text'), '当前生图模型')
        self.assertEqual(self.app.image_apply_button.cget('text'), '应用')
        self.errors.assert_not_called()
        settings = self.app.image_manager.settings()
        self.assertTrue(settings['enabled'])
        self.assertEqual(settings['output_dir'], gui.DEFAULT_OUTPUT_DIR)
        self.assertEqual(self.app.image_manager.generation_profile()['model'], 'alternate-image-model')
        config = _parse(self.config.read_bytes())
        server = config['mcp_servers'].pop('relay_switcher_image')
        self.assertEqual(config, _parse(self.original))
        self.assertEqual(server['tool_timeout_sec'], 300)
        self.assertNotIn('synthetic-image-test-key', str(server))
        self.app.image_enabled.set(False)
        self.app.toggle_images()
        self.assertFalse(self.app.image_manager.settings()['enabled'])
        with self.assertRaisesRegex(RelayError, '已关闭'):
            self.app.image_manager.generation_profile()
        self.app.image_enabled.set(True)
        self.app.toggle_images()
        self.assertTrue(self.app.image_manager.settings()['enabled'])

    def test_registration_conflict_keeps_generation_disabled(self):
        conflict = self.original + chr(10).join(['', '[mcp_servers.relay_switcher_image]', 'command = "another-tool"', '']).encode()
        self.config.write_bytes(conflict)
        self.app.apply_image()
        self.errors.assert_called_once()
        self.assertEqual(self.config.read_bytes(), conflict)
        self.assertFalse(self.app.image_manager.settings()['enabled'])
        self.assertFalse(self.app.image_enabled.get())
        self.assertIn('未全部应用', self.app.image_status.get())

    def test_toggle_before_apply_does_not_register_or_enable(self):
        self.app.image_enabled.set(True)
        self.app.toggle_images()
        self.errors.assert_called_once()
        self.assertFalse(self.app.image_enabled.get())
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_broken_settings_do_not_raise_second_toggle_error(self):
        with patch.object(self.app.image_manager, 'settings', side_effect=RelayError('invalid synthetic settings')):
            self.app.image_enabled.set(True)
            self.app.toggle_images()
        self.errors.assert_called_once()
        self.assertFalse(self.app.image_enabled.get())

    def test_failed_home_refresh_restores_both_managers(self):
        previous, previous_image = self.app.manager, self.app.image_manager
        other = self.root / 'other-home'
        original_refresh = self.app.refresh_images
        def refresh_or_fail(*args, **kwargs):
            if self.app.image_manager.home == other.resolve():
                raise RelayError('synthetic refresh failure')
            return original_refresh(*args, **kwargs)
        with (patch.object(gui.filedialog, 'askdirectory', return_value=str(other)),
              patch.object(self.app, 'refresh_images', side_effect=refresh_or_fail)):
            self.app.choose_home()
        self.assertIs(self.app.manager, previous)
        self.assertIs(self.app.image_manager, previous_image)
        self.assertIn(self.profile_id, self.app.image_profiles)
        self.errors.assert_called_once()

    def test_image_dialog_normalizes_model_endpoint_and_saves_separate_key(self):
        dialog = gui.ProfileDialog(self.app, self.app.image_manager, image=True)
        try:
            dialog.vars['api_key'].set('synthetic-second-image-key')
            dialog.vars['base_url'].set('https://images.example/v1/images/generations')
            dialog.vars['model'].set('manual-image-model')
            def immediate(owner, base_url, api_key, on_success, on_error):
                self.assertEqual(base_url, 'https://images.example/v1')
                on_success(ModelCatalog(base_url, ['manual-image-model']))
            with patch.object(gui, 'ModelRequest', side_effect=immediate):
                dialog.fetch()
            self.assertIn('图片模型', dialog.model_status.get())
            self.assertNotIn('写入 Codex', dialog.model_status.get())
            dialog.save()
            profile = self.app.image_manager.get_profile(dialog.saved_id)
            self.assertEqual(profile['base_url'], 'https://images.example/v1')
            self.assertEqual(profile['api_key'], 'synthetic-second-image-key')
            self.assertEqual(self.config.read_bytes(), self.original)
            self.errors.assert_not_called()
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

    def test_image_dialog_rejects_plain_http_before_fetching(self):
        dialog = gui.ProfileDialog(self.app, self.app.image_manager, image=True)
        try:
            dialog.vars['base_url'].set('http://images.example/v1')
            with patch.object(gui, 'ModelRequest') as request:
                dialog.fetch()
            request.assert_not_called()
            self.errors.assert_called_once()
            self.assertFalse(dialog.fetching)
        finally:
            dialog.destroy()

if __name__ == '__main__':
    unittest.main()
