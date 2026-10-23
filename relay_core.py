"""Local Codex provider switching; no network calls or global environment writes."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import tomllib
from urllib.parse import urlsplit
import uuid


class RelayError(Exception):
    """A redacted error suitable for display to the user."""


REASONING_EFFORTS = ('light', 'medium', 'high', 'xhigh', 'max', 'ultra')
_CODEX_REASONING_EFFORTS = {'light': 'low', **{value: value for value in REASONING_EFFORTS[1:]}}
_BUILTIN_PROVIDER_IDS = frozenset(('openai', 'ollama', 'lmstudio', 'amazon-bedrock'))


def _reasoning_effort(value):
    if value not in REASONING_EFFORTS:
        raise RelayError('默认思考强度无效，请选择 light、medium、high、xhigh、max 或 ultra。')
    return value


def _stored_reasoning_effort(value):
    if value == 'low':
        return 'light'
    return value if value in REASONING_EFFORTS else 'high'


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except RelayError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise RelayError('本地配置操作失败，请检查目录权限、配置格式和磁盘空间。') from None
    return wrapped


def _environment(name):
    value = os.environ.get(name)
    if value:
        return value
    if os.name == 'nt':
        import winreg
        for root, key in ((winreg.HKEY_CURRENT_USER, 'Environment'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')):
            try:
                with winreg.OpenKey(root, key) as handle:
                    value, kind = winreg.QueryValueEx(handle, name)
                    if isinstance(value, str) and value:
                        return os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value
            except OSError:
                pass
    return ''


@_safe
def resolve_home():
    value = _environment('CODEX_HOME')
    return Path(value).expanduser().resolve() if value else (Path.home() / '.codex').resolve()


class _Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, encrypt):
    if os.name != 'nt':
        raise RelayError('凭证保护需要 Windows 当前用户账户。')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    if encrypt:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
        result = function(ctypes.byref(source), 'Codex relay switcher', None, None, None, 1, ctypes.byref(target))
    else:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
        result = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    if not result:
        raise RelayError('Windows 凭证保护失败；请使用保存这些站点的 Windows 用户账户。')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _parse(data):
    try:
        return tomllib.loads(data.decode('utf-8'))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise RelayError('config.toml 格式无效，未修改配置。请先修复 TOML 格式。') from None


def _statements(text):
    """Yield complete lexical statements, preserving original bytes after UTF-8 decoding."""
    start = index = 0
    quote = None
    triple = False
    depth = 0
    comment = False
    while index < len(text):
        char = text[index]
        if comment:
            if char == '\n':
                comment = False
                if depth == 0:
                    yield text[start:index + 1]
                    start = index + 1
            index += 1
            continue
        if quote:
            if quote == '"' and char == '\\':
                index += 2
                continue
            if triple and text.startswith(quote * 3, index):
                # TOML allows one or two quotes at the end of a multiline string.
                run = 3
                while index + run < len(text) and text[index + run] == quote:
                    run += 1
                index += run
                quote = None
                triple = False
                continue
            if not triple and char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            triple = text.startswith(char * 3, index)
            index += 3 if triple else 1
            continue
        if char == '#':
            comment = True
        elif char in '[{':
            depth += 1
        elif char in ']}':
            depth -= 1
        elif char == '\n' and depth == 0:
            yield text[start:index + 1]
            start = index + 1
        index += 1
    if start < len(text):
        yield text[start:]


def _table_path(statement):
    parsed = tomllib.loads(statement + '\n__relay_boundary_marker__ = true\n')
    def find(value, path):
        if isinstance(value, list):
            return find(value[-1], path)
        if isinstance(value, dict):
            if '__relay_boundary_marker__' in value:
                return path
            for key, item in value.items():
                result = find(item, path + (key,))
                if result is not None:
                    return result
        return None
    return find(parsed, ())


def _quote(value):
    return json.dumps(value, ensure_ascii=False)


def _global_provider_ids(config, profiles=()):
    """Keep legacy relay IDs usable when stored conversations are reopened."""
    providers = config.get('model_providers', {})
    if not isinstance(providers, dict):
        raise RelayError('全局 provider 配置格式无效，未修改配置。')
    managed = {'relay_switcher'}
    current_id = config.get('model_provider', 'openai')
    current = providers.get(current_id, {})
    relay_urls = {profile['base_url'].rstrip('/') for profile in profiles
                  if isinstance(profile.get('base_url'), str) and profile['base_url']}
    if current_id not in _BUILTIN_PROVIDER_IDS and isinstance(current, dict) and current:
        managed.add(current_id)
        if isinstance(current.get('base_url'), str) and current['base_url']:
            relay_urls.add(current['base_url'].rstrip('/'))
    for provider_id, provider in providers.items():
        if provider_id in _BUILTIN_PROVIDER_IDS or not isinstance(provider, dict):
            continue
        url = provider.get('base_url')
        if not isinstance(url, str) or not url:
            continue
        # Earlier configurations used "custom" before the switcher introduced
        # its own ID. Match other relay aliases through saved station URLs.
        legacy_custom = provider_id == 'custom' and provider.get('wire_api', 'responses') == 'responses'
        if legacy_custom or url.rstrip('/') in relay_urls:
            managed.add(provider_id)
    return ('relay_switcher', *sorted(managed - {'relay_switcher'}))


def _new_config(original, profile, catalog_path=None, provider_ids=None):
    before = _parse(original)
    provider_ids = _global_provider_ids(before) if provider_ids is None else provider_ids
    text = original.decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    reasoning_effort = _reasoning_effort(profile.get('reasoning_effort', 'high'))
    table = ()
    retained = []
    agents_table_found = False
    for statement in _statements(text):
        stripped = statement.lstrip(' \t\r\n')
        if stripped.startswith('['):
            try:
                table = _table_path(statement)
            except (tomllib.TOMLDecodeError, ValueError):
                raise RelayError('配置包含暂不支持的复杂表头，未修改配置。') from None
            if table == ('agents',):
                agents_table_found = True
                retained.append(statement)
                retained.append('default_subagent_reasoning_effort = "max"' + newline)
                continue
        if len(table) >= 2 and table[0] == 'model_providers' and table[1] in provider_ids:
            continue
        if table == ('agents',) and stripped and not stripped.startswith('#'):
            entry = tomllib.loads('[agents]' + newline + statement)
            if 'default_subagent_reasoning_effort' in entry.get('agents', {}):
                continue
        if not table and stripped and not stripped.startswith('#'):
            entry = tomllib.loads(statement)
            if 'model_providers' in entry:
                raise RelayError('根节点使用了内联或点式 provider 定义，请先改成独立 provider 表。配置未修改。')
            if 'agents' in entry:
                raise RelayError('根节点使用了内联或点式 agents 定义，请先改成独立 [agents] 表。配置未修改。')
            if ('model_provider' in entry or (profile['model'] and 'model' in entry)
                    or 'model_reasoning_effort' in entry
                    or 'conversationDetailMode' in entry
                    or (catalog_path is not None and 'model_catalog_json' in entry)):
                continue
        retained.append(statement)
    if not agents_table_found:
        retained.extend(('[agents]' + newline, 'default_subagent_reasoning_effort = "max"' + newline))
    prefix = 'model_provider = "relay_switcher"' + newline
    if profile['model']:
        prefix += 'model = ' + _quote(profile['model']) + newline
    prefix += 'model_reasoning_effort = ' + _quote(_CODEX_REASONING_EFFORTS[reasoning_effort]) + newline
    prefix += 'conversationDetailMode = "STEPS_COMMANDS"' + newline
    if catalog_path is not None:
        prefix += 'model_catalog_json = ' + _quote(str(catalog_path)) + newline
    provider = ''.join(newline.join(('[model_providers.' + _quote(provider_id) + ']',
                                    'name = ' + _quote(profile['name']),
                                    'base_url = ' + _quote(profile['base_url']),
                                    'wire_api = "responses"',
                                    'requires_openai_auth = false',
                                    'experimental_bearer_token = ' + _quote(profile['api_key']))) + newline
                       for provider_id in provider_ids)
    retained_text = ''.join(retained)
    if retained_text and not retained_text.endswith('\n'):
        retained_text += newline
    result = (prefix + retained_text + newline + provider).encode('utf-8')
    after = _parse(result)
    # A second semantic guard prevents a scanner mistake from dropping unrelated settings.
    def unrelated(value):
        value = dict(value)
        value.pop('model_provider', None)
        if profile['model']:
            value.pop('model', None)
        value.pop('model_reasoning_effort', None)
        value.pop('conversationDetailMode', None)
        if catalog_path is not None:
            value.pop('model_catalog_json', None)
        agents = dict(value.get('agents', {}))
        agents.pop('default_subagent_reasoning_effort', None)
        if agents:
            value['agents'] = agents
        else:
            value.pop('agents', None)
        providers = dict(value.get('model_providers', {}))
        for provider_id in provider_ids:
            providers.pop(provider_id, None)
        if providers:
            value['model_providers'] = providers
        else:
            value.pop('model_providers', None)
        return value
    if unrelated(before) != unrelated(after):
        raise RelayError('无法安全保留现有配置，未修改配置。')
    return result


def _validate(name, base_url, api_key, model, require_key=True):
    if not all(isinstance(item, str) for item in (name, base_url, api_key, model)):
        raise RelayError('站点字段必须是文本。')
    name, base_url, api_key, model = (item.strip() for item in (name, base_url, api_key, model))
    if not name:
        raise RelayError('请输入站点名称。')
    if any(ord(char) < 32 or ord(char) == 127 for item in (name, base_url, api_key, model) for char in item):
        raise RelayError('站点字段不能包含换行或控制字符。')
    try:
        parts = urlsplit(base_url)
        valid = parts.scheme in ('https', 'http') and parts.hostname and not parts.username and not parts.password and not parts.query and not parts.fragment
        valid = valid and not any(char.isspace() for char in base_url)
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise RelayError('请输入完整的 http:// 或 https:// API 地址；不能包含用户名、密码、查询参数或片段。')
    if require_key and not api_key:
        raise RelayError('请输入此站点的 API 密钥。')
    return dict(name=name, base_url=base_url.rstrip('/'), api_key=api_key, model=model)


def _validate_models(models):
    if not isinstance(models, list):
        raise RelayError('模型列表必须是文本列表。')
    result = []
    seen = set()
    for model in models:
        if not isinstance(model, str):
            raise RelayError('模型列表只能包含非空模型名称。')
        model = model.strip()
        if not model or any(ord(char) < 32 or ord(char) == 127 for char in model):
            raise RelayError('模型名称不能为空，也不能包含控制字符。')
        if model not in seen:
            seen.add(model)
            result.append(model)
    return result


def _optional_model(value):
    """Validate an optional model ID, where an empty value means no override."""
    if not isinstance(value, str):
        raise RelayError('Auto Review 模型必须是文本。')
    value = value.strip()
    return _validate_models([value])[0] if value else ''


def _catalog_auto_review_model(home, config):
    """Read the current model's reviewer override without exposing catalog data."""
    catalog_value = config.get('model_catalog_json')
    model = config.get('model')
    if not isinstance(catalog_value, str) or not catalog_value.strip() or not isinstance(model, str):
        return ''
    path = Path(catalog_value).expanduser()
    if not path.is_absolute():
        path = Path(home) / path
    try:
        home = Path(home).expanduser().resolve()
        path = path.resolve()
        if not path.is_relative_to(home):
            return ''
        raw = path.read_bytes()
        if len(raw) > 10 * 1024 * 1024:
            return ''
        catalog = json.loads(raw.decode('utf-8-sig'))
        entries = catalog.get('models', []) if isinstance(catalog, dict) else []
        matches = [entry for entry in entries
                   if isinstance(entry, dict) and entry.get('slug') == model]
        if len(matches) != 1:
            return ''
        return _optional_model(matches[0].get('auto_review_model_override', '') or '')
    except (OSError, ValueError, TypeError, UnicodeError, RelayError):
        return ''


