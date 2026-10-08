"""Provider settings shared by the editor, probes and conversation transport.

No desktop, memory or private-file imports. A bound connection is immutable for
one conversation, including its tool rounds; saving settings affects the next.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://api.deepseek.com"
# Official API documentation, checked 2026-10-08. These are API IDs, not labels.
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")
EFFORTS = {"none": None, "low": "low", "medium": "high", "high": "high", "max": "max"}
_current = ContextVar("aipet_provider", default=None)


def validate_base(value):
    value = str(value or "").strip().rstrip("/")
    u = urllib.parse.urlsplit(value)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise ValueError("请填写完整的 API 地址，例如 https://api.deepseek.com。")
    if u.username or u.password or u.query or u.fragment:
        raise ValueError("API 地址不能包含密码、查询参数或片段；密钥请填在专用输入框。")
    if u.path.endswith(("/chat/completions", "/models", "/responses")):
        raise ValueError("请填写接口根地址，不要包含 /chat/completions、/models 或 /responses。")
    if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("远程接口需要 https；本机 localhost 接口可以使用 http。")
    return value


def official_deepseek(base):
    u = urllib.parse.urlsplit(base)
    return u.scheme == "https" and u.hostname == "api.deepseek.com"


def validate_model(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}", value) or "::" in value:
        raise ValueError("模型名必须是接口的准确 ID，不能填写中文显示名、空格或“服务商::”前缀。")
    return value


def legacy_model(value):
    value = str(value or "deepseek-flash").strip().split("::", 1)[-1]
    return {"deepseek-v4-flash": "deepseek-flash",
            "deepseek-v4-flash-vision-exp": "deepseek-flash"}.get(value, value)


@dataclass(frozen=True)
class Connection:
    base: str
    key: str = field(repr=False)
    model: str = "deepseek-flash"
    protocol: str = "compatible"
    name: str = "当前接口"
    tools: bool = True
    source: str = "本机配置"

    def fingerprint(self):
        return hashlib.sha256((self.base + "\0" + self.key + "\0" + self.model + "\0" + self.protocol).encode()).hexdigest()


def current():
    return _current.get()


@contextmanager
def bind(connection):
    token = _current.set(connection)
    try:
        yield connection
    finally:
        _current.reset(token)


def legacy_profile(secrets, environ=None):
    """Editable local values; never copy environment credentials into the file."""
    base = secrets.get("deepseek_base_url") or secrets.get("base_url") or DEFAULT_BASE
    return {"name": "现有对话接口", "base_url": base,
            "api_key": secrets.get("deepseek_api_key") or secrets.get("auth_token") or "",
            "model": legacy_model(secrets.get("model") or "deepseek-flash"),
            "protocol": "deepseek" if official_deepseek(base) else "compatible",
            "tools": True, "use_environment": True}


def from_profile(profile, environ=None):
    env = os.environ if environ is None else environ
    key = str(profile.get("api_key") or "").strip()
    base = str(profile.get("base_url") or DEFAULT_BASE).strip().rstrip("/")
    sources = []
    if profile.get("use_environment"):
        for names, target in ((("DEEPSEEK_API_KEY", "OPENAI_API_KEY"), "key"),
                              (("DEEPSEEK_BASE_URL", "OPENAI_BASE_URL"), "base")):
            for name in names:
                if env.get(name):
                    if target == "key": key = env[name]
                    else: base = env[name].rstrip("/")
                    sources.append(name)
                    break
    if any(ord(char) < 32 for char in key):
        raise ValueError("密钥中不能包含换行或控制字符。")
    protocol = profile.get("protocol") or ("deepseek" if official_deepseek(base) else "compatible")
    if protocol not in ("deepseek", "compatible"):
        raise ValueError("未知接口格式。")
    return Connection(validate_base(base), key, validate_model(profile.get("model")),
                      protocol, str(profile.get("name") or "未命名接口"),
                      profile.get("tools", True) is True,
                      "环境变量：" + "、".join(sources) if sources else "本机配置")


def resolve(secrets, params=None, environ=None):
    params = params or {}
    profiles = secrets.get("model_profiles") or {}
    selected = params.get("model_profile") or secrets.get("default_model_profile")
    if profiles and selected:
        if selected not in profiles:
            raise ValueError("当前档位选择的模型方案已不存在，请在设置中重新选择。")
        profile = dict(profiles[selected])
        override = params.get("model_override", "")
        if override:
            profile["model"] = validate_model(override)
    else:
        profile = legacy_profile(secrets)
        model = params.get("model") or secrets.get("model") or "deepseek-flash"
        if secrets.get("model") and (not params.get("model") or str(model).startswith("deepseek::")):
            model = secrets["model"]
        profile["model"] = legacy_model(model)
    return from_profile(profile, environ)


def vision_connection(secrets, listing=False):
    base = str(secrets.get("vision_base_url") or "").rstrip("/")
    validate_base(base)
    return from_profile({"base_url": base, "api_key": secrets.get("vision_api_key", ""),
                         "model": secrets.get("vision_model") or ("list-probe" if listing else ""),
                         "protocol": "deepseek" if official_deepseek(base) else "compatible",
                         "name": "读图接口"})


def apply_parameters(body, connection, params):
    """Only DeepSeek receives its proprietary thinking extension."""
    if not connection.tools:
        body.pop("tools", None)
    if connection.protocol == "deepseek":
        effort = EFFORTS.get(params.get("reasoning_effort", "low"), "high")
        body["thinking"] = {"type": "enabled" if effort else "disabled"}
        if effort:
            body["reasoning_effort"] = effort
            return
    body["temperature"] = params.get("temperature", .75)
    for name in ("frequency_penalty", "presence_penalty"):
        if name in params:
            body[name] = params[name]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an Authorization header to another URL.


def request_json(connection, path, payload=None, timeout=20):
    if not connection.key:
        raise ValueError("尚未填写此接口的 API key。")
    req = urllib.request.Request(connection.base + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + connection.key, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(524289)
        if len(raw) > 524288:
            raise ValueError("接口返回内容过大。")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("接口返回格式不是 JSON 对象。")
        return data
    except urllib.error.HTTPError as error:
        # Provider messages can contain credentials or signed URLs. Use bounded,
        # fixed messages in the UI instead of echoing response bodies.
        error.close()
        hints = {401: "密钥无效或没有权限", 403: "接口拒绝访问", 404: "地址或模型不存在",
                 402: "额度不足", 429: "接口限流", 400: "接口不接受此模型或请求参数"}
        raise ValueError(f"HTTP {error.code}：{hints.get(error.code, '接口请求失败')}。") from None
    except (OSError, urllib.error.URLError):
        raise ValueError("无法连接接口，请检查地址、网络和代理。") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("接口没有返回有效 JSON。") from None


def list_models(connection):
    data = request_json(connection, "/models")
    rows = data.get("data")
    if not isinstance(rows, list):
        raise ValueError("该服务未提供标准模型列表；可输入官方 ID 后测试调用。")
    models = sorted({row["id"] for row in rows if isinstance(row, dict)
                     and isinstance(row.get("id"), str) and len(row["id"]) <= 256})
    if not models:
        raise ValueError("接口没有返回可用模型。")
    return models


def probe(connection, vision=False):
    body = {"model": connection.model, "messages": [{"role": "user", "content": "Reply OK."}],
            "max_tokens": 64, "stream": False}
    if vision:
        # A generated 64-pixel square image, never a user's photograph or screenshot.
        body["messages"][0]["content"] = [
            {"type": "text", "text": "Describe the image briefly."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
             "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAe0lEQVR4nO3PUQkAIBTAwFfKCPavYQxD+HEIgwW4zVn764YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS1oQAsa0IIGtKABLXjsAqkjIUspJL0dAAAAAElFTkSuQmCC"}}]
    apply_parameters(body, connection, {"reasoning_effort": "none", "temperature": 0})
    data = request_json(connection, "/chat/completions", body, timeout=60 if vision else 30)
    if not isinstance(data.get("choices"), list) or not data["choices"]:
        raise ValueError("接口返回成功状态，但没有有效的模型响应。")
    first = data["choices"][0]
    content = first.get("message", {}).get("content") if isinstance(first, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("接口没有返回可用文字，请检查模型能力及调用格式。")
    returned = str(data.get("model") or connection.model)
    returned = returned if re.fullmatch(r"[A-Za-z0-9._:/@+\-]{1,256}", returned) else "未提供型号"
    return "调用成功；请求 ID：" + connection.model + "；返回型号：" + returned
