"""Local stdio MCP bridge for the Switcher's independently configured image API."""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import json
import os
from pathlib import Path
import sys
import tomllib

from relay_core import (RelayError, RelayManager, _atomic, _crypt, _parse, _quote,
                        _safe, _statements, _table_path)
from relay_images import MAX_INPUT_IMAGES, validate_image_options


SERVER_NAME = 'relay_switcher_image'
_PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
_QUALITIES = ('low', 'medium', 'high', 'auto', 'xhigh', 'max')
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_MESSAGE_CHARS = 128 * 1024
_TOOL = {
    'name': 'generate_image',
    'description': (
        'Generate one image from a text prompt using the separately configured image relay. '
        'Call only when the user explicitly asks to generate an image or edit an image. If intent is unclear, '
        'ask the user first. Never call automatically for ordinary chat. The service checks '
        'the Switcher image enable switch for every call, saves to its configured local folder, '
        'and returns image content plus an absolute file path. Display the returned image '
        'in the conversation using its Markdown path. A timed-out request must not be '
        'automatically retried because it may already have been billed. Reference edits accept '
        'explicit absolute local PNG, JPEG, or WebP paths; Canva is a separate tool and its '
        'assets are never automatically uploaded. quality xhigh/max are explicit relay requests '
        'whose server support is unverified.'
    ),
    'inputSchema': {
        'type': 'object',
        'properties': {
            'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 16000},
            'size': {
                'type': 'string',
                'pattern': r'^(auto|[1-9][0-9]*x[1-9][0-9]*)$',
                'description': 'auto or positive integer dimensions; each side <=4096 and total <=16777216 pixels.',
            },
            'quality': {
                'type': 'string', 'enum': list(_QUALITIES),
                'description': 'low/medium/high/auto; xhigh and max are explicit unverified relay pass-through values.',
            },
            'input_images': {
                'type': 'array', 'minItems': 1, 'maxItems': MAX_INPUT_IMAGES,
                'items': {'type': 'string'},
                'description': 'Optional absolute local PNG, JPEG, or WebP paths supplied explicitly by the user.',
            },
            'mask': {
                'type': 'string',
                'description': 'Optional absolute local alpha PNG mask; only valid with one input image.',
            },
            'model': {
                'type': 'string',
                'description': 'Optional per-call override limited to the active or cached station models.',
            },
        },
        'required': ['prompt'],
        'additionalProperties': False,
    },
    'annotations': {
        'readOnlyHint': False, 'destructiveHint': False,
        'idempotentHint': False, 'openWorldHint': True,
    },
}


def _absolute_path(value, label):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RelayError(label + '必须是绝对路径。')
    return path.resolve()