class RelayManager:
    @_safe
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.config = self.home / 'config.toml'
        self.directory = self.home / 'relay-switcher'
        self.profiles_file = self.directory / 'profiles.dpapi'
        self.backup_file = self.directory / 'backup.dpapi'
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if not self.profiles_file.exists():
                current, key = self._current_with_key()
                profiles = []
                if current['base_url']:
                    imported = _validate(current['name'], current['base_url'], key, current['model'], require_key=False)
                    imported['id'] = uuid.uuid4().hex
                    imported['models'] = [imported['model']] if imported['model'] else []
                    imported['reasoning_effort'] = current['reasoning_effort']
                    imported['auto_review_model'] = current['auto_review_model']
                    profiles.append(imported)
                self._save(profiles)

    @contextmanager
    def _locked(self):
        import msvcrt
        with (self.directory / '.lock').open('a+b') as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            deadline = time.monotonic() + 8
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RelayError('另一个切换窗口正在操作，请稍后重试。') from None
                    time.sleep(.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _load(self):
        try:
            result = json.loads(_crypt(self.profiles_file.read_bytes(), False).decode('utf-8'))
            if result.get('version') != 1 or not isinstance(result.get('profiles'), list):
                raise ValueError
            for profile in result['profiles']:
                if not isinstance(profile, dict):
                    raise ValueError
                profile['models'] = _validate_models(profile.get('models', []))
                profile['reasoning_effort'] = _reasoning_effort(profile.get('reasoning_effort', 'high'))
                if 'auto_review_model' in profile:
                    profile['auto_review_model'] = _optional_model(profile['auto_review_model'])
            return result['profiles']
        except (ValueError, UnicodeError):
            raise RelayError('站点资料损坏，未修改配置。') from None

    def _save(self, profiles):
        data = json.dumps(dict(version=1, profiles=profiles), ensure_ascii=False).encode('utf-8')
        _atomic(self.profiles_file, _crypt(data, True))

    def _read(self):
        return self.config.read_bytes() if self.config.exists() else b''

    def _current_with_key(self):
        config = _parse(self._read())
        provider_id = config.get('model_provider', 'openai')
        providers = config.get('model_providers', {})
        provider = providers.get(provider_id, {}) if isinstance(providers, dict) else {}
        if not isinstance(provider, dict):
            raise RelayError('当前 provider 格式无效。')
        default_url = config.get('openai_base_url', '') if provider_id == 'openai' else ''
        current = dict(name=provider.get('name', provider_id), base_url=provider.get('base_url', default_url),
                       model=config.get('model', ''),
                       reasoning_effort=_stored_reasoning_effort(config.get('model_reasoning_effort', 'high')),
                       auto_review_model=_catalog_auto_review_model(self.home, config))
        key = provider.get('experimental_bearer_token', '')
        if not key and isinstance(provider.get('env_key'), str):
            key = _environment(provider['env_key'])
        uses_openai_auth = provider_id == 'openai' or provider.get('requires_openai_auth') is True
        if not key and uses_openai_auth and (self.home / 'auth.json').exists():
            try:
                auth = json.loads((self.home / 'auth.json').read_text(encoding='utf-8'))
                key = auth.get('OPENAI_API_KEY', '') or ''
            except (ValueError, UnicodeError):
                pass
        return current, key

    @property
    def has_backup(self):
        return self.backup_file.exists()

    @_safe
    def current(self):
        with self._locked():
            result, key = self._current_with_key()
            for profile in self._load():
                if profile['base_url'].rstrip('/') == result['base_url'].rstrip('/') and profile['api_key'] == key and (not profile['model'] or profile['model'] == result['model']):
                    result['id'] = profile['id']
                    if 'auto_review_model' in profile:
                        result['auto_review_model'] = profile['auto_review_model']
                    break
            return result

    @_safe
    def list_profiles(self):
        with self._locked():
            return [{key: value for key, value in profile.items() if key != 'api_key'} for profile in self._load()]

    @_safe
    def get_profile(self, profile_id):
        with self._locked():
            for profile in self._load():
                if profile['id'] == profile_id:
                    return dict(profile)
        raise RelayError('站点不存在，请刷新列表。')

    @_safe
    def save_profile(self, name, base_url, api_key, model='', profile_id=None, models=None,
                     reasoning_effort=None, auto_review_model=None):
        profile = _validate(name, base_url, api_key, model)
        new_models = _validate_models(models) if models is not None else None
        new_auto_review_model = _optional_model(auto_review_model) if auto_review_model is not None else None
        with self._locked():
            profiles = self._load()
            previous = next((item for item in profiles if item['id'] == profile_id), None)
            if profile_id is not None and previous is None:
                raise RelayError('站点已被另一窗口删除，请刷新列表。')
            same_connection = (previous is not None and previous['base_url'] == profile['base_url']
                               and previous['api_key'] == profile['api_key'])
            profile['reasoning_effort'] = _reasoning_effort(
                reasoning_effort if reasoning_effort is not None else
                previous.get('reasoning_effort', 'high') if previous is not None else 'high'
            )
            if new_auto_review_model is not None:
                profile['auto_review_model'] = new_auto_review_model
            elif same_connection and 'auto_review_model' in previous:
                profile['auto_review_model'] = previous['auto_review_model']
            elif not same_connection:
                profile['auto_review_model'] = ''
            if new_models is not None:
                profile['models'] = new_models
            elif same_connection:
                profile['models'] = previous['models']
            else:
                profile['models'] = []
            profile['id'] = profile_id or uuid.uuid4().hex
            profiles = [item for item in profiles if item['id'] != profile['id']]
            profiles.append(profile)
            self._save(profiles)
        return profile['id']

    @_safe
    def save_models(self, profile_id, models, base_url=None, api_key=None, resolved_base_url=None):
        models = _validate_models(models)
        with self._locked():
            profiles = self._load()
            profile = next((item for item in profiles if item['id'] == profile_id), None)
            if profile is None:
                raise RelayError('站点已被删除，获取到的模型列表未保存。')
            if base_url is not None and (not isinstance(base_url, str) or base_url.strip().rstrip('/') != profile['base_url']):
                raise RelayError('站点地址已被修改，获取到的模型列表未保存。请重新获取。')
            if api_key is not None and (not isinstance(api_key, str) or api_key.strip() != profile['api_key']):
                raise RelayError('站点凭证已被修改，获取到的模型列表未保存。请重新获取。')
            if resolved_base_url is not None:
                resolved = _validate(profile['name'], resolved_base_url, profile['api_key'], profile['model'], require_key=False)
                profile['base_url'] = resolved['base_url']
            profile['models'] = models
            self._save(profiles)

    @_safe
    def delete_profile(self, profile_id):
        with self._locked():
            profiles = self._load()
            remaining = [item for item in profiles if item['id'] != profile_id]
            if len(remaining) == len(profiles):
                raise RelayError('站点不存在，请刷新列表。')
            self._save(remaining)

    @_safe
    def switch(self, profile_id):
        with self._locked():
            stored_profiles = self._load()
            stored_profile = next((item for item in stored_profiles if item['id'] == profile_id), None)
            if stored_profile is None:
                raise RelayError('站点不存在，请刷新列表。')
            profile = _validate(stored_profile['name'], stored_profile['base_url'], stored_profile['api_key'], stored_profile['model'])
            existed = self.config.exists()
            original = self._read()
            parsed = _parse(original)
            effective_model = profile['model'] or parsed.get('model')
            if not isinstance(effective_model, str) or not effective_model.strip():
                raise RelayError('请先选择或填写默认模型，再切换站点。')
            from relay_catalog import write_model_catalog
            catalog_path = write_model_catalog(
                self.home, parsed, stored_profile.get('models', []), effective_model,
                auto_review_model=stored_profile.get('auto_review_model', '')
            )
            profile['reasoning_effort'] = stored_profile.get('reasoning_effort', 'high')
            provider_ids = _global_provider_ids(parsed, stored_profiles)
            result = _new_config(original, profile, catalog_path, provider_ids)
            backup = dict(version=1, existed=existed, original=base64.b64encode(original).decode('ascii'),
                          expected=hashlib.sha256(result).hexdigest())
            encrypted = _crypt(json.dumps(backup).encode('utf-8'), True)
            if self._read() != original or self.config.exists() != existed:
                raise RelayError('配置被其他程序修改，请重新读取后再切换。')
            previous_backup = self.backup_file.read_bytes() if self.backup_file.exists() else None
            _atomic(self.backup_file, encrypted)
            try:
                if self._read() != original or self.config.exists() != existed:
                    raise RelayError('配置被其他程序修改，请重新读取后再切换。')
                _atomic(self.config, result)
            except (OSError, RelayError):
                try:
                    if previous_backup is None:
                        self.backup_file.unlink()
                    else:
                        _atomic(self.backup_file, previous_backup)
                except OSError:
                    raise RelayError('配置写入失败，且无法恢复上一份备份。请检查磁盘空间与目录权限。') from None
                raise
            return provider_ids

    @_safe
    def restore(self):
        with self._locked():
            if not self.backup_file.exists():
                raise RelayError('没有可恢复的切换备份。')
            try:
                backup = json.loads(_crypt(self.backup_file.read_bytes(), False).decode('utf-8'))
                original = base64.b64decode(backup['original'], validate=True)
            except (ValueError, KeyError, UnicodeError):
                raise RelayError('恢复备份损坏，未修改配置。') from None
            if not self.config.exists() or hashlib.sha256(self._read()).hexdigest() != backup.get('expected'):
                raise RelayError('配置在切换后又被修改，为避免覆盖这些修改，自动恢复已停止。请先保存手工修改，再自行决定如何恢复。')
            _parse(original)
            if backup['existed']:
                _atomic(self.config, original)
            else:
                self.config.unlink()
            self.backup_file.unlink()
