"""Tests for the independent image relay and image response handling."""
import base64
from contextlib import contextmanager
from email import policy
import io
import json
import os
from email.parser import BytesParser
from email.message import Message
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zlib

import relay_images
from relay_core import RelayError, RelayManager
from relay_images import ImageRelayManager, generate_image


@contextmanager
def _no_lock(self):
    yield


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def _png(width=1, height=1, color_type=2):
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    pixel = b"\x12\x34\x56" if color_type == 2 else b"\x12\x34\x56\xff"
    pixels = b"".join(b"\x00" + pixel * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(pixels)) + _chunk(b"IEND", b""))


class FakeResponse:
    def __init__(self, body, status=200, content_type="application/json"):
        self._body = body
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))

    def getcode(self):
        return self.status

    def read(self, length=-1):
        if length < 0:
            result, self._body = self._body, b""
        else:
            result, self._body = self._body[:length], self._body[length:]
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _multipart_message(request):
    content_type = request.get_header("Content-type")
    raw = (b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + request.data)
    return BytesParser(policy=policy.default).parsebytes(raw)


class _ImageTestBase:
    def setUp(self):
        verification = Path(__file__).resolve().parent / ".verification"
        verification.mkdir(exist_ok=True)
        self.temporary = TemporaryDirectory(prefix="image-backend-", dir=verification)
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.crypto = patch("relay_images._crypt", side_effect=lambda data, encrypt: data)
        self.crypto.start()
        self.addCleanup(self.crypto.stop)
        self.lock = patch.object(RelayManager, "_locked", _no_lock)
        self.lock.start()
        self.addCleanup(self.lock.stop)
        self.manager = ImageRelayManager(self.home)

    def _profile(self, name="image relay", api_key="image-secret", model="gpt-image-2"):
        return self.manager.save_profile(name, "https://images.example/v1", api_key, model)


