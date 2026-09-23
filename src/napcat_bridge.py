#!/usr/bin/env python3
"""NapCat / OneBot 11 → 小日和桥接。

NapCat 负责登录 QQ 并提供反向 WebSocket；这里把 OneBot 事件转换成
现有 qq_bridge.QQEvent，回复仍然走同一套人格、记忆和本地工具。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402
import qq_bot as QB  # noqa: E402
import qq_bridge as BR  # noqa: E402


def config() -> dict:
    cfg = M.CFG.get("tools", {}) or {}
    return cfg.get("napcat", {}) or {}


def _text_and_attachments(message) -> tuple[str, list[dict], bool]:
    """把 OneBot message 段转成正文、图片附件和是否 @ 本机。"""
    if isinstance(message, str):
        raw = message
        return raw, [], bool(re.search(r"\[CQ:at,qq=\d+\]", raw))
    if not isinstance(message, list):
        return str(message or ""), [], False
    chunks: list[str] = []
    attachments: list[dict] = []
    mentioned = False
    self_id = str(config().get("self_id", "")).strip()
    for seg in message:
        if not isinstance(seg, dict):
            continue
        typ = seg.get("type", "")
        data = seg.get("data") or {}
        if typ == "text":
            chunks.append(str(data.get("text") or ""))
        elif typ == "at":
            qq = str(data.get("qq") or "")
            if qq == "all":
                chunks.append("@全体成员")
            else:
                chunks.append(f"@{qq}")
                mentioned = mentioned or (bool(self_id) and qq == self_id)
        elif typ == "image":
            url = str(data.get("url") or data.get("file") or "")
            chunks.append("[图片]")
            if url:
                attachments.append({"content_type": "image/*", "filename": "image.jpg", "url": url})
        elif typ == "video":
            url = str(data.get("url") or data.get("file") or "")
            chunks.append("[视频]")
            if url:
                attachments.append({"content_type": "video/*", "filename": "video.mp4", "url": url})
        elif typ == "record":
            chunks.append("[语音]")
        elif typ == "file":
            chunks.append(f"[文件：{data.get('name') or data.get('file') or '未命名'}]")
    return "".join(chunks).strip(), attachments, mentioned


def event_from_onebot(payload: dict) -> QB.QQEvent | None:
    if payload.get("post_type") != "message":
        return None
    msg_type = payload.get("message_type")
    if msg_type not in ("group", "private"):
        return None
    text, attachments, mentioned = _text_and_attachments(payload.get("message"))
    self_id = str(config().get("self_id", "")).strip()
    raw = str(payload.get("raw_message") or "")
    if msg_type == "group":
        if not mentioned and (self_id and f"[CQ:at,qq={self_id}]" not in raw):
            return None
        sender = payload.get("sender") or {}
        ts = payload.get("time")
        when = datetime.fromtimestamp(float(ts)) if ts else M.now()
        return QB.QQEvent(
            kind="group_at", msg_id=str(payload.get("message_id") or ""),
            content=text, ts=when, scene="group",
            group_openid=str(payload.get("group_id") or ""),
            member_openid=str(sender.get("user_id") or ""),
            username=str(sender.get("card") or sender.get("nickname") or ""),
            member_role=str(sender.get("role") or "member"),
            attachments=attachments,
        )
    sender = payload.get("sender") or {}
    ts = payload.get("time")
    when = datetime.fromtimestamp(float(ts)) if ts else M.now()
    return QB.QQEvent(
        kind="c2c", msg_id=str(payload.get("message_id") or ""),
        content=text, ts=when, scene="c2c",
        user_openid=str(sender.get("user_id") or payload.get("user_id") or ""),
        username=str(sender.get("nickname") or ""), attachments=attachments,
    )


class NapCatGateway:
    def __init__(self, on_event):
        self.on_event = on_event
        self.ws = None
        self.app = None

    def start(self) -> None:
        from PySide6.QtCore import QUrl, QObject, Signal
        from PySide6.QtNetwork import QNetworkRequest
        from PySide6.QtWebSockets import QWebSocket

        self.ws = QWebSocket()
        self.ws.textMessageReceived.connect(self._received)
        self.ws.connected.connect(lambda: print("[napcat] WebSocket 已连接", flush=True))
        self.ws.disconnected.connect(lambda: print("[napcat] WebSocket 已断开", flush=True))
        self.ws.errorOccurred.connect(lambda e: print(f"[napcat] WebSocket 错误：{e}", flush=True))
        token = str(config().get("access_token") or "").strip()
        req = QNetworkRequest(QUrl(str(config().get("ws_url") or "ws://127.0.0.1:8080/onebot/v11/ws")))
        if token:
            req.setRawHeader(b"Authorization", f"Bearer {token}".encode())
        self.ws.open(req)

    def _received(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if payload.get("post_type") == "meta_event":
            return
        ev = event_from_onebot(payload)
        if ev is not None:
            self.on_event(ev)

    def send_action(self, action: str, params: dict) -> None:
        if not self.ws:
            return
        body = json.dumps({"action": action, "params": params}, ensure_ascii=False)
        self.ws.sendTextMessage(body)


class NapCatBridge(BR.Bridge):
    def __init__(self, gateway: NapCatGateway, reply_enabled: bool = True):
        super().__init__(reply_enabled=reply_enabled)
        self.gateway = gateway

    def _send(self, ev: QB.QQEvent, text: str) -> None:
        clean, notes = BR.QT.sanitize(text)
        if not clean:
            BR.log("清洗后没内容了，不发")
            return
        if notes:
            BR.log(f"出站清洗：{'、'.join(notes)}")
        if ev.scene == "group":
            self.gateway.send_action("send_group_msg", {
                "group_id": int(ev.group_openid), "message": clean})
        else:
            self.gateway.send_action("send_private_msg", {
                "user_id": int(ev.user_openid), "message": clean})
        BR.log("已通过 NapCat 回复")


def run(reply_enabled: bool = True) -> int:
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication(sys.argv)
    if reply_enabled and not __import__("brain").api_key():
        print("没配 DeepSeek key，无法回复", flush=True)
        return 1
    gateway = NapCatGateway(lambda ev: bridge.handle(ev))
    bridge = NapCatBridge(gateway, reply_enabled=reply_enabled)
    gateway.start()
    print("[napcat] 已启动，只处理 @ 机器人的群消息；Ctrl+C 停止。", flush=True)
    return app.exec()


def selftest() -> int:
    old = config().get("self_id")
    config()["self_id"] = "10001"
    try:
        p = {"post_type": "message", "message_type": "group", "group_id": 7,
             "message_id": 1, "raw_message": "[CQ:at,qq=10001] 看视频",
             "message": [{"type": "at", "data": {"qq": "10001"}},
                         {"type": "text", "data": {"text": " 看视频"}}],
             "sender": {"user_id": 8, "nickname": "测试", "role": "member"}}
        ev = event_from_onebot(p)
        ok = ev is not None and ev.kind == "group_at" and ev.group_openid == "7" and "看视频" in ev.content
        print(f"NapCat 适配自检：{'通过' if ok else '失败'}")
        return 0 if ok else 1
    finally:
        if old is not None:
            config()["self_id"] = old


if __name__ == "__main__":
    if "selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(run("--debug" not in sys.argv))
