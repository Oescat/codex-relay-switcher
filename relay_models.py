"""Fetch a relay's advertised model IDs without following credential-bearing redirects."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import math
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from relay_core import RelayError


_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass
class ModelCatalog:
    """A model list paired with the API base that actually answered its request."""
    base_url: str
    models: list[str]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _control(text):
    return any(not character.isprintable() for character in text)


def _endpoints(base_url):
    if not isinstance(base_url, str) or _control(base_url):
        raise RelayError('API 地址必须是文本，且不能包含控制字符。')
    base_url = base_url.strip()
    try:
        parts = urlsplit(base_url)
        valid = (parts.scheme in ('http', 'https') and parts.hostname
                 and parts.username is None and parts.password is None
                 and '?' not in base_url and '#' not in base_url
                 and not any(character.isspace() for character in base_url))
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise RelayError('请输入完整的 http:// 或 https:// API 地址；不能包含用户名、密码、查询参数或片段。')
    path = parts.path.rstrip('/')
    bare_domain = not path
    for suffix in ('/chat/completions', '/responses'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if bare_domain:
        paths = ('/v1/models', '/models')
    else:
        paths = (path if path.endswith('/models') else path + '/models',)
    return [urlunsplit((parts.scheme, parts.netloc, path, '', '')) for path in paths]


def _http_error(code):
    if 300 <= code < 400:
        return RelayError('站点返回了重定向。为避免密钥被发送到其他地址，请填写站点最终的 API 地址。')
    messages = {
        401: '认证失败（HTTP 401），请检查此站点的 API Key。',
        403: '访问被拒绝（HTTP 403），请检查密钥是否有模型列表权限。',
        404: '站点未提供此模型列表地址（HTTP 404），请检查 API 地址，或手动填写模型名称。',
        405: '站点不支持读取模型列表（HTTP 405），可手动填写模型名称。',
        429: '请求过于频繁或额度不足（HTTP 429），请稍后重试。',
    }
    if code in messages:
        return RelayError(messages[code])
    if 500 <= code < 600:
        return RelayError(f'站点服务暂时异常（HTTP {code}），请稍后重试。')
    return RelayError(f'读取模型列表失败（HTTP {code}），请检查站点连接设置。')


def _request(opener, endpoint, api_key, timeout):
    request = Request(endpoint, headers={'Authorization': 'Bearer ' + api_key,
                                        'Accept': 'application/json'}, method='GET')
    with opener.open(request, timeout=timeout) as response:
        code = response.getcode()
        if not 200 <= code < 300:
            raise _http_error(code)
        length = response.headers.get('Content-Length')
        if length:
            try:
                oversized = int(length) > _MAX_RESPONSE_BYTES
            except ValueError:
                oversized = False
            if oversized:
                raise RelayError('模型列表响应过大，已停止读取。')
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise RelayError('模型列表响应过大，已停止读取。')
    return body


def _model_ids(body):
    try:
        payload = json.loads(body.decode('utf-8'))
    except (UnicodeError, ValueError, RecursionError):
        raise RelayError('站点返回的模型列表格式无效，需要 UTF-8 JSON；请检查 API 地址是否误填为网页地址。') from None
    if isinstance(payload, dict):
        entries = payload.get('data') if 'data' in payload else payload.get('models')
    else:
        entries = payload
    if not isinstance(entries, list):
        raise RelayError('站点返回的模型列表格式无效，需要 data 数组与模型 id。')
    models = []
    seen = set()
    for entry in entries:
        model = entry.get('id') if isinstance(entry, dict) else entry
        if not isinstance(model, str) or _control(model):
            continue
        model = model.strip()
        if not model or any(character.isspace() for character in model) or model in seen:
            continue
        seen.add(model)
        models.append(model)
    if not models:
        raise RelayError('模型列表为空，或未包含有效的模型 id。可以手动填写站点提供的模型名称。')
    return models


def fetch_model_catalog(base_url: str, api_key: str, timeout: float = 15) -> ModelCatalog:
    """Read model IDs and return the successful endpoint's reusable API base."""
    endpoints = _endpoints(base_url)
    if not isinstance(api_key, str) or _control(api_key):
        raise RelayError('API Key 必须是文本，且不能包含控制字符。')
    api_key = api_key.strip()
    if not api_key:
        raise RelayError('请先填写此站点的 API Key，再读取模型列表。')
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise RelayError('读取模型列表的超时时间必须是大于零的有限数值。') from None
    opener = build_opener(_NoRedirect())
    for index, endpoint in enumerate(endpoints):
        try:
            body = _request(opener, endpoint, api_key, timeout)
            return ModelCatalog(base_url=endpoint[:-len('/models')], models=_model_ids(body))
        except HTTPError as error:
            code = error.code
            error.close()
            if index == 0 and len(endpoints) > 1 and code in (404, 405):
                continue
            raise _http_error(code) from None
        except (TimeoutError, socket.timeout):
            raise RelayError('读取模型列表超时，请检查站点连接或稍后重试。') from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise RelayError('读取模型列表超时，请检查站点连接或稍后重试。') from None
            if isinstance(error.reason, ssl.SSLError):
                raise RelayError('站点的 HTTPS 连接无法验证，请检查地址和证书。') from None
            raise RelayError('无法连接站点，请检查 API 地址、网络和代理设置。') from None
        except (OSError, http.client.HTTPException, ValueError, UnicodeError):
            raise RelayError('读取模型列表失败，请检查 API 地址、密钥格式和网络连接。') from None
    raise RelayError('未能读取模型列表，可手动填写站点提供的模型名称。')


def fetch_models(base_url: str, api_key: str, timeout: float = 15) -> list[str]:
    """Read advertised IDs without changing the original list-returning interface."""
    return fetch_model_catalog(base_url, api_key, timeout).models