def _validate_local_argument(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RelayError(label + '必须是绝对本地路径。')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RelayError(label + '必须是绝对本地路径。')
    if os.name == 'nt' and path.drive.startswith('\\\\'):
        raise RelayError(label + '不能使用网络共享路径。')
    return value


def _console_python(python_path=None):
    path = _absolute_path(python_path or sys.executable, 'Python 路径')
    if path.name.lower() == 'pythonw.exe':
        path = path.with_name('python.exe')
    if not path.is_file():
        raise RelayError('找不到可用于 MCP 标准输入输出的 Python 程序。')
    return path


def _without_managed_server(config):
    result = dict(config)
    servers = dict(result.get('mcp_servers', {}))
    servers.pop(SERVER_NAME, None)
    if servers:
        result['mcp_servers'] = servers
    else:
        result.pop('mcp_servers', None)
    return result


def _is_own_registration(existing, entry):
    if not isinstance(existing, dict) or 'url' in existing:
        return False
    command = existing.get('command')
    args = existing.get('args')
    if (not isinstance(command, str) or not Path(command).is_absolute()
            or not isinstance(args, list) or len(args) != 3
            or not all(isinstance(value, str) for value in args)
            or args[1] != '--home'):
        return False
    python_names = {'python', 'python3', 'python.exe', 'python3.exe', 'pythonw.exe',
                    Path(entry['command']).name.casefold()}
    return (Path(command).name.casefold() in python_names
            and Path(args[0]).is_absolute() and Path(args[2]).is_absolute()
            and Path(args[0]).name.casefold() == Path(entry['args'][0]).name.casefold()
            and os.path.normcase(os.path.normpath(args[2]))
            == os.path.normcase(os.path.normpath(entry['args'][2])))


def _registered_config(original, entry):
    before = _parse(original)
    servers = before.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise RelayError('mcp_servers 配置格式无效，未修改配置。')
    text = original.decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    table = ()
    retained = []
    managed = []
    for statement in _statements(text):
        stripped = statement.lstrip(' \t\r\n')
        if stripped.startswith('['):
            try:
                table = _table_path(statement)
            except (tomllib.TOMLDecodeError, ValueError):
                raise RelayError('配置包含暂不支持的复杂表头，未修改配置。') from None
            if stripped.startswith('[[') and table[:2] == ('mcp_servers', SERVER_NAME):
                raise RelayError('图片 MCP 配置使用了数组表，未修改配置。')
        if table and table[:2] == ('mcp_servers', SERVER_NAME):
            managed.append((table, statement))
            continue
        if stripped and not stripped.startswith(('#', '[')):
            try:
                if not table:
                    parsed = tomllib.loads(statement)
                    conflict = 'mcp_servers' in parsed
                elif table == ('mcp_servers',):
                    parsed = tomllib.loads('[mcp_servers]\n' + statement)
                    conflict = SERVER_NAME in parsed.get('mcp_servers', {})
                else:
                    conflict = False
            except tomllib.TOMLDecodeError:
                raise RelayError('无法安全编辑 MCP 配置，未修改配置。') from None
            if conflict:
                raise RelayError('MCP 配置使用了内联或点式定义，请改为独立表后重试。')
        retained.append(statement)
    existing = servers.get(SERVER_NAME)
    if existing == entry:
        return original
    if existing is not None:
        if not _is_own_registration(existing, entry):
            raise RelayError('mcp_servers.relay_switcher_image 指向其他程序或配置目录，未修改配置。')
        updates = {key: entry[key] for key in ('enabled', 'command', 'args')}
        if 'tool_timeout_sec' not in existing:
            updates['tool_timeout_sec'] = entry['tool_timeout_sec']
        expected = dict(existing, **updates)
        if expected == existing:
            return original
        rewritten = []
        for table, statement in managed:
            stripped = statement.lstrip(' \t\r\n')
            if table == ('mcp_servers', SERVER_NAME):
                if stripped.startswith('['):
                    rewritten.append(statement if statement.endswith('\n') else statement + newline)
                    rewritten.extend(key + ' = ' + json.dumps(value, ensure_ascii=False) + newline
                                     for key, value in updates.items())
                    continue
                if stripped and not stripped.startswith('#'):
                    if set(tomllib.loads(statement)) & updates.keys():
                        continue
            rewritten.append(statement)
        stanza = ''.join(rewritten)
    else:
        expected = entry
        stanza = newline.join((
            '[mcp_servers.' + SERVER_NAME + ']',
            'enabled = true',
            'command = ' + _quote(entry['command']),
            'args = ' + json.dumps(entry['args'], ensure_ascii=False),
            'tool_timeout_sec = 300',
        )) + newline
    retained_text = ''.join(retained)
    if retained_text and not retained_text.endswith('\n'):
        retained_text += newline
    result = (retained_text + newline + stanza).encode('utf-8')
    after = _parse(result)
    if (_without_managed_server(before) != _without_managed_server(after)
            or after.get('mcp_servers', {}).get(SERVER_NAME) != expected):
        raise RelayError('无法安全保留现有配置，未修改配置。')
    return result


@_safe
def register_mcp(home, script_path=None, python_path=None):
    """Register only this MCP server; do not initialize or import relay credentials."""
    home = _absolute_path(home, 'Codex 配置目录')
    script = _absolute_path(script_path or __file__, 'MCP 脚本路径')
    if not script.is_file():
        raise RelayError('找不到图片 MCP 服务脚本。')
    python = _console_python(python_path)
    entry = dict(enabled=True, command=str(python),
                 args=[str(script), '--home', str(home)], tool_timeout_sec=300)
    config = home / 'config.toml'
    directory = home / 'relay-switcher'
    directory.mkdir(parents=True, exist_ok=True)
    # The existing lock does not need RelayManager.__init__, which imports auth.
    lock_owner = object.__new__(RelayManager)
    lock_owner.directory = directory
    with RelayManager._locked(lock_owner):
        existed = config.exists()
        original = config.read_bytes() if existed else b''
        result = _registered_config(original, entry)
        if result == original:
            return dict(changed=False, server=SERVER_NAME, config=str(config))
        backup_path = directory / 'image-mcp-backup.dpapi'
        backup = dict(version=1, existed=existed,
                      original=base64.b64encode(original).decode('ascii'),
                      expected=hashlib.sha256(result).hexdigest())
        encrypted = _crypt(json.dumps(backup).encode('utf-8'), True)

        def unchanged():
            return config.exists() == existed and (config.read_bytes() if config.exists() else b'') == original

        if not unchanged():
            raise RelayError('配置被其他程序修改，图片 MCP 未注册。')
        backup_created = not backup_path.exists()
        if backup_created:
            _atomic(backup_path, encrypted)
        try:
            if not unchanged():
                raise RelayError('配置被其他程序修改，图片 MCP 未注册。')
            _atomic(config, result)
        except (OSError, RelayError):
            if backup_created:
                backup_path.unlink(missing_ok=True)
            raise
    return dict(changed=True, server=SERVER_NAME, config=str(config))


class ImageMCPServer:
    """A synchronous newline-delimited JSON-RPC MCP server with lazy backend access."""

    def __init__(self, home, manager_factory=None, generator=None):
        self.home = _absolute_path(home, 'Codex 配置目录')
        self.manager_factory = manager_factory
        self.generator = generator

    @staticmethod
    def _tool_error(message):
        return {'content': [{'type': 'text', 'text': message}], 'isError': True}

    def _generate(self, arguments):
        allowed = {'prompt', 'size', 'quality', 'input_images', 'mask', 'model'}
        if not isinstance(arguments, dict) or set(arguments) - allowed:
            return self._tool_error('生图参数无效；请检查 prompt、size、quality、input_images、mask 和 model。')
        prompt = arguments.get('prompt')
        size = arguments.get('size')
        quality = arguments.get('quality')
        input_images = arguments.get('input_images')
        mask = arguments.get('mask')
        model = arguments.get('model')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000 or '\x00' in prompt:
            return self._tool_error('请提供 1 至 16000 字符的有效文字生图提示词。')
        if size is not None and not isinstance(size, str):
            return self._tool_error('生图尺寸或质量参数无效。')
        if quality is not None and (not isinstance(quality, str) or quality not in _QUALITIES):
            return self._tool_error('生图尺寸或质量参数无效。')
        if input_images is not None:
            if (not isinstance(input_images, list) or not input_images
                    or len(input_images) > MAX_INPUT_IMAGES):
                return self._tool_error('input_images 必须包含 1 至 4 个绝对本地图片路径。')
            try:
                for path in input_images:
                    _validate_local_argument(path, '参考图片路径')
            except RelayError as error:
                return self._tool_error(str(error))
        if mask is not None:
            try:
                _validate_local_argument(mask, '遮罩路径')
            except RelayError as error:
                return self._tool_error(str(error))
            if input_images is None or len(input_images) != 1:
                return self._tool_error('遮罩仅支持与一张参考图片同时使用。')
        if model is not None and (not isinstance(model, str) or not model.strip()
                                  or any(ord(char) < 32 or ord(char) == 127 for char in model)):
            return self._tool_error('model 必须是有效的模型 ID。')
        try:
            size, quality = validate_image_options(size, quality)
        except RelayError as error:
            return self._tool_error(str(error))
        try:
            # Keep unexpected backend diagnostics out of protocol and credential logs.
            with open(os.devnull, 'w', encoding='utf-8') as sink, redirect_stdout(sink), redirect_stderr(sink):
                if self.manager_factory is None:
                    from relay_images import ImageRelayManager
                    manager = ImageRelayManager(self.home)
                else:
                    manager = self.manager_factory(self.home)
                if self.generator is None:
                    from relay_images import generate_image
                    generator = generate_image
                else:
                    generator = self.generator
                options = {'size': size, 'quality': quality}
                if input_images is not None:
                    options['input_images'] = input_images
                if mask is not None:
                    options['mask'] = mask
                if model is not None:
                    options['model'] = model
                result = generator(manager, prompt.strip(), **options)
            path = _absolute_path(result['path'], '图片保存路径')
            mime = result['mime_type']
            data = result['data']
            width, height = result['width'], result['height']
            model = result['model']
            if (mime not in ('image/png', 'image/jpeg', 'image/webp')
                    or not isinstance(data, bytes) or not data
                    or len(data) > _MAX_IMAGE_BYTES
                    or any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in (width, height))
                    or not isinstance(model, str)):
                raise ValueError
            # Encoded Markdown destination stays usable even for spaces and parentheses.
            markdown_path = path.as_posix().replace('>', '%3E').replace('\n', '%0A').replace('\r', '%0D')
            text = (f'图片已保存：{path}\n实际尺寸：{width}×{height}；模型：{model}。\n'
                    f'请在当前对话中展示下方图片，并保留本地文件链接。\n\n'
                    f'![生成的图片](<{markdown_path}>)')
            metadata = dict(path=str(path), mime_type=mime, width=width, height=height, model=model)
            return {'content': [{'type': 'text', 'text': text},
                                {'type': 'image', 'data': base64.b64encode(data).decode('ascii'),
                                 'mimeType': mime}],
                    'structuredContent': metadata, 'isError': False}
        except RelayError as error:
            # RelayError is the application's redacted, user-displayable error type.
            return self._tool_error(str(error)[:600])
        except Exception:
            return self._tool_error('生图未完成，或图片结果无法验证。为避免重复计费，未自动重试。请检查 Switcher 的生图配置。')

    def handle(self, request):
        if not isinstance(request, dict):
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
        has_id = 'id' in request
        request_id = request.get('id')
        if (request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str)
                or (has_id and (isinstance(request_id, bool) or not isinstance(request_id, (str, int, type(None)))))):
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
        method = request['method']
        if not has_id:
            # Notifications must not trigger a billed tool call or produce a response.
            return None
        params = request.get('params', {})
        if not isinstance(params, dict):
            return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32602, 'message': 'Invalid params'}}
        if method == 'initialize':
            requested = params.get('protocolVersion')
            version = requested if requested in _PROTOCOL_VERSIONS else _PROTOCOL_VERSIONS[0]
            result = {'protocolVersion': version, 'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': SERVER_NAME, 'version': '1.1.0'},
                      'instructions': 'Use generate_image only for explicit image-generation or image-edit requests. Display returned images and local paths.'}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': [_TOOL]}
        elif method == 'tools/call':
            result = (self._generate(params.get('arguments', {})) if params.get('name') == 'generate_image'
                      else self._tool_error('未知的图片工具。'))
        else:
            return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'Method not found'}}
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}

    def serve(self, input_stream=None, output_stream=None):
        input_stream = sys.stdin if input_stream is None else input_stream
        output_stream = sys.stdout if output_stream is None else output_stream
        while True:
            try:
                line = input_stream.readline(_MAX_MESSAGE_CHARS + 1)
            except (UnicodeError, OSError):
                return
            if not line:
                return
            if len(line) > _MAX_MESSAGE_CHARS:
                while line and not line.endswith('\n'):
                    line = input_stream.readline(_MAX_MESSAGE_CHARS + 1)
                response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Message too large'}}
            elif not line.strip():
                continue
            else:
                try:
                    request = json.loads(line)
                except (ValueError, RecursionError):
                    response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
                else:
                    try:
                        response = self.handle(request)
                    except Exception:
                        response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32603, 'message': 'Internal error'}}
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False, separators=(',', ':')) + '\n')
                output_stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Codex Relay Switcher image MCP stdio service')
    parser.add_argument('--home', required=True, help='Absolute Codex configuration directory')
    args = parser.parse_args(argv)
    try:
        home = _absolute_path(args.home, 'Codex 配置目录')
    except RelayError:
        parser.error('--home must be an absolute directory path')
    # UTF-8 JSON on Windows must not inherit the console code page.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict', newline='\n')
    try:
        ImageMCPServer(home).serve()
    except (OSError, UnicodeError):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