class ImageRelayManagerTests(_ImageTestBase, unittest.TestCase):
    def test_image_settings_are_independent_and_default_disabled(self):
        config = self.home / "config.toml"
        config.write_text("language-config-sentinel", encoding="utf-8")
        profile_id = self._profile()
        listed = self.manager.list_profiles()
        self.assertNotIn("api_key", listed[0])
        self.assertEqual(self.manager.get_profile(profile_id)["api_key"], "image-secret")
        self.assertEqual(self.manager.settings(), {
            "active_id": None, "enabled": False, "output_dir": relay_images.DEFAULT_OUTPUT_DIR,
        })
        self.assertEqual(config.read_text(encoding="utf-8"), "language-config-sentinel")
        self.assertTrue(self.manager.profiles_file.name.startswith("image-"))
        self.assertNotEqual(self.manager.profiles_file, self.home / "config.toml")

    def test_apply_preserves_runtime_toggle_and_enable_requires_complete_profile(self):
        with self.assertRaises(RelayError):
            self.manager.set_enabled(True)
        profile_id = self._profile(model="")
        with self.assertRaises(RelayError):
            self.manager.set_enabled(True)
        target = str(self.home / "generated")
        self.manager.apply(profile_id, "gpt-image-2", target)
        self.assertFalse(self.manager.settings()["enabled"])
        self.manager.set_enabled(True)
        self.assertTrue(self.manager.settings()["enabled"])
        self.manager.apply(profile_id, "gpt-image-2", str(self.home / "other"))
        self.assertTrue(self.manager.settings()["enabled"])
        self.assertEqual(self.manager.settings()["output_dir"], str((self.home / "other").resolve()))
        self.manager.set_enabled(False)
        with self.assertRaisesRegex(RelayError, "已关闭"):
            self.manager.generation_profile()

    def test_deleting_active_profile_disables_image_generation(self):
        profile_id = self._profile()
        self.manager.apply(profile_id, "gpt-image-2", str(self.home / "out"))
        self.manager.set_enabled(True)
        self.manager.delete_profile(profile_id)
        self.assertEqual(self.manager.settings()["active_id"], None)
        self.assertFalse(self.manager.settings()["enabled"])

    def test_active_connection_or_model_edits_disable_generation(self):
        profile_id = self._profile()
        self.manager.apply(profile_id, "gpt-image-2", str(self.home / "out"))
        changes = {"base_url": "https://other-images.example/v1",
                   "api_key": "replacement-synthetic-secret", "model": "new-image-model"}
        for field, replacement in changes.items():
            with self.subTest(field=field):
                self.manager.set_enabled(True)
                profile = self.manager.get_profile(profile_id)
                fields = {key: profile[key] for key in ("name", "base_url", "api_key", "model")}
                fields[field] = replacement
                self.manager.save_profile(**fields, profile_id=profile_id)
                self.assertFalse(self.manager.settings()["enabled"])
                with self.assertRaisesRegex(RelayError, "已关闭"):
                    self.manager.generation_profile()

    def test_name_and_model_catalog_edits_do_not_disable_active_generation(self):
        profile_id = self._profile()
        self.manager.apply(profile_id, "gpt-image-2", str(self.home / "out"))
        self.manager.set_enabled(True)
        self.manager.save_profile("renamed image relay", "https://images.example/v1",
                                  "image-secret", "gpt-image-2", profile_id=profile_id,
                                  models=["gpt-image-2", "another-image-model"])
        self.assertTrue(self.manager.settings()["enabled"])

    def test_model_refresh_endpoint_change_disables_active_generation(self):
        profile_id = self._profile()
        self.manager.apply(profile_id, "gpt-image-2", str(self.home / "out"))
        self.manager.set_enabled(True)
        self.manager.save_models(profile_id, ["gpt-image-2"],
                                 resolved_base_url="https://images.example/v1/models")
        self.assertTrue(self.manager.settings()["enabled"])
        self.manager.save_models(profile_id, ["gpt-image-2"],
                                 resolved_base_url="https://new-images.example/v1")
        self.assertFalse(self.manager.settings()["enabled"])

    def test_normalizes_openai_image_endpoints_and_rejects_insecure_remote_http(self):
        self.assertEqual(relay_images._normalize_base_url("https://images.example/v1/models"),
                         "https://images.example/v1")
        self.assertEqual(relay_images._normalize_base_url(
            "https://images.example/v1/images/generations"), "https://images.example/v1")
        with self.assertRaises(RelayError):
            self.manager.save_profile("bad", "http://images.example/v1", "key", "model")

    def test_model_specific_image_defaults_restore_and_legacy_defaults_survive(self):
        profile_id = self._profile()
        self.manager.apply(profile_id, "gpt-image-2", str(self.home / "out"),
                           quality="high", size="2048x1024")
        self.manager.set_enabled(True)
        current = self.manager.generation_profile()
        self.assertEqual(current["default_quality"], "high")
        self.assertEqual(current["default_size"], "2048x1024")

        self.manager.apply(profile_id, "another-image-model", str(self.home / "out"))
        current = self.manager.generation_profile()
        self.assertEqual(current["default_quality"], "low")
        self.assertEqual(current["default_size"], "1024x1024")

        self.manager.apply(profile_id, "gpt-image-2", str(self.home / "out"))
        current = self.manager.generation_profile()
        self.assertEqual(current["default_quality"], "high")
        self.assertEqual(current["default_size"], "2048x1024")

    def test_legacy_profile_without_model_settings_uses_default_image_options(self):
        profile_id = self._profile()
        self.manager.apply(profile_id, "gpt-image-2", str(self.home / "out"))
        self.manager.set_enabled(True)
        stored = json.loads(self.manager.profiles_file.read_text(encoding="utf-8"))
        stored["profiles"][0].pop("model_settings", None)
        self.manager.profiles_file.write_text(json.dumps(stored), encoding="utf-8")
        current = self.manager.generation_profile()
        self.assertEqual(current["default_quality"], "low")
        self.assertEqual(current["default_size"], "1024x1024")

    def test_invalid_image_defaults_do_not_disable_or_mutate_active_profile(self):
        profile_id = self._profile()
        self.manager.apply(profile_id, "gpt-image-2", str(self.home / "out"),
                           quality="high", size="2048x1024")
        self.manager.set_enabled(True)
        with self.assertRaises(RelayError):
            self.manager.apply(profile_id, "another-image-model", str(self.home / "out"),
                               quality="max", size="4096x4112")
        current = self.manager.generation_profile()
        self.assertEqual(current["model"], "gpt-image-2")
        self.assertEqual(current["default_quality"], "high")
        self.assertEqual(current["default_size"], "2048x1024")

    def test_apply_merges_per_model_settings_and_current_options_override(self):
        profile_id = self._profile()
        self.manager.apply(
            profile_id, "gpt-image-2", str(self.home / "out"),
            quality="high", size="2048x1024",
            model_settings={
                "gpt-image-2": {"quality": "medium", "size": "1536x1024"},
                "cached-image-model": {"quality": "auto", "size": "1000x1024"},
            })
        self.manager.set_enabled(True)
        profile = self.manager.get_profile(profile_id)
        self.assertEqual(profile["model_settings"], {
            "gpt-image-2": {"quality": "high", "size": "2048x1024"},
            "cached-image-model": {"quality": "auto", "size": "1000x1024"},
        })
        self.manager.apply(profile_id, "cached-image-model", str(self.home / "out"))
        current = self.manager.generation_profile()
        self.assertEqual(current["default_quality"], "auto")
        self.assertEqual(current["default_size"], "1000x1024")


