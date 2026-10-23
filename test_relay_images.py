"""Tests for the independent image relay and image response handling."""
import base64
from contextlib import contextmanager
import io
import json
import os
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


def _png(width=1, height=1):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\x00" + b"\x12\x34\x56" * width for _ in range(height))
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
