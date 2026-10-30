"""Independent, DPAPI-protected image relay settings and image generation."""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import urllib.error
import urllib.request
import uuid
import zlib
from urllib.parse import urlsplit, urlunsplit

from relay_core import (
    RelayError,
    RelayManager,
    _atomic,
    _crypt,
    _reasoning_effort,
    _safe,
    _validate,
    _validate_models,
)


DEFAULT_OUTPUT_DIR = r"D:\codex-\image"
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_DIMENSION = 32768
MAX_INPUT_IMAGES = 4
_MAX_EDIT_INPUT_BYTES = 50 * 1024 * 1024
_MAX_EDIT_DIMENSION = 4096
_MAX_EDIT_PIXELS = 16_777_216
_MODEL_ID = re.compile(r"^[^\s\x00-\x1f\x7f]+$")
_QUALITY = frozenset(("low", "medium", "high", "auto", "xhigh", "max"))
_SIZE_PATTERN = re.compile(r"^([1-9][0-9]*)x([1-9][0-9]*)$")
_DEFAULT_QUALITY = "low"
_DEFAULT_SIZE = "1024x1024"


def _validate_quality(value):
    if not isinstance(value, str) or value not in _QUALITY:
        raise RelayError("图片画质无效；可选 low、medium、high、auto、xhigh 或 max。"
                         " xhigh/max 仅作为显式中转请求，服务端是否支持未验证。")
    return value


def _validate_size(value):
    if value == "auto":
        return value
    if not isinstance(value, str):
        raise RelayError("图片尺寸无效；请使用 auto 或正整数 WxH。")
    match = _SIZE_PATTERN.fullmatch(value)
    if match is None:
        raise RelayError("图片尺寸无效；请使用 auto 或正整数 WxH。")
    if any(len(part) > 4 for part in match.groups()):
        raise RelayError("图片尺寸超出限制；单边不超过 4096，像素总数不超过 16777216。")
    width, height = (int(part) for part in match.groups())
    if (width > _MAX_EDIT_DIMENSION or height > _MAX_EDIT_DIMENSION
            or width * height > _MAX_EDIT_PIXELS):
        raise RelayError("图片尺寸超出限制；单边不超过 4096，"
                         "像素总数不超过 16777216。")
    return value


def validate_image_options(size, quality):
    """Validate generic relay image options without claiming model capabilities."""
    return (None if size is None else _validate_size(size),
            None if quality is None else _validate_quality(quality))


def _normalize_base_url(value):
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RelayError("图片 API 地址必须是文本，且不能包含控制字符。")
    value = value.strip()
    try:
        parts = urlsplit(value)
        hostname = (parts.hostname or "").lower().rstrip(".")
        valid = (parts.scheme in ("https", "http") and hostname
                 and parts.username is None and parts.password is None
                 and "?" not in value and "#" not in value
                 and not any(char.isspace() for char in value))
        parts.port
    except ValueError:
        valid = False
        hostname = ""
    if not valid:
        raise RelayError("请输入完整的图片 API 地址，不能包含用户名、密码、查询参数或片段。")
    if parts.scheme == "http" and hostname not in ("localhost", "127.0.0.1", "::1"):
        raise RelayError("图片 API 必须使用 HTTPS；仅本机回环地址允许使用 HTTP。")
    path = parts.path.rstrip("/")
    for suffix in ("/images/generations", "/models"):
        if path.endswith(suffix):
            path = path[:-len(suffix)].rstrip("/")
            break
    if not path.endswith("/v1"):
        raise RelayError("图片 API 地址必须是 OpenAI 兼容的 /v1 API 根地址。")
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


def _output_path(value):
    if not isinstance(value, str) or not value.strip():
        raise RelayError("请填写图片保存目录。")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RelayError("图片保存目录不能包含控制字符。")
    try:
        return str(Path(value).expanduser().resolve())
    except (OSError, RuntimeError):
        raise RelayError("图片保存目录无效。") from None


def _validate_profile(name, base_url, api_key, model, require_key=True):
    profile = _validate(name, base_url, api_key, model, require_key=require_key)
    profile["base_url"] = _normalize_base_url(profile["base_url"])
    if profile["model"] and not _MODEL_ID.fullmatch(profile["model"]):
        raise RelayError("图片模型 ID 不能包含空白或控制字符。")
    return profile