class ImageGenerationTests(_ImageTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.output_dir = self.home / "rendered"
        self.profile_id = self._profile()
        self.manager.apply(self.profile_id, "gpt-image-2", str(self.output_dir))
        self.manager.set_enabled(True)

    def test_request_saves_verified_png_and_uses_single_result(self):
        png = _png(2, 3)
        response_body = json.dumps({"data": [{"b64_json": base64.b64encode(png).decode("ascii")}]}).encode()
        seen = []

        def open_request(request, timeout):
            seen.append((request, timeout))
            return FakeResponse(response_body)

        with patch("relay_images._open", side_effect=open_request):
            result = generate_image(self.manager, "A test image")
        self.assertEqual(len(seen), 1)
        request = seen[0][0]
        self.assertEqual(request.full_url, "https://images.example/v1/images/generations")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer image-secret")
        payload = json.loads(request.data)
        self.assertEqual(payload["n"], 1)
        self.assertEqual(payload["model"], "gpt-image-2")
        self.assertEqual(payload["prompt"], "A test image")
        self.assertEqual(result["width"], 2)
        self.assertEqual(result["height"], 3)
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(result["data"], png)
        self.assertTrue(Path(result["path"]).is_absolute())
        self.assertEqual(Path(result["path"]).read_bytes(), png)

    def test_reference_edit_uses_multipart_and_preserves_single_result(self):
        reference = self.home / "reference.png"
        mask = self.home / "mask.png"
        reference.write_bytes(_png(2, 3))
        mask.write_bytes(_png(2, 3, color_type=6))
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(_png(4, 5)).decode("ascii")
        }]}).encode()
        seen = []

        def open_request(request, timeout):
            seen.append((request, timeout))
            return FakeResponse(response_body)

        with patch("relay_images._open", side_effect=open_request):
            result = generate_image(self.manager, "Edit this image", size="2048x1024",
                                    quality="auto", input_images=[str(reference)],
                                    mask=str(mask))
        self.assertEqual(len(seen), 1)
        request = seen[0][0]
        self.assertEqual(request.full_url, "https://images.example/v1/images/edits")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer image-secret")
        self.assertTrue(request.get_header("Content-type").startswith("multipart/form-data; boundary="))
        message = _multipart_message(request)
        parts = {part.get_param("name", header="content-disposition"): part
                 for part in message.iter_parts()}
        self.assertEqual(parts["model"].get_content(), "gpt-image-2")
        self.assertEqual(parts["prompt"].get_content(), "Edit this image")
        self.assertEqual(parts["size"].get_content(), "2048x1024")
        self.assertEqual(parts["quality"].get_content(), "auto")
        self.assertEqual(parts["n"].get_content(), "1")
        self.assertEqual(parts["image[]"].get_payload(decode=True), _png(2, 3))
        self.assertEqual(parts["mask"].get_payload(decode=True), _png(2, 3, color_type=6))
        self.assertEqual(result["model"], "gpt-image-2")
        self.assertEqual(Path(result["path"]).read_bytes(), _png(4, 5))

    def test_multiple_references_are_sent_as_repeated_image_parts(self):
        first = self.home / "first.png"
        second = self.home / "second.png"
        first.write_bytes(_png(1, 1))
        second.write_bytes(_png(2, 2))
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(_png()).decode("ascii")
        }]}).encode()
        with patch("relay_images._open", return_value=FakeResponse(response_body)) as open_request:
            generate_image(self.manager, "Combine references",
                           input_images=[str(first), str(second)])
        message = _multipart_message(open_request.call_args.args[0])
        image_parts = [part for part in message.iter_parts()
                       if part.get_param("name", header="content-disposition") == "image[]"]
        self.assertEqual([part.get_payload(decode=True) for part in image_parts],
                         [_png(1, 1), _png(2, 2)])

    def test_invalid_reference_path_is_rejected_before_network(self):
        with patch("relay_images._open") as open_request:
            with self.assertRaisesRegex(RelayError, "绝对本地路径|不存在"):
                generate_image(self.manager, "Edit this", input_images=["relative.png"])
        open_request.assert_not_called()

    def test_invalid_reference_content_is_rejected_before_network(self):
        reference = self.home / "reference.png"
        reference.write_bytes(b"not an image")
        with patch("relay_images._open") as open_request:
            with self.assertRaisesRegex(RelayError, "不是受支持"):
                generate_image(self.manager, "Edit this", input_images=[str(reference)])
        open_request.assert_not_called()

    def test_mask_requires_one_reference_and_png_content_before_network(self):
        first = self.home / "first.png"
        second = self.home / "second.png"
        mask = self.home / "mask.jpg"
        first.write_bytes(_png())
        second.write_bytes(_png())
        mask.write_bytes(_png(color_type=6))
        with patch("relay_images._open") as open_request:
            with self.assertRaisesRegex(RelayError, "遮罩|mask|一张"):
                generate_image(self.manager, "Edit this", input_images=[str(first), str(second)],
                               mask=str(mask))
        open_request.assert_not_called()

    def test_mask_requires_matching_png_alpha_before_network(self):
        reference = self.home / "reference.png"
        rgb_mask = self.home / "rgb-mask.png"
        wrong_size_mask = self.home / "wrong-size-mask.png"
        reference.write_bytes(_png(2, 3))
        rgb_mask.write_bytes(_png(2, 3))
        wrong_size_mask.write_bytes(_png(1, 1, color_type=6))
        with patch("relay_images._open") as open_request:
            for mask in (rgb_mask, wrong_size_mask):
                with self.subTest(mask=mask.name), self.assertRaises(RelayError):
                    generate_image(self.manager, "Edit this", input_images=[str(reference)],
                                   mask=str(mask))
        open_request.assert_not_called()

    def test_explicit_dynamic_size_and_unverified_quality_are_sent(self):
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(_png()).decode("ascii")
        }]}).encode()
        with patch("relay_images._open", return_value=FakeResponse(response_body)) as open_request:
            generate_image(self.manager, "A large image", size="4096x4096", quality="max")
        request = open_request.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["size"], "4096x4096")
        self.assertEqual(payload["quality"], "max")

    def test_invalid_dynamic_size_is_rejected_before_network(self):
        with patch("relay_images._open") as open_request:
            for size in ("0x1024", "4096x4112", "4097x4096", "12x", "9" * 5000 + "x16"):
                with self.subTest(size=size), self.assertRaises(RelayError):
                    generate_image(self.manager, "Invalid dimensions", size=size)
        open_request.assert_not_called()

    def test_non_preset_positive_dimensions_are_passed_through(self):
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(_png()).decode("ascii")
        }]}).encode()
        with patch("relay_images._open", return_value=FakeResponse(response_body)) as open_request:
            generate_image(self.manager, "Relay decides support", size="1000x1024")
        self.assertEqual(json.loads(open_request.call_args.args[0].data)["size"], "1000x1024")

    def test_saved_defaults_are_used_when_call_options_are_omitted(self):
        self.manager.apply(self.profile_id, "gpt-image-2", str(self.output_dir),
                           quality="high", size="2048x1024")
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(_png()).decode("ascii")
        }]}).encode()
        with patch("relay_images._open", return_value=FakeResponse(response_body)) as open_request:
            generate_image(self.manager, "Use saved defaults")
        payload = json.loads(open_request.call_args.args[0].data)
        self.assertEqual(payload["quality"], "high")
        self.assertEqual(payload["size"], "2048x1024")

    def test_edit_timeout_is_not_retried(self):
        reference = self.home / "reference.png"
        reference.write_bytes(_png())
        with patch("relay_images._open", side_effect=TimeoutError) as open_request:
            with self.assertRaisesRegex(RelayError, "未自动重试"):
                generate_image(self.manager, "Edit once", input_images=[str(reference)])
        self.assertEqual(open_request.call_count, 1)

    def test_model_settings_change_during_preflight_prevents_submission(self):
        original = relay_images._check_output_dir

        def change_after_check(value):
            directory = original(value)
            self.manager.apply(self.profile_id, "gpt-image-2", str(self.output_dir),
                               quality="high", size="1536x1024")
            return directory

        with (patch("relay_images._check_output_dir", side_effect=change_after_check),
              patch("relay_images._open") as open_request):
            with self.assertRaisesRegex(RelayError, "提交前发生变化"):
                generate_image(self.manager, "Do not submit")
        open_request.assert_not_called()

    def test_model_override_uses_cached_model_without_persisting_it(self):
        self.manager.save_models(self.profile_id, ["gpt-image-2", "cached-image-model"])
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(_png()).decode("ascii")
        }]}).encode()
        with patch("relay_images._open", return_value=FakeResponse(response_body)) as open_request:
            result = generate_image(self.manager, "Use cached model", model="cached-image-model")
        payload = json.loads(open_request.call_args.args[0].data)
        self.assertEqual(payload["model"], "cached-image-model")
        self.assertEqual(result["model"], "cached-image-model")
        self.assertEqual(self.manager.generation_profile()["model"], "gpt-image-2")

    def test_model_override_outside_cached_models_is_rejected_before_network(self):
        self.manager.save_models(self.profile_id, ["gpt-image-2", "cached-image-model"])
        with patch("relay_images._open") as open_request:
            with self.assertRaisesRegex(RelayError, "模型"):
                generate_image(self.manager, "Do not send", model="unlisted-image-model")
        open_request.assert_not_called()

    def test_disabled_request_is_rejected_before_network(self):
        self.manager.set_enabled(False)
        with patch("relay_images._open") as open_request:
            with self.assertRaisesRegex(RelayError, "未发送"):
                generate_image(self.manager, "A test image")
        open_request.assert_not_called()

    def test_runtime_disabled_during_preflight_does_not_submit(self):
        original = relay_images._check_output_dir

        def disable_after_check(value):
            directory = original(value)
            self.manager.set_enabled(False)
            return directory

        with patch("relay_images._check_output_dir", side_effect=disable_after_check), patch("relay_images._open") as open_request:
            with self.assertRaisesRegex(RelayError, "已关闭"):
                generate_image(self.manager, "A test image")
        open_request.assert_not_called()

    def test_profile_changed_during_preflight_does_not_submit(self):
        original = relay_images._check_output_dir

        def change_after_check(value):
            directory = original(value)
            self.manager.apply(self.profile_id, "different-image-model", str(self.output_dir))
            return directory

        with patch("relay_images._check_output_dir", side_effect=change_after_check), patch("relay_images._open") as open_request:
            with self.assertRaisesRegex(RelayError, "提交前发生变化"):
                generate_image(self.manager, "A test image")
        open_request.assert_not_called()

    def test_output_directory_failure_prevents_generation_charge(self):
        with patch("relay_images._check_output_dir", side_effect=RelayError("directory unavailable")), patch("relay_images._open") as open_request:
            with self.assertRaisesRegex(RelayError, "directory unavailable"):
                generate_image(self.manager, "A test image")
        open_request.assert_not_called()

    def test_bad_png_scanline_data_is_rejected(self):
        header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
        bad = (bytes.fromhex("89504e470d0a1a0a") + _chunk(b"IHDR", header)
               + _chunk(b"IDAT", zlib.compress(b"short")) + _chunk(b"IEND", b""))
        with self.assertRaises(RelayError):
            relay_images._image_dimensions(bad)

    def test_truncated_zlib_is_redacted(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        bad = (bytes.fromhex("89504e470d0a1a0a") + _chunk(b"IHDR", header)
               + _chunk(b"IDAT", zlib.compress(bytes(4))[:-1]) + _chunk(b"IEND", b""))
        with self.assertRaisesRegex(RelayError, "解码失败"):
            relay_images._image_dimensions(bad)

    def test_redirect_post_is_not_retried(self):
        failure = urllib.error.HTTPError("https://images.example/v1/images/generations", 302,
                                        "moved", Message(), io.BytesIO())
        with patch("relay_images._open", side_effect=failure) as open_request:
            with self.assertRaisesRegex(RelayError, "重定向"):
                generate_image(self.manager, "A test image")
        self.assertEqual(open_request.call_count, 1)

    def test_two_generations_never_overwrite_existing_output(self):
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(_png()).decode()}]}).encode()
        with patch("relay_images._open", side_effect=lambda *args, **kwargs: FakeResponse(payload)):
            first = generate_image(self.manager, "First")
            second = generate_image(self.manager, "Second")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(list(self.output_dir.iterdir())), 2)

    def test_failed_partial_write_removes_only_new_output(self):
        self.output_dir.mkdir()
        existing = self.output_dir / "keep-existing.png"
        existing.write_bytes(b"existing picture")
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(_png()).decode()}]}).encode()
        original_open = Path.open

        @contextmanager
        def fail_during_write(path, mode="r", *args, **kwargs):
            with original_open(path, mode, *args, **kwargs) as stream:
                if mode == "xb":
                    def partial_write(data):
                        stream.write(data[:8])
                        raise OSError("simulated disk full")
                    writer = Mock()
                    writer.write.side_effect = partial_write
                    yield writer
                else:
                    yield stream

        with (patch("relay_images._open", return_value=FakeResponse(payload)) as request,
              patch.object(Path, "open", fail_during_write)):
            with self.assertRaisesRegex(RelayError, "无法完整写入"):
                generate_image(self.manager, "A test image")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(list(self.output_dir.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"existing picture")

    def test_output_name_collision_never_removes_existing_picture(self):
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(_png()).decode()}]}).encode()
        with (patch("relay_images._open", side_effect=lambda *args, **kwargs: FakeResponse(payload)) as request,
              patch("relay_images.datetime") as now, patch("relay_images.uuid.uuid4") as unique):
            now.now.return_value.strftime.return_value = "fixed-time"
            unique.return_value.hex = "fixed-unique-id"
            first = generate_image(self.manager, "First image")
            with self.assertRaisesRegex(RelayError, "无法完整写入"):
                generate_image(self.manager, "Second image")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(Path(first["path"]).read_bytes(), _png())
        self.assertEqual(list(self.output_dir.iterdir()), [Path(first["path"])])

    def test_short_write_is_rejected_and_incomplete_output_removed(self):
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(_png()).decode()}]}).encode()
        original_open = Path.open

        @contextmanager
        def short_write(path, mode="r", *args, **kwargs):
            with original_open(path, mode, *args, **kwargs) as stream:
                if mode == "xb":
                    writer = Mock()
                    writer.write.side_effect = lambda data: stream.write(data[:8])
                    yield writer
                else:
                    yield stream

        with (patch("relay_images._open", return_value=FakeResponse(payload)),
              patch.object(Path, "open", short_write)):
            with self.assertRaisesRegex(RelayError, "无法完整写入"):
                generate_image(self.manager, "A test image")
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_http_error_is_redacted_and_never_retried(self):
        failure = urllib.error.HTTPError("https://images.example/v1/images/generations", 429,
                                         "private body", Message(), io.BytesIO(b"secret error detail"))
        with patch("relay_images._open", side_effect=failure) as open_request:
            with self.assertRaisesRegex(RelayError, "HTTP 429") as caught:
                generate_image(self.manager, "A test image")
        self.assertNotIn("secret error detail", str(caught.exception))
        self.assertEqual(open_request.call_count, 1)

    def test_malformed_image_data_is_not_saved(self):
        response_body = json.dumps({"data": [{"b64_json": base64.b64encode(b"<html>bad</html>").decode()}]}).encode()
        with patch("relay_images._open", return_value=FakeResponse(response_body)):
            with self.assertRaisesRegex(RelayError, "不是受支持"):
                generate_image(self.manager, "A test image")
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_url_result_download_does_not_send_api_key(self):
        image_url = "https://images.example/generated.png"
        response_body = json.dumps({"data": [{"url": image_url}]}).encode()
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            if request.full_url == image_url:
                return FakeResponse(_png(), content_type="image/png")
            return FakeResponse(response_body)

        with patch("relay_images._open", side_effect=open_request):
            result = generate_image(self.manager, "A test image")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertIsNone(requests[1].get_header("Authorization"))
        self.assertEqual(Path(result["path"]).read_bytes(), _png())

    def test_timeout_has_unknown_result_warning_and_no_retry(self):
        with patch("relay_images._open", side_effect=TimeoutError) as open_request:
            with self.assertRaisesRegex(RelayError, "无法确认"):
                generate_image(self.manager, "A test image")
        self.assertEqual(open_request.call_count, 1)


