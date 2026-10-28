"""Immutable Codex catalogs for all relay models and one chosen default."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import urllib.request

from relay_core import RelayError, _atomic, _validate_models

MAX_CATALOG_BYTES = 10 * 1024 * 1024
BUNDLED_CATALOG = Path(__file__).with_name('bundled_model_catalog.json')
OFFICIAL_CATALOG_URL = 'https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/models.json'


def refresh_official_model_catalog(path: Path) -> int:
    """Download and validate OpenAI Codex's public model metadata catalog."""
    request = urllib.request.Request(
        OFFICIAL_CATALOG_URL, headers={'User-Agent': 'Codex-Relay-Switcher'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise ValueError('catalog too large')
        catalog = json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(catalog, dict) or not isinstance(catalog.get('models'), list)
                or any(not isinstance(entry, dict) or not isinstance(entry.get('slug'), str)
                       for entry in catalog['models'])):
            raise ValueError('invalid model catalog')
        _atomic(Path(path), raw)
        return len(catalog['models'])
    except Exception:
        raise RelayError('无法刷新 OpenAI 官方 Codex 模型目录；保留已缓存目录，未修改全局配置。') from None


def _read_models(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise ValueError('catalog too large')
        catalog = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(catalog, dict) or not isinstance(catalog.get('models'), list):
            raise ValueError('invalid catalog')
        if any(not isinstance(item, dict) or not isinstance(item.get('slug'), str)
               for item in catalog['models']):
            raise ValueError('invalid model entries')
        return catalog['models']
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        raise RelayError('模型目录无法读取或格式无效，未修改 Codex 配置。') from None


def _validate_entry(entry):
    required = {
        'slug': str, 'display_name': str, 'supported_reasoning_levels': list,
        'shell_type': str, 'visibility': str, 'supported_in_api': bool,
        'priority': int, 'support_verbosity': bool, 'truncation_policy': dict,
        'experimental_supported_tools': list,
    }
    if any(type(entry.get(key)) is not kind for key, kind in required.items()):
        raise RelayError('所选模型的目录信息不完整，未修改 Codex 配置。')
    messages = entry.get('model_messages')
    template = messages.get('instructions_template') if isinstance(messages, dict) else None
    if not isinstance(entry.get('base_instructions'), str) and not isinstance(template, str):
        raise RelayError('所选模型缺少 Codex 指令信息，未修改 Codex 配置。')
    if entry['shell_type'] not in ('shell_command', 'unified_exec', 'local'):
        raise RelayError('所选模型的工具信息无效，未修改 Codex 配置。')
    truncation = entry['truncation_policy']
    if truncation.get('mode') not in ('tokens', 'bytes') or type(truncation.get('limit')) is not int or truncation['limit'] <= 0:
        raise RelayError('所选模型的截断设置无效，未修改 Codex 配置。')


def _matches(path, models, optional=False):
    try:
        entries = {}
        for entry in _read_models(path):
            if entry['slug'] in models:
                entries.setdefault(entry['slug'], []).append(entry)
    except RelayError:
        if not optional:
            raise
        return {}
    result = {}
    for model, matching in entries.items():
        try:
            if len(matching) != 1:
                raise RelayError('模型目录中存在重复名称，未修改 Codex 配置。')
            entry = copy.deepcopy(matching[0])
            _validate_entry(entry)
            result[model] = entry
        except RelayError:
            if not optional:
                raise
    return result


def _fallback(model):
    # A /models response supplies IDs, not capabilities. Do not borrow another
    # model's context size, vision support, reasoning levels, or tool policy.
    return {
        'slug': model,
        'display_name': model,
        'description': '中转站自定义模型',
        'supported_reasoning_levels': [],
        'default_reasoning_level': 'none',
        'shell_type': 'shell_command',
        'visibility': 'list',
        'supported_in_api': True,
        'priority': 0,
        'support_verbosity': False,
        'input_modalities': ['text'],
        'supports_parallel_tool_calls': False,
        'supports_reasoning_summaries': False,
        'supports_reasoning_summary_parameter': False,
        'supports_reasoning_effort_updates': False,
        'truncation_policy': {'mode': 'tokens', 'limit': 10000},
        'experimental_supported_tools': [],
        'base_instructions': (
            'You are a coding assistant running in Codex. Help the user complete '
            'their requested task. Read relevant workspace instructions and inspect '
            'existing code before making changes. Use the tools provided by Codex '
            'when needed. Preserve unrelated user changes. Verify the work with '
            'appropriate checks, and report the result and any remaining limitations '
            'clearly and concisely. Do not claim an action succeeded without evidence.'
        ),
    }


def write_model_catalog(home: Path, config: dict, models: list[str], default_model: str,
                        auto_review_model: str = '') -> Path:
    """Expose all fetched IDs and mark the chosen model as Codex's default.

    Explicit user catalogs are required to be readable. The optional Codex cache
    may be stale or incomplete, so it is skipped when invalid. Old generated
    files remain available for configuration rollback. The default is placed
    first with priority zero because Codex marks the first catalog entry as
    default. A manually entered default is included even if not in /models.
    """
    try:
        if (not isinstance(default_model, str) or not default_model.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in default_model)):
            raise RelayError('请先选择或填写一个有效的默认模型。')
        if not isinstance(auto_review_model, str) or any(ord(char) < 32 or ord(char) == 127
                                                        for char in auto_review_model):
            raise RelayError('Auto Review 模型必须是有效的模型名称。')
        auto_review_model = auto_review_model.strip()
        if not auto_review_model:
            auto_review_model = None
        else:
            auto_review_model = _validate_models([auto_review_model])[0]
        fetched = _validate_models(models)
        ordered = [default_model] + [model for model in fetched if model != default_model]
        home = Path(home).expanduser().resolve()
        resolved = {}
        if 'model_catalog_json' in config:
            value = config['model_catalog_json']
            if not isinstance(value, str) or not value.strip():
                raise RelayError('现有模型目录路径无效，未修改 Codex 配置。')
            source = Path(value).expanduser()
            if not source.is_absolute():
                source = home / source
            generated_catalogs = (home / 'relay-switcher' / 'catalogs').resolve()
            try:
                source_is_generated = source.resolve().is_relative_to(generated_catalogs)
            except OSError:
                source_is_generated = False
            if not source_is_generated:
                resolved.update(_matches(source, set(ordered)))
        pending = set(ordered) - resolved.keys()
        if pending:
            official = home / 'relay-switcher' / 'official_model_catalog.json'
            if official.exists():
                resolved.update(_matches(official, pending, optional=True))
        pending = set(ordered) - resolved.keys()
        if pending:
            cache = home / 'models_cache.json'
            if cache.exists():
                resolved.update(_matches(cache, pending, optional=True))
        pending = set(ordered) - resolved.keys()
        if pending:
            resolved.update(_matches(BUNDLED_CATALOG, pending))
        entries = []
        for priority, model in enumerate(ordered):
            entry = resolved.get(model) or _fallback(model)
            # Every fetched option must be visible, while the chosen default
            # sorts first. Avoid upgrades that would suggest an unlisted model.
            entry.update(visibility='list', supported_in_api=True, priority=priority, upgrade=None,
                         auto_review_model_override=auto_review_model)
            _validate_entry(entry)
            entries.append(entry)
        raw = (json.dumps({'models': entries}, ensure_ascii=False, sort_keys=True,
                          indent=2, allow_nan=False) + '\n').encode('utf-8')
        if len(raw) > MAX_CATALOG_BYTES:
            raise RelayError('模型目录过大，未修改 Codex 配置。')
        digest = hashlib.sha256(raw).hexdigest()
        target = home / 'relay-switcher' / 'catalogs' / (digest + '.json')
        if target.exists():
            if target.read_bytes() != raw:
                raise RelayError('已生成的模型目录被修改，请先检查该文件；Codex 配置未改动。')
        else:
            _atomic(target, raw)
        return target
    except RelayError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise RelayError('模型目录生成失败，请检查目录权限、格式和磁盘空间。') from None


def write_selected_catalog(home: Path, config: dict, model: str) -> Path:
    """Compatibility helper for a manually selected model with no fetched list."""
    return write_model_catalog(home, config, [], model)