def _validate_model_settings(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 500:
        raise RelayError("图片模型默认设置无效，未修改配置。")
    result = {}
    for model, options in value.items():
        if (not isinstance(model, str) or len(model) > 2000 or not _MODEL_ID.fullmatch(model)
                or not isinstance(options, dict) or set(options) - {"quality", "size"}):
            raise RelayError("图片模型默认设置无效，未修改配置。")
        try:
            quality = _validate_quality(options.get("quality", _DEFAULT_QUALITY))
            size = _validate_size(options.get("size", _DEFAULT_SIZE))
        except RelayError:
            raise RelayError("图片模型默认设置无效，未修改配置。") from None
        result[model] = {"quality": quality, "size": size}
    return result


def _image_dimensions(data):
    """Validate a supported raster signature and return format plus dimensions."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_dimensions(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(data)
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_dimensions(data)
    raise RelayError("生图接口返回的内容不是受支持的 PNG、JPEG 或 WebP 图片。")


def _png_has_alpha(data):
    offset = 8
    color_type = None
    has_transparency = False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        chunk_data = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            color_type = chunk_data[9]
        elif kind == b"tRNS":
            has_transparency = True
        elif kind == b"IEND":
            break
        offset += 12 + length
    return color_type in (4, 6) or has_transparency


def _read_edit_image(value, label):
    if (not isinstance(value, str) or not value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise RelayError(label + "必须是有效的绝对本地路径。")
    path = Path(value)
    if not path.is_absolute():
        raise RelayError(label + "必须是绝对本地路径。")
    try:
        path = path.resolve(strict=True)
        if os.name == "nt" and path.drive.startswith("\\\\"):
            raise RelayError(label + "不能使用网络共享路径。")
        if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > _MAX_IMAGE_BYTES:
            raise RelayError(label + "为空、不是普通文件或超过 25 MiB。")
        with path.open("rb") as stream:
            data = stream.read(_MAX_IMAGE_BYTES + 1)
    except RelayError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise RelayError(label + "无法读取。") from None
    if not data or len(data) > _MAX_IMAGE_BYTES:
        raise RelayError(label + "为空或超过 25 MiB。")
    try:
        mime_type, width, height = _image_dimensions(data)
    except RelayError as error:
        raise RelayError(label + "无效：" + str(error)) from None
    if (width > _MAX_EDIT_DIMENSION or height > _MAX_EDIT_DIMENSION
            or width * height > _MAX_EDIT_PIXELS):
        raise RelayError(label + "尺寸超出限制：单边最多 4096，像素总数最多 16777216。")
    return {"data": data, "mime_type": mime_type, "width": width, "height": height}


def _prepare_edit_inputs(input_images, mask):
    if input_images is None:
        if mask is not None:
            raise RelayError("使用遮罩前必须提供一张参考图片。")
        return None, None
    if (not isinstance(input_images, (list, tuple)) or not input_images
            or len(input_images) > MAX_INPUT_IMAGES):
        raise RelayError("参考图片必须是 1 至 4 个绝对本地 PNG、JPEG 或 WebP 路径。")
    if mask is not None and len(input_images) != 1:
        raise RelayError("遮罩仅支持与一张参考图片同时使用。")
    images = [_read_edit_image(path, "参考图片") for path in input_images]
    total_bytes = sum(len(image["data"]) for image in images)
    if total_bytes > _MAX_EDIT_INPUT_BYTES:
        raise RelayError("参考图片总大小超过 50 MiB。")
    checked_mask = None
    if mask is not None:
        checked_mask = _read_edit_image(mask, "遮罩图片")
        total_bytes += len(checked_mask["data"])
        if total_bytes > _MAX_EDIT_INPUT_BYTES:
            raise RelayError("参考图片和遮罩总大小超过 50 MiB。")
        if checked_mask["mime_type"] != "image/png" or not _png_has_alpha(checked_mask["data"]):
            raise RelayError("遮罩必须是带 alpha 通道的 PNG 图片。")
        if (checked_mask["width"], checked_mask["height"]) != (
                images[0]["width"], images[0]["height"]):
            raise RelayError("遮罩尺寸必须与参考图片完全一致。")
    return images, checked_mask


def _multipart_body(fields, images, mask):
    boundary = "codex-relay-" + uuid.uuid4().hex
    marker = boundary.encode("ascii")
    chunks = []
    for name, value in fields:
        chunks.extend((b"--" + marker + b"\r\n",
                       f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                       str(value).encode("utf-8"), b"\r\n"))
    extensions = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
    for index, image in enumerate(images, 1):
        filename = f"reference-{index}.{extensions[image['mime_type']]}"
        chunks.extend((b"--" + marker + b"\r\n",
                       f'Content-Disposition: form-data; name="image[]"; filename="{filename}"\r\n'
                       f'Content-Type: {image["mime_type"]}\r\n\r\n'.encode("ascii"),
                       image["data"], b"\r\n"))
    if mask is not None:
        chunks.extend((b"--" + marker + b"\r\n",
                       b'Content-Disposition: form-data; name="mask"; filename="mask.png"\r\n',
                       b"Content-Type: image/png\r\n\r\n", mask["data"], b"\r\n"))
    chunks.append(b"--" + marker + b"--\r\n")
    return boundary, b"".join(chunks)


def _checked_dimensions(width, height):
    if not (1 <= width <= _MAX_DIMENSION and 1 <= height <= _MAX_DIMENSION):
        raise RelayError("图片尺寸无效或超出允许范围。")
    return width, height


def _png_dimensions(data):
    offset = 8
    width = height = None
    idat = bytearray()
    ended = False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            break
        kind = data[offset + 4:offset + 8]
        chunk_data = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + chunk_data) & 0xffffffff != expected_crc:
            raise RelayError("PNG 图片校验失败。")
        if offset == 8 and (kind != b"IHDR" or length != 13):
            raise RelayError("PNG 图片头无效。")
        if kind == b"IHDR":
            if width is not None or length != 13:
                raise RelayError("PNG 图片头重复或无效。")
            width, height = struct.unpack_from(">II", chunk_data)
            _checked_dimensions(width, height)
            bit_depth, color_type, compression, filtering, interlace = chunk_data[8:13]
            valid_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                            4: (8, 16), 6: (8, 16)}
            if (color_type not in valid_depths or bit_depth not in valid_depths[color_type]
                    or compression != 0 or filtering != 0 or interlace not in (0, 1)):
                raise RelayError("PNG 图片编码参数无效。")
        elif kind == b"IDAT":
            idat.extend(chunk_data)
            if len(idat) > _MAX_IMAGE_BYTES:
                raise RelayError("PNG 图片数据超过大小限制。")
        elif kind == b"IEND":
            if length != 0:
                raise RelayError("PNG 结束标记无效。")
            ended = True
            offset = end
            break
        offset = end
    if width is None or not idat or not ended or offset != len(data):
        raise RelayError("PNG 图片数据不完整。")
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(bytes(idat), _MAX_IMAGE_BYTES + 1)
        if len(raw) > _MAX_IMAGE_BYTES or not inflater.eof or inflater.unused_data:
            raise ValueError
    except (zlib.error, ValueError):
        raise RelayError("PNG 图片数据解码失败。") from None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    passes = ((0, 0, 1, 1),) if interlace == 0 else (
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    position = 0
    for x_start, y_start, x_step, y_step in passes:
        columns = max(0, (width - x_start + x_step - 1) // x_step)
        rows = max(0, (height - y_start + y_step - 1) // y_step)
        if not columns or not rows:
            continue
        row_size = (columns * channels * bit_depth + 7) // 8 + 1
        end = position + row_size * rows
        if end > len(raw) or any(raw[position + row * row_size] > 4 for row in range(rows)):
            raise RelayError("PNG 图片像素数据不完整或过滤参数无效。")
        position = end
    if position != len(raw):
        raise RelayError("PNG 图片像素数据长度无效。")
    return "image/png", width, height


def _decoded_dimensions(data, expected_format, mime_type):
    """Fully decode optional raster formats instead of trusting their headers."""
    try:
        from PIL import Image
    except ImportError:
        raise RelayError("校验 JPEG/WebP 图片需要可选依赖 Pillow；请让中转站返回 PNG，或安装 Pillow。"
                         "本次请求未自动重试。") from None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != expected_format:
                raise ValueError
            width, height = _checked_dimensions(*image.size)
            if width * height * len(image.getbands()) > _MAX_IMAGE_BYTES:
                raise RelayError("图片解码后的数据超过大小限制。")
            if getattr(image, "n_frames", 1) != 1:
                raise RelayError("生图接口必须返回单张静态图片。")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.format != expected_format or image.size != (width, height):
                raise ValueError
    except RelayError:
        raise
    except Exception:
        raise RelayError(f"{expected_format} 图片数据损坏或无法完整解码；本次请求未自动重试。") from None
    return mime_type, width, height


def _jpeg_dimensions(data):
    if not data.endswith(b"\xff\xd9"):
        raise RelayError("JPEG 图片数据不完整。")
    return _decoded_dimensions(data, "JPEG", "image/jpeg")


def _webp_dimensions(data):
    if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
        raise RelayError("WebP 图片数据不完整。")
    return _decoded_dimensions(data, "WEBP", "image/webp")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _open(request, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _read_limited(response, limit):
    length = response.headers.get("Content-Length")
    if length:
        try:
            if int(length) > limit:
                raise RelayError("图片中转站响应超过大小限制。")
        except ValueError:
            pass
    body = response.read(limit + 1)
    if len(body) > limit:
        raise RelayError("图片中转站响应超过大小限制。")
    return body


def _image_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise RelayError("图片中转站返回的图片地址无效。")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        valid = (parts.scheme == "https" and hostname and not parts.username and not parts.password
                 and not parts.fragment and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
                 and hostname.lower().rstrip(".") not in ("localhost", "localhost.localdomain"))
        parts.port
        if hostname:
            try:
                ip = ipaddress.ip_address(hostname)
            except ValueError:
                ip = None
            if ip is not None and not ip.is_global:
                valid = False
    except ValueError:
        valid = False
    if not valid:
        raise RelayError("图片中转站返回了不安全或无效的图片地址。")
    return value


def _response_image(payload):
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise RelayError("图片中转站没有返回唯一的图片结果。")
    result = entries[0]
    if isinstance(result.get("b64_json"), str):
        try:
            import base64
            image = base64.b64decode(result["b64_json"], validate=True)
        except (ValueError, TypeError):
            raise RelayError("图片中转站返回的 Base64 图片数据无效。") from None
    elif isinstance(result.get("url"), str):
        image = _download_image(_image_url(result["url"]))
    else:
        raise RelayError("图片中转站未返回 Base64 图片数据或 HTTPS 图片地址。")
    if not image or len(image) > _MAX_IMAGE_BYTES:
        raise RelayError("图片中转站返回的图片为空或超过大小限制。")
    mime_type, width, height = _image_dimensions(image)
    return image, mime_type, width, height


def _download_image(url):
    request = urllib.request.Request(url, headers={"Accept": "image/png, image/jpeg, image/webp"}, method="GET")
    try:
        with _open(request, timeout=60) as response:
            if not 200 <= response.getcode() < 300:
                raise RelayError(f"图片下载失败（HTTP {response.getcode()}）。")
            content_type = response.headers.get_content_type()
            if content_type not in ("image/png", "image/jpeg", "image/webp", "application/octet-stream"):
                raise RelayError("图片地址没有返回图片内容。")
            return _read_limited(response, _MAX_IMAGE_BYTES)
    except urllib.error.HTTPError as error:
        error.close()
        if 300 <= error.code < 400:
            raise RelayError("图片下载地址返回重定向，已停止下载。") from None
        raise RelayError(f"图片下载失败（HTTP {error.code}）。") from None
    except (TimeoutError, http.client.HTTPException, OSError, urllib.error.URLError):
        raise RelayError("图片下载失败或超时；没有重试。") from None


class ImageRelayManager:
    """Persist image relays separately from Codex language-model profiles."""

    @_safe
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.directory = self.home / "relay-switcher"
        self.profiles_file = self.directory / "image-profiles.dpapi"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock_manager = RelayManager.__new__(RelayManager)
        self._lock_manager.directory = self.directory
        with self._locked():
            if not self.profiles_file.exists():
                self._save([], {"active_id": None, "enabled": False, "output_dir": DEFAULT_OUTPUT_DIR})

    def _locked(self):
        return self._lock_manager._locked()

    def _load(self):
        try:
            result = json.loads(_crypt(self.profiles_file.read_bytes(), False).decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError
            profiles = result.get("profiles")
            settings = result.get("settings")
            if result.get("version") != 1 or not isinstance(profiles, list) or not isinstance(settings, dict):
                raise ValueError
            active_id = settings.get("active_id")
            enabled = settings.get("enabled")
            output_dir = _output_path(settings.get("output_dir", DEFAULT_OUTPUT_DIR))
            if active_id is not None and not isinstance(active_id, str):
                raise ValueError
            if type(enabled) is not bool:
                raise ValueError
            cleaned = []
            seen = set()
            for stored in profiles:
                if not isinstance(stored, dict):
                    raise ValueError
                profile = _validate_profile(stored.get("name"), stored.get("base_url"),
                                            stored.get("api_key"), stored.get("model", ""),
                                            require_key=False)
                profile_id = stored.get("id")
                if not isinstance(profile_id, str) or not profile_id or profile_id in seen:
                    raise ValueError
                seen.add(profile_id)
                profile["id"] = profile_id
                profile["models"] = _validate_models(stored.get("models", []))
                profile["reasoning_effort"] = _reasoning_effort(stored.get("reasoning_effort", "high"))
                profile["model_settings"] = _validate_model_settings(stored.get("model_settings"))
                cleaned.append(profile)
            if active_id is not None and active_id not in seen:
                active_id = None
                enabled = False
            return cleaned, {"active_id": active_id, "enabled": enabled, "output_dir": output_dir}
        except (ValueError, UnicodeError, RecursionError):
            raise RelayError("图片中转配置损坏，未修改配置。") from None

    def _save(self, profiles, settings):
        payload = json.dumps({"version": 1, "profiles": profiles, "settings": settings},
                             ensure_ascii=False).encode("utf-8")
        _atomic(self.profiles_file, _crypt(payload, True))

    @_safe
    def settings(self):
        with self._locked():
            _, settings = self._load()
            return dict(settings)

    @_safe
    def list_profiles(self):
        with self._locked():
            profiles, _ = self._load()
            return [{key: value for key, value in profile.items() if key != "api_key"}
                    for profile in profiles]

    @_safe
    def get_profile(self, profile_id):
        with self._locked():
            profiles, _ = self._load()
            for profile in profiles:
                if profile["id"] == profile_id:
                    return dict(profile)
        raise RelayError("图片中转站不存在，请刷新列表。")

    @_safe
    def save_profile(self, name, base_url, api_key, model="", profile_id=None, models=None,
                     reasoning_effort=None):
        profile = _validate_profile(name, base_url, api_key, model)
        new_models = _validate_models(models) if models is not None else None
        with self._locked():
            profiles, settings = self._load()
            previous = next((item for item in profiles if item["id"] == profile_id), None)
            if profile_id is not None and previous is None:
                raise RelayError("图片中转站已被另一窗口删除，请刷新列表。")
            same_connection = (previous is not None and previous["base_url"] == profile["base_url"]
                               and previous["api_key"] == profile["api_key"])
            profile["id"] = profile_id or uuid.uuid4().hex
            profile["models"] = (new_models if new_models is not None else
                                  previous["models"] if same_connection else [])
            profile["reasoning_effort"] = _reasoning_effort(
                reasoning_effort if reasoning_effort is not None else
                previous.get("reasoning_effort", "high") if previous is not None else "high")
            profile["model_settings"] = (previous["model_settings"] if same_connection else {})
            profiles = [item for item in profiles if item["id"] != profile["id"]]
            profiles.append(profile)
            if (settings["enabled"] and settings["active_id"] == profile["id"] and previous is not None
                    and any(previous[field] != profile[field] for field in ("base_url", "api_key", "model"))):
                settings["enabled"] = False
            self._save(profiles, settings)
        return profile["id"]

    @_safe
    def save_models(self, profile_id, models, base_url=None, api_key=None, resolved_base_url=None):
        models = _validate_models(models)
        with self._locked():
            profiles, settings = self._load()
            profile = next((item for item in profiles if item["id"] == profile_id), None)
            if profile is None:
                raise RelayError("图片中转站已被删除，获取到的模型列表未保存。")
            if base_url is not None and _normalize_base_url(base_url) != profile["base_url"]:
                raise RelayError("图片中转站地址已被修改，模型列表未保存。请重新获取。")
            if api_key is not None and (not isinstance(api_key, str) or api_key.strip() != profile["api_key"]):
                raise RelayError("图片中转凭证已被修改，模型列表未保存。请重新获取。")
            if resolved_base_url is not None:
                resolved = _normalize_base_url(resolved_base_url)
                if (settings["enabled"] and settings["active_id"] == profile_id
                        and resolved != profile["base_url"]):
                    settings["enabled"] = False
                profile["base_url"] = resolved
            profile["models"] = models
            self._save(profiles, settings)

    @_safe
    def delete_profile(self, profile_id):
        with self._locked():
            profiles, settings = self._load()
            remaining = [item for item in profiles if item["id"] != profile_id]
            if len(remaining) == len(profiles):
                raise RelayError("图片中转站不存在，请刷新列表。")
            if settings["active_id"] == profile_id:
                settings["active_id"] = None
                settings["enabled"] = False
            self._save(remaining, settings)

    @_safe
    def apply(self, profile_id, model, output_dir, quality=None, size=None, model_settings=None):
        if not isinstance(model, str):
            raise RelayError("图片模型 ID 必须是文本。")
        model = model.strip()
        if not model or not _MODEL_ID.fullmatch(model):
            raise RelayError("请先选择或填写有效的图片模型 ID。")
        if quality is not None:
            quality = _validate_quality(quality)
        if size is not None:
            size = _validate_size(size)
        checked_model_settings = (_validate_model_settings(model_settings)
                                  if model_settings is not None else None)
        output_dir = _output_path(output_dir)
        with self._locked():
            profiles, settings = self._load()
            profile = next((item for item in profiles if item["id"] == profile_id), None)
            if profile is None:
                raise RelayError("图片中转站不存在，请刷新列表。")
            _validate_profile(profile["name"], profile["base_url"], profile["api_key"], model)
            profile["model"] = model
            if model not in profile["models"]:
                profile["models"].insert(0, model)
            if checked_model_settings is not None:
                profile["model_settings"].update(checked_model_settings)
            options = dict(profile["model_settings"].get(
                model, {"quality": _DEFAULT_QUALITY, "size": _DEFAULT_SIZE}))
            if quality is not None or size is not None or checked_model_settings is not None:
                if quality is not None:
                    options["quality"] = quality
                if size is not None:
                    options["size"] = size
                profile["model_settings"][model] = options
            settings["active_id"] = profile_id
            settings["output_dir"] = output_dir
            self._save(profiles, settings)
            return dict(settings)

    @_safe
    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise RelayError("生图启用状态必须是开或关。")
        with self._locked():
            profiles, settings = self._load()
            if enabled:
                active = next((item for item in profiles if item["id"] == settings["active_id"]), None)
                if active is None or not active["api_key"] or not active["model"]:
                    raise RelayError("启用生图前，请先选择已配置密钥和模型的生图中转站。")
            settings["enabled"] = enabled
            self._save(profiles, settings)
            return dict(settings)

    @_safe
    def generation_profile(self):
        with self._locked():
            profiles, settings = self._load()
            if not settings["enabled"]:
                raise RelayError("生图功能当前已关闭；本次请求未发送到图片中转站。")
            profile = next((item for item in profiles if item["id"] == settings["active_id"]), None)
            if profile is None or not profile["api_key"] or not profile["model"]:
                raise RelayError("生图配置不完整；请在切换器中选择图片中转站、密钥和模型。")
            result = dict(profile)
            result["output_dir"] = settings["output_dir"]
            options = profile["model_settings"].get(profile["model"], {})
            result["default_quality"] = options.get("quality", _DEFAULT_QUALITY)
            result["default_size"] = options.get("size", _DEFAULT_SIZE)
            return result


def _check_output_dir(value):
    path = Path(_output_path(value))
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".codex-image-write-check-", dir=path)
        os.close(fd)
        os.unlink(probe)
    except OSError:
        raise RelayError("图片保存目录无法创建或写入，请检查目录权限。") from None
    return path


def _decode_response(body):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise RelayError("图片中转站返回的 JSON 无效。") from None
    if not isinstance(payload, dict):
        raise RelayError("图片中转站返回的数据格式无效。")
    return payload


def _http_failure(error):
    if 300 <= error.code < 400:
        return RelayError("生图 API 返回重定向，已停止请求；请填写最终 API 地址。")
    messages = {
        400: "生图请求被拒绝（HTTP 400），请检查模型 ID、尺寸和画质参数。",
        401: "生图认证失败（HTTP 401），请检查独立的图片 API Key。",
        403: "生图访问被拒绝（HTTP 403），请检查密钥权限或模型访问权限。",
        404: "生图 API 或模型不存在（HTTP 404），请检查 /v1/images/generations 或 /v1/images/edits 地址和模型 ID。",
        429: "生图额度不足或请求过于频繁（HTTP 429）；本程序未自动重试。",
    }
    return RelayError(messages.get(error.code,
                                   f"生图 API 请求失败（HTTP {error.code}）；本程序未自动重试。"))


def generate_image(manager, prompt, size=None, quality=None, model=None,
                   input_images=None, mask=None):
    """Submit one generation or reference-edit request and save its validated result."""
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
        raise RelayError("请提供 1 到 20000 个字符的生图提示词。")
    if any(ord(char) == 0 for char in prompt):
        raise RelayError("生图提示词不能包含 NUL 字符。")
    profile = manager.generation_profile()
    if model is None:
        request_model = profile["model"]
    elif (not isinstance(model, str) or len(model) > 2000 or not _MODEL_ID.fullmatch(model)
          or model != profile["model"] and model not in profile["models"]):
        raise RelayError("图片模型覆盖值必须是当前模型或此中转站已缓存的模型。")
    else:
        request_model = model
    model_options = profile["model_settings"].get(request_model, {})
    if size is None:
        size = (profile.get("default_size", _DEFAULT_SIZE)
                if request_model == profile["model"]
                else model_options.get("size", _DEFAULT_SIZE))
    if quality is None:
        quality = (profile.get("default_quality", _DEFAULT_QUALITY)
                   if request_model == profile["model"]
                   else model_options.get("quality", _DEFAULT_QUALITY))
    size = _validate_size(size)
    quality = _validate_quality(quality)
    images, checked_mask = _prepare_edit_inputs(input_images, mask)
    output_dir = _check_output_dir(profile["output_dir"])
    base_url = _normalize_base_url(profile["base_url"])
    if images is None:
        endpoint = base_url + "/images/generations"
        request_body = json.dumps({"model": request_model, "prompt": prompt.strip(),
                                   "size": size, "quality": quality, "n": 1},
                                  ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
    else:
        endpoint = base_url + "/images/edits"
        fields = (("model", request_model), ("prompt", prompt.strip()), ("size", size),
                  ("quality", quality), ("n", "1"))
        boundary, request_body = _multipart_body(fields, images, checked_mask)
        content_type = "multipart/form-data; boundary=" + boundary
    request = urllib.request.Request(
        endpoint, data=request_body,
        headers={"Authorization": "Bearer " + profile["api_key"],
                 "Content-Type": content_type, "Accept": "application/json"}, method="POST")
    current = manager.generation_profile()
    if any(current[field] != profile[field]
           for field in ("id", "base_url", "api_key", "model", "output_dir", "model_settings")):
        raise RelayError("生图配置在提交前发生变化；本次请求未发送，请重新发起。")
    try:
        with _open(request, timeout=180) as response:
            if not 200 <= response.getcode() < 300:
                raise RelayError(f"生图 API 请求失败（HTTP {response.getcode()}）；本程序未自动重试。")
            body = _read_limited(response, _MAX_JSON_BYTES)
    except urllib.error.HTTPError as error:
        failure = _http_failure(error)
        error.close()
        raise failure from None
    except (TimeoutError, http.client.HTTPException, OSError, urllib.error.URLError):
        raise RelayError("生图请求失败或超时，服务端是否已生成图片无法确认；为避免重复计费，本程序未自动重试。") from None
    payload = _decode_response(body)
    image, mime_type, width, height = _response_image(payload)
    extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"codex-image-{timestamp}-{uuid.uuid4().hex[:12]}{extensions[mime_type]}"
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            if stream.write(image) != len(image):
                raise OSError("incomplete image write")
    except OSError:
        if created:
            try:
                path.unlink()
            except OSError:
                raise RelayError("图片生成成功，但写入失败且未能清理本次不完整文件；"
                                 "请检查保存目录、权限和磁盘空间。本程序未自动重试。") from None
        raise RelayError("图片生成成功，但无法完整写入保存目录；请检查目录权限和磁盘空间。"
                         "本程序未自动重试。") from None
    return {"path": str(path.resolve()), "mime_type": mime_type, "width": width,
            "height": height, "data": image, "model": request_model}