class OptionalRasterValidationTests(unittest.TestCase):
    def _encoded_image(self, image_format):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is optional")
        output = io.BytesIO()
        try:
            Image.new("RGB", (2, 3), (12, 34, 56)).save(output, format=image_format)
        except (KeyError, OSError):
            self.skipTest(f"{image_format} codec is unavailable")
        return output.getvalue()

    def test_valid_jpeg_is_fully_decoded(self):
        self.assertEqual(relay_images._image_dimensions(self._encoded_image("JPEG")),
                         ("image/jpeg", 2, 3))

    def test_valid_webp_is_fully_decoded(self):
        self.assertEqual(relay_images._image_dimensions(self._encoded_image("WEBP")),
                         ("image/webp", 2, 3))

    def test_jpeg_scan_header_without_compressed_pixels_is_rejected(self):
        image = self._encoded_image("JPEG")
        scan = image.index(bytes.fromhex("ffda"))
        corrupt = image[:scan + 2] + bytes.fromhex("ffd9")
        with self.assertRaisesRegex(RelayError, "损坏|完整解码"):
            relay_images._image_dimensions(corrupt)

    def test_webp_canvas_header_without_image_pixels_is_rejected(self):
        chunk = b"VP8X" + struct.pack("<I", 10) + bytes(10)
        corrupt = b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk
        with self.assertRaises(RelayError):
            relay_images._image_dimensions(corrupt)

    def test_optional_decoder_absent_rejects_jpeg_and_webp_but_allows_png(self):
        with patch.dict("sys.modules", {"PIL": None}):
            for image in (bytes.fromhex("ffd8ffd9"), b"RIFF" + struct.pack("<I", 4) + b"WEBP"):
                with self.subTest(signature=image[:4]):
                    with self.assertRaisesRegex(RelayError, "Pillow.*PNG"):
                        relay_images._image_dimensions(image)
            self.assertEqual(relay_images._image_dimensions(_png()), ("image/png", 1, 1))


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class ImageRelayDPAPITests(unittest.TestCase):
    def test_image_key_is_encrypted_in_dedicated_store(self):
        verification = Path(__file__).resolve().parent / ".verification"
        verification.mkdir(exist_ok=True)
        with (TemporaryDirectory(prefix="image-dpapi-", dir=verification) as temporary,
              patch.object(RelayManager, "_locked", _no_lock)):
            manager = ImageRelayManager(Path(temporary))
            manager.save_profile("image", "https://images.example/v1", "unique-image-secret", "gpt-image-2")
            encrypted = manager.profiles_file.read_bytes()
            self.assertNotIn(b"unique-image-secret", encrypted)
            self.assertEqual(manager.get_profile(manager.list_profiles()[0]["id"])["api_key"],
                             "unique-image-secret")
            self.assertFalse((Path(temporary) / "config.toml").exists())


if __name__ == "__main__":
    unittest.main()
