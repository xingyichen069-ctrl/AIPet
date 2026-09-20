"""Conversation guided plugin drafting for the standalone AIPet harness.

The model is only allowed to return a structured draft.  It never receives a
filesystem tool and it never gets to choose the activation path.  The host
materializes the draft in a disposable directory, validates it, runs the
plugin's offline self-test, and optionally sends it through the normal atomic
activation pipeline.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from .core import (
    HarnessError,
    MAX_FILE_BYTES,
    NAME_RE,
    VERSION_RE,
    _safe_relative,
    auto_use,
    validate_plugin,
)


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DRAFT_BYTES = 8 * 1024 * 1024
SECRET_SHAPE_RE = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|bearer\s+[A-Za-z0-9._-]{12,}|api[_-]?key\s*[:=]\s*[A-Za-z0-9._-]{12,})")
SYSTEM_PROMPT = """你是 AIPet 独立插件编辑器。你只负责提出插件草案，不直接访问文件系统、网络服务或 AIPet 私人数据。

每次只输出一个合法 JSON 对象，不要 Markdown 代码围栏。JSON 必须符合：
{
  "status": "ready" 或 "needs_clarification",
  "reply": "给用户看的简短说明",
  "plugin": {
    "name": "小写安全标识符",
    "version": "三段数字版本",
    "display_name": "显示名称",
    "entrypoint": "entrypoint.py",
    "files": [{"path": "entrypoint.py", "content": "完整文件内容"}]
  }
}

