"""Bounded Markdown attachments through QQ's official multipart upload API.

Uploading never sends a message. The bridge separately performs the passive
reply, preserving its delivery ledger and avoiding retries of uncertain sends.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
import urllib.request

MAX_BYTES = 200_000
MAX_PARTS = 16
MAX_CACHE = 32
HELP_PATH = Path("docs/QQ使用帮助.md")
_cache: OrderedDict = OrderedDict()
_lock = threading.Lock()


class DocumentError(RuntimeError):
    """A safe-to-log upload failure, without signed URLs or file handles."""


@dataclass(frozen=True)
class MarkdownDocument:
    name: str
    text: str

    def data(self) -> bytes:
        if (not self.name.lower().endswith(".md") or len(self.name.encode("utf-8")) > 120
                or any(c in self.name for c in '/\\\r\n\x00') or self.name.startswith(".")):
            raise DocumentError("文档文件名无效")
        raw = self.text.encode("utf-8")
        if not raw or len(raw) > MAX_BYTES or b"\x00" in raw:
            raise DocumentError("Markdown 文档为空或超过 200 KB")
        return raw


def help_document(root: Path) -> MarkdownDocument:
    source = root / HELP_PATH
    if not source.is_file():
        source = root / "templates/qq_help.md"
    with source.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    doc = MarkdownDocument("AIPet-QQ指令手册.md", raw.decode("utf-8-sig"))
    doc.data()
    return doc


def _timeout(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise DocumentError("文档上传已超时")
    return min(12.0, left)


def _checked(api, path: str, body: dict, deadline: float) -> dict:
    try:
        result = api("POST", path, body, timeout=_timeout(deadline))
    except Exception:
        raise DocumentError("QQ 文档上传连接失败") from None
    if (not isinstance(result, dict) or result.get("_http_error")
            or result.get("_error") or "_raw" in result
            or result.get("code") not in (None, 0)):
        code = result.get("code", result.get("_http_error", "unknown")) if isinstance(result, dict) else "invalid"
        safe_code = str(code) if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", str(code)) else "unknown"
        raise DocumentError("QQ 文档上传被拒绝，错误码 " + safe_code)
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _put(url: str, data: bytes, timeout: float) -> None:
    try:
        address = urlsplit(url)
        host = address.hostname or ""
        valid = (address.scheme == "https" and address.port in (None, 443)
                 and not address.username and not address.password and not address.fragment
                 and host.endswith(".myqcloud.com"))
    except ValueError:
        valid = False
    if not valid:
        raise DocumentError("QQ 返回了无效的文档上传地址")
    # Never forward the QQ API credential, follow redirects or use a local VPN.
    req = urllib.request.Request(url, data=data, method="PUT",
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(req, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise DocumentError("文档分片上传失败")
            response.read(1024)
    except Exception:
        raise DocumentError("文档分片上传失败") from None


def upload(document: MarkdownDocument, scene: str, destination: str, api,
           *, deadline: float | None = None) -> str:
    """Return a scoped file_info; never call /messages or srv_send_msg=true."""
    raw = document.data()
    if scene not in ("group", "c2c") or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", destination):
        raise DocumentError("文档目标会话无效")
    deadline = deadline if deadline is not None else time.monotonic() + 45
    _timeout(deadline)
    key = (id(api), scene, destination, document.name, hashlib.sha256(raw).hexdigest())
    with _lock:
        now = time.monotonic()
        for stale in [k for k, (_, expires) in _cache.items() if expires <= now]:
            del _cache[stale]
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key][0]
    base = "/v2/" + ("groups/" if scene == "group" else "users/") + destination
    prepared = _checked(api, base + "/upload_prepare", {
        "file_type": 4, "file_size": str(len(raw)), "file_name": document.name,
        "md5": hashlib.md5(raw).hexdigest(), "sha1": hashlib.sha1(raw).hexdigest(),
        "md5_10m": hashlib.md5(raw[:10_002_432]).hexdigest(),
    }, deadline)
    upload_id, parts = prepared.get("upload_id"), prepared.get("parts")
    try:
        block = int(prepared["block_size"])
        count = (len(raw) + block - 1) // block if block > 0 else 0
        valid = (isinstance(upload_id, str) and 0 < len(upload_id) <= 4096
                 and isinstance(parts, list) and 1 <= count <= MAX_PARTS and len(parts) == count)
        if not valid:
            raise ValueError
        parts = sorted(parts, key=lambda part: part["index"])
        indexes = [part["index"] for part in parts]
        # Official docs say zero-based; live responses also use one-based IDs.
        # Use the returned IDs when acknowledging, never calculate our own.
        if (any(type(index) is not int for index in indexes) or indexes[0] not in (0, 1)
                or indexes != list(range(indexes[0], indexes[0] + count))):
            raise ValueError
        for offset, part in enumerate(parts):
            chunk = raw[offset * block:(offset + 1) * block]
            if int(part["block_size"]) != len(chunk) or not isinstance(part["presigned_url"], str):
                raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise DocumentError("QQ 文档分片信息无效") from None
    for offset, part in enumerate(parts):
        chunk = raw[offset * block:(offset + 1) * block]
        _put(part["presigned_url"], chunk, _timeout(deadline))
        _checked(api, base + "/upload_part_finish", {
            "upload_id": upload_id, "part_index": part["index"],
            "block_size": str(len(chunk)), "md5": hashlib.md5(chunk).hexdigest(),
        }, deadline)
    merged = _checked(api, base + "/files", {
        "file_type": 4, "file_name": document.name,
        "upload_id": upload_id, "srv_send_msg": False,
    }, deadline)
    info = merged.get("file_info")
    if not isinstance(info, str) or not 0 < len(info) <= 64_000:
        raise DocumentError("QQ 未返回可发送的文档信息")
    try:
        ttl = int(merged.get("ttl", 300))
        if ttl < 0:
            raise ValueError
    except (ValueError, TypeError):
        raise DocumentError("QQ 文档有效期无效") from None
    duration = 3600 if ttl == 0 else max(0, min(3600, ttl - 15))
    if duration:
        with _lock:
            _cache[key] = (info, time.monotonic() + duration)
            _cache.move_to_end(key)
            while len(_cache) > MAX_CACHE:
                _cache.popitem(last=False)
    return info