status 为 needs_clarification 时可以省略 plugin，只说明缺少什么；status 为 ready 时必须给出完整插件。
files 不要包含 .aipet-plugin/plugin.json，宿主会根据草案生成它。入口必须是 Python 文件，并实现 --selftest 和 --health：
selftest 必须离线、有限时长、只检查自身；health 只输出 JSON 健康状态。除非用户明确要求，插件不要联网、不要读取密钥、人格、记忆或 AIPet 私人运行数据。
不要在文件内容中放 API key、密码、token 或其他凭据。"""


def _clean_text(value: Any) -> str:
    """Remove lone console surrogates before JSON or UTF-8 output."""
    return str(value or "").encode("utf-8", "replace").decode("utf-8")


def _json_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "".join(parts)
    raise HarnessError("模型没有返回文本内容")


def _parse_json(text: str) -> dict[str, Any]:
    raw = _clean_text(text).strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise HarnessError("模型没有返回合法 JSON")
        try:
            value = json.loads(raw[start:end + 1])
        except (TypeError, ValueError) as exc:
            raise HarnessError("模型返回的 JSON 无法解析") from exc
    if not isinstance(value, dict):
        raise HarnessError("模型返回的 JSON 根节点不是对象")
    return value


@dataclass(frozen=True)
class DraftResult:
    status: str
    reply: str
    plugin: dict[str, Any] | None
    raw: str = ""

    @property
    def ready(self) -> bool:
        return self.status == "ready" and isinstance(self.plugin, dict)


class OpenAICompatClient:
    """Small stdlib-only client for an OpenAI-compatible chat endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str = "qwen", timeout: int = 90):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "qwen").strip()
        self.timeout = max(5, int(timeout))
        if not self.base_url:
            raise HarnessError("缺少模型 API base URL")
        if not self.api_key:
            raise HarnessError("缺少模型 API key；请设置 AIPET_HARNESS_API_KEY")
        if not self.model:
            raise HarnessError("缺少模型名称")

    @property
    def endpoint(self) -> str:
        return self.base_url if self.base_url.endswith("/chat/completions") \
            else self.base_url + "/chat/completions"

    def complete(self, messages: list[dict[str, str]]) -> str:
        safe_messages = [
            {"role": _clean_text(item.get("role")), "content": _clean_text(item.get("content"))}
            for item in messages
        ]
        body = {
            "model": self.model,
            "messages": safe_messages,
            "stream": False,
            "temperature": 0,
            "max_tokens": 8000,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise HarnessError(f"模型 API 请求失败：HTTP {exc.code}") from exc
        except (OSError, TimeoutError) as exc:
            raise HarnessError("模型 API 请求未完成") from exc
        if len(data) > MAX_RESPONSE_BYTES:
            raise HarnessError("模型响应过大，已拒绝")
        try:
            payload = json.loads(data.decode("utf-8", "replace"))
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") if isinstance(choice, dict) else None
            return _json_content(message.get("content") if isinstance(message, dict) else None)
        except (UnicodeError, ValueError, AttributeError, TypeError, IndexError) as exc:
            raise HarnessError("模型 API 返回格式无效") from exc


class GuidedEditor:
    """Keep a short model conversation and materialize only validated drafts."""

    def __init__(self, client: Any):
        self.client = client
        self.messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def turn(self, instruction: str) -> DraftResult:
        text = _clean_text(instruction).strip()
        if not text:
            raise HarnessError("插件编辑指令不能为空")
        self.messages.append({"role": "user", "content": text})
        raw = _clean_text(self.client.complete(self.messages))
        self.messages.append({"role": "assistant", "content": raw})
        value = _parse_json(raw)
        status = str(value.get("status") or "").strip().lower()
        if status in {"success", "ok", "done"}:
            status = "ready"
        if status not in {"ready", "needs_clarification"}:
            raise HarnessError("模型返回了未知插件编辑状态")
        reply = str(value.get("reply") or "").strip()
        plugin = value.get("plugin")
        if status == "ready":
            _validate_draft(plugin)
            if not reply:
                reply = "插件草案已生成。"
        elif plugin is not None and not isinstance(plugin, dict):
            raise HarnessError("模型的插件草案格式无效")
        return DraftResult(status=status, reply=reply, plugin=plugin if isinstance(plugin, dict) else None, raw=raw)

    def materialize(self, result: DraftResult, target: Path) -> Path:
        if not result.ready or not result.plugin:
            raise HarnessError("当前没有可以写入的插件草案")
        plugin = result.plugin
        _validate_draft(plugin)
        target = Path(target).resolve()
        if target.exists():
            if not target.is_dir() or any(target.iterdir()):
                raise HarnessError(f"插件草案目录已存在且非空：{target}")
        target.mkdir(parents=True, exist_ok=True)
        files = _draft_files(plugin)
        for relative, content in files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        manifest_dir = target / ".aipet-plugin"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": 1,
            "name": plugin["name"],
            "version": plugin["version"],
            "display_name": plugin["display_name"].strip(),
            "entrypoint": plugin["entrypoint"],
            "files": [".aipet-plugin/plugin.json", *sorted(files)],
            "test_command": ["{python}", "-I", "-B", "{entrypoint}", "--selftest"],
            "health_command": ["{python}", "-I", "-B", "{entrypoint}", "--health"],
        }
        (manifest_dir / "plugin.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        validate_plugin(target)
        return target

    def apply(self, result: DraftResult, target: Path, app_root: Path) -> dict[str, Any]:
        source = self.materialize(result, target)
        result_data = auto_use(source, Path(app_root))
        result_data["source"] = str(source)
        return result_data


def _validate_draft(plugin: Any) -> None:
    if not isinstance(plugin, dict):
        raise HarnessError("模型没有返回插件草案")
    name = plugin.get("name")
    version = plugin.get("version")
    display_name = plugin.get("display_name")
    entrypoint = plugin.get("entrypoint")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise HarnessError("模型生成的插件名称不安全")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise HarnessError("模型生成的插件版本无效")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 120:
        raise HarnessError("模型生成的显示名称无效")
    entrypoint = _safe_relative(entrypoint)
    if not entrypoint.endswith(".py"):
        raise HarnessError("模型生成的入口必须是 Python 文件")
    files = plugin.get("files")
    if not isinstance(files, list) or not files:
        raise HarnessError("模型生成的文件列表为空")
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            raise HarnessError("模型生成的文件项无效")
        relative = _safe_relative(item["path"])
        if relative in {".aipet-plugin/plugin.json", "release-manifest.json"}:
            raise HarnessError("模型不能生成宿主保留文件")
        if relative in seen:
            raise HarnessError("模型生成了重复文件")
        seen.add(relative)
        size = len(item["content"].encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise HarnessError(f"模型生成的文件过大：{relative}")
        if SECRET_SHAPE_RE.search(item["content"]):
            raise HarnessError(f"模型生成的文件疑似包含凭据：{relative}")
        total += size
        if total > MAX_DRAFT_BYTES:
            raise HarnessError("模型生成的插件草案过大")
    if entrypoint not in seen:
        raise HarnessError("模型生成的文件列表缺少入口")


def _draft_files(plugin: dict[str, Any]) -> dict[str, str]:
    result = {item["path"].replace("\\", "/"): item["content"] for item in plugin["files"]}
    if "README.md" not in result:
        result["README.md"] = (
            f"# {plugin['display_name'].strip()}\n\n"
            "这是通过 AIPet 对话引导编辑器生成的独立插件。\n\n"
            "入口支持 `--selftest` 和 `--health`，由 AIPet harness 管理生命周期。\n"
        )
    return result


def default_target(root: Path, name: str) -> Path:
    return Path(root).resolve() / "work" / "harness-guided" / uuid.uuid4().hex[:12] / name


def client_from_environment(*, base_url: str | None = None, model: str | None = None) -> OpenAICompatClient:
    # Keep the editor's provider boundary separate from AIPet's normal brain
    # credentials.  A guide invocation must opt into its own environment vars.
    key = os.environ.get("AIPET_HARNESS_API_KEY") or ""
    base = base_url or os.environ.get("AIPET_HARNESS_BASE_URL") or ""
    selected_model = model or os.environ.get("AIPET_HARNESS_MODEL") or "qwen"
    return OpenAICompatClient(base, key, selected_model)


def summarize(result: DraftResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": result.status, "reply": result.reply}
    if result.plugin:
        payload["plugin"] = {
            "name": result.plugin.get("name"),
            "version": result.plugin.get("version"),
            "display_name": result.plugin.get("display_name"),
            "entrypoint": result.plugin.get("entrypoint"),
            "files": [item.get("path") for item in result.plugin.get("files", []) if isinstance(item, dict)],
        }
    return payload
