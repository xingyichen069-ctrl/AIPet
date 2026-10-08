"""Opt-in daily group cards. Data-only templates; one bounded background worker.

Claim a slot durably before sending: an interrupted/ambiguous send is never
retried. This chooses an occasional missed card over duplicate group messages.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time

import atomic_store as STORE
import qq_text

CST = timezone(timedelta(hours=8), "Asia/Shanghai")
MAX_GROUPS = 32
MAX_TASKS = 8
GRACE_SECONDS = 120
_ACTION_LOCK = threading.RLock()
ID = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
GROUP = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SLOT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\Z")


class ScheduleError(ValueError):
    pass


def _text(path: Path, limit: int) -> str:
    if path.is_symlink():
        raise ScheduleError("任务文件不能是符号链接")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ScheduleError("任务文件超过大小限制")
    return raw.decode("utf-8-sig")


def initialize(root: Path) -> None:
    """Install editable defaults once; updates never overwrite private copies."""
    root = Path(root)
    target = root / "data/qq_tasks"
    if target.is_symlink():
        raise ScheduleError("任务目录不能是符号链接")
    with STORE.state_lock(root):
        target.mkdir(parents=True, exist_ok=True)
        source = root / "templates/qq_tasks"
        for name in ("spellcards.json", "spellcards.md"):
            dest = target / name
            if not dest.exists():
                raw = _text(source / name, 128 * 1024)
                STORE.write_bytes(dest, raw.encode("utf-8"))


def templates(root: Path) -> dict:
    directory = Path(root) / "data/qq_tasks"
    if not directory.exists():
        initialize(root)
    if directory.is_symlink():
        raise ScheduleError("任务目录不能是符号链接")
    paths = sorted(directory.glob("*.json"))
    if len(paths) > MAX_TASKS:
        raise ScheduleError("任务模板过多")
    result = {}
    for path in paths:
        cfg = json.loads(_text(path, 16 * 1024))
        if not isinstance(cfg, dict):
            raise ScheduleError("任务模板必须是对象")
        task = cfg.get("id", "")
        if not isinstance(task, str) or not ID.fullmatch(task) or path.stem != task:
            raise ScheduleError("任务名称与文件名不匹配")
        if cfg.get("schema") != 1 or cfg.get("kind") != "cards" or cfg.get("timezone") != "Asia/Shanghai":
            raise ScheduleError("只支持北京时间的每日卡片模板")
        title, catalog, times = cfg.get("title"), cfg.get("catalog"), cfg.get("times")
        if not isinstance(title, str) or not 1 <= len(title) <= 24:
            raise ScheduleError("任务标题无效")
        if not isinstance(catalog, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}\.md", catalog):
            raise ScheduleError("卡片文档必须位于任务目录")
        if not isinstance(times, list) or not 1 <= len(times) <= 6 or len(set(map(str, times))) != len(times):
            raise ScheduleError("每日时段应为 1 至 6 个不重复时间")
        for value in times:
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise ScheduleError("时段必须写为 HH:MM")
        result[task] = {**cfg, "times": sorted(times)}
    return result


def cards(root: Path, cfg: dict) -> list[dict]:
    rows = []
    for line in _text(Path(root) / "data/qq_tasks" / cfg["catalog"], 128 * 1024).splitlines():
        if not line.startswith("|"):
            continue
        cells = [s.strip() for s in line.strip().strip("|").split("|")]
        if cells == ["符卡", "持有者", "解说"] or all(re.fullmatch(r"[-: ]+", s) for s in cells):
            continue
        if len(cells) != 3:
            raise ScheduleError("每张卡需要符卡、持有者、解说三列")
        name, holder, intro = cells
        lines = intro.split("<br>")
        if (not 1 <= len(name) <= 40 or not 1 <= len(holder) <= 32
                or not 1 <= len(lines) <= 2 or any(not 1 <= len(s) <= 60 for s in lines)
                or any(ord(c) < 32 for c in "".join(cells))):
            raise ScheduleError("卡片内容过长或含无效字符")
        row = {"name": name, "holder": holder, "intro": "\n".join(lines)}
        rendered = render(row)
        clean, notes = qq_text.sanitize(rendered)
        if notes or clean != rendered or len(rendered.encode("utf-8")) > qq_text.QQ_SAFE_BYTES:
            raise ScheduleError("卡片内容不适合 QQ 纯文本发送")
        rows.append(row)
        if len(rows) > 300:
            raise ScheduleError("卡片文档最多 300 张")
    if not rows:
        raise ScheduleError("卡片文档里没有可用条目")
    return rows


def render(card: dict) -> str:
    # A standalone ASCII hyphen becomes a Markdown bullet in QQ's sanitizer.
    # Use vertical punctuation while keeping the original catalog name intact.
    title = card["name"].translate(str.maketrans({"「": "﹁", "」": "﹂", "『": "﹃", "』": "﹄", "-": "︱"}))
    vertical = "\n".join(c for c in title if not c.isspace())
    return f"{card['intro'].rstrip('：:')}：\n{vertical}\n持有者：{card['holder']}"


def _state(root: Path) -> dict:
    path = Path(root) / "data/qq_schedules.json"
    if not path.exists():
        return {"schema": 1, "groups": {}}
    data = json.loads(_text(path, 2 * 1024 * 1024))
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("groups"), dict):
        raise ScheduleError("定时状态损坏，已停止发送，原文件保留")
    if len(data["groups"]) > MAX_GROUPS:
        raise ScheduleError("定时群数量超过限制")
    for group, tasks in data["groups"].items():
        if not GROUP.fullmatch(group) or not isinstance(tasks, dict) or len(tasks) > MAX_TASKS:
            raise ScheduleError("定时群设置无效")
        for task, value in tasks.items():
            if not ID.fullmatch(task) or not isinstance(value, dict) or type(value.get("enabled")) is not bool:
                raise ScheduleError("定时任务状态无效")
            if type(value.get("cursor")) is not int or not 0 <= value["cursor"] <= 1_000_000_000:
                raise ScheduleError("定时轮换进度无效")
            enabled_at = value.get("enabled_at")
            if type(enabled_at) not in (int, float) or not math.isfinite(enabled_at) or enabled_at < 0:
                raise ScheduleError("定时启用时间无效")
            water = value.get("high_water", "")
            if not isinstance(water, str) or (water and not SLOT.fullmatch(water)):
                raise ScheduleError("定时时段记录无效")
            if not isinstance(value.get("recent", []), list) or len(value.get("recent", [])) > 8:
                raise ScheduleError("定时记录超过限制")
    return data


def _save(root: Path, state: dict) -> None:
    STORE.write_json(Path(root) / "data/qq_schedules.json", state)


def _new() -> dict:
    return {"enabled": False, "enabled_at": 0, "cursor": 0, "high_water": "", "recent": []}


def _status(cfg: dict, value: dict) -> str:
    state = "已开启" if value.get("enabled") else "未开启"
    if value.get("blocked"):
        state = "已暂停：QQ 拒绝主动消息，请检查群权限或禁言后重新开启"
    text = f"本群{cfg['title']}：{state}。\n北京时间 {' / '.join(cfg['times'])}，每次 1 张，按文档顺序轮换。"
    recent = value.get("recent") or []
    if recent:
        last = recent[-1]
        labels = {"sending": "发送中或结果未知", "sent": "QQ 已受理", "failed": "发送失败", "unknown": "结果未知，未重发"}
        text += f"\n最近时段：{last.get('slot', '')}，{labels.get(last.get('status'), '未确认')}"
        if last.get("code"):
            text += f"（QQ 错误码 {last['code']}）"
    return text


def command(root: Path, text: str, *, scene: str, group: str, is_owner: bool, now=None) -> str | None:
    words = text.strip().lstrip("/／").split()
    if not words or words[0] not in {"符卡", "定时任务"}:
        return None
    if not is_owner:
        return "定时任务仅主人可管理。"
    if scene != "group" or not GROUP.fullmatch(group):
        return "请在需要推送的群里 @我，发送 /符卡 开启、关闭、状态或预览。"
    task = "spellcards"
    action = words[1] if len(words) > 1 else "状态"
    if words[0] == "定时任务" and len(words) > 2:
        task = "spellcards" if words[2] == "符卡" else words[2]
    if len(words) > (2 if words[0] == "符卡" else 3):
        return "用法：/符卡 开启、关闭、状态、预览；/定时任务 列表。"
    try:
        with _ACTION_LOCK:
            if action == "关闭" and ID.fullmatch(task):
                # A broken/removed template must never prevent disabling a job.
                with STORE.state_lock(root):
                    data = _state(root)
                    value = data["groups"].get(group, {}).get(task)
                    if value is not None:
                        value.update(enabled=False, blocked=False)
                        _save(root, data)
                return "本群任务已关闭；后续时段不再发送，其他群不受影响。"
            cfgs = templates(root)
            if words[0] == "定时任务" and action == "列表":
                return "可选定时任务：\n" + "\n".join(f"{k}（{v['title']}）" for k, v in cfgs.items()) + "\n用法：/定时任务 开启 任务名"
            cfg = cfgs.get(task)
            if cfg is None:
                return "找不到这个任务；用 /定时任务 列表 查看。"
            with STORE.state_lock(root):
                data = _state(root)
                value = data["groups"].get(group, {}).get(task, _new())
                if action in {"状态", "查询", "查看"}:
                    return _status(cfg, value)
                if action == "预览":
                    deck = cards(root, cfg)
                    return render(deck[value["cursor"] % len(deck)])
                if action not in {"开启", "关闭"}:
                    return "用法：/符卡 开启、关闭、状态、预览。"
                if action == "开启":
                    cards(root, cfg)  # Validate before accepting an enabled schedule.
                    if not value["enabled"] or value.get("blocked"):
                        value["enabled_at"] = time.time() if now is None else now
                    value.update(enabled=True, blocked=False)
                else:
                    value.update(enabled=False, blocked=False)
                if group not in data["groups"] and len(data["groups"]) >= MAX_GROUPS:
                    raise ScheduleError("已达到定时群数量上限")
                existing = data["groups"].get(group, {})
                if task not in existing and len(existing) >= MAX_TASKS:
                    raise ScheduleError("本群任务数量已达上限")
                data["groups"].setdefault(group, {})[task] = value
                _save(root, data)
                suffix = ("\n从下一个时段开始；需 QQ 允许主动发送，失败可用 /符卡 状态 查看。"
                          if value["enabled"] else "\n后续时段不再发送，其他群不受影响。")
                return _status(cfg, value) + suffix
    except (OSError, ValueError, TypeError, KeyError):
        return "定时任务文件暂不可用，请检查任务文档或状态文件；未重置任何记录。"


def gateway_ready(root: Path) -> bool:
    try:
        status = json.loads(_text(Path(root) / "data/qq_status.json", 32 * 1024))
        age = time.time() - datetime.fromisoformat(status["at"]).timestamp()
        return (status.get("pid") == os.getpid() and status.get("state") == "ready"
                and status.get("online") is True and 0 <= age < 180)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run_due(root: Path, send, *, now=None, stop=None, wait=None, ready=None) -> int:
    """Run current slots only. Injected clock/sender make offline checks possible."""
    root = Path(root)
    stop = stop or (lambda: False)
    wait = wait or time.sleep
    clock = (lambda: time.time()) if now is None else (lambda: now)
    with STORE.state_lock(root):
        groups = list(_state(root)["groups"])
    if not groups:
        return 0
    cfgs = templates(root)
    sent = 0
    for group in groups:
        for task, cfg in cfgs.items():
            if stop() or (ready is not None and not ready()):
                return sent
            # Commands wait only for an already-started send, never a global
            # storage lock held across a network request.
            with _ACTION_LOCK:
                at = clock()
                date = datetime.fromtimestamp(at, CST)
                slots = [(date.replace(hour=int(t[:2]), minute=int(t[3:]), second=0, microsecond=0), t) for t in cfg["times"]]
                current = [dt for dt, _ in slots if 0 <= at - dt.timestamp() < GRACE_SECONDS]
                if not current:
                    continue
                slot = max(current).strftime("%Y-%m-%dT%H:%M")
                due_at = max(current).timestamp()
                with STORE.state_lock(root):
                    data = _state(root)
                    value = data["groups"].get(group, {}).get(task)
                    if (not value or not value["enabled"] or value.get("blocked")
                            or value["enabled_at"] > due_at or value["high_water"] >= slot):
                        continue
                    deck = cards(root, cfg)
                    card = deck[value["cursor"] % len(deck)]
                    value["cursor"] += 1
                    value["high_water"] = slot
                    record = {"slot": slot, "status": "sending", "card": card["name"]}
                    value["recent"] = (value.get("recent", []) + [record])[-8:]
                    _save(root, data)  # Crash from here onward must not repeat.
                if stop():
                    return sent
                code = 0
                try:
                    result = send(group, render(card))
                    code = result.get("code", 0) if isinstance(result, dict) else 0
                    if isinstance(code, str) and code.isdecimal() and len(code) <= 10:
                        code = int(code)
                    code = code if type(code) is int else 0
                    success = (isinstance(result, dict) and bool(result.get("id"))
                               and not code and not any(result.get(k) for k in ("_http_error", "_error", "_skipped")))
                    outcome = "sent" if success else "failed" if isinstance(result, dict) and (result.get("_http_error") or code) else "unknown"
                except Exception:
                    outcome = "unknown"
                with STORE.state_lock(root):
                    data = _state(root)
                    value = data["groups"][group][task]
                    for record in value["recent"]:
                        if record.get("slot") == slot:
                            record.update(status=outcome, code=code)
                    if code in {40034101, 40034105, 40054002}:
                        value["blocked"] = True
                    _save(root, data)
                sent += 1
            # <= 20 attempts/minute, independent of the number of enabled groups.
            if sent >= 8 or wait(3.1):
                return sent
    return sent


class Worker:
    def __init__(self, root: Path, send, log=lambda text: None):
        self.root, self.send, self.log = Path(root), send, log
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is not None:
            return
        initialize(self.root)
        self.thread = threading.Thread(target=self._loop, daemon=True, name="qq-schedules")
        self.thread.start()

    def stop(self):
        self.stopping.set()

    def join(self, timeout=15):
        if self.thread is not None:
            self.thread.join(timeout)

    def _loop(self):
        # The gateway's instance lock owns this worker; durable claims also
        # prevent repeated sends if a second process ever reaches this code.
        last_error = None
        while not self.stopping.is_set():
            try:
                if gateway_ready(self.root):
                    run_due(self.root, self.send, stop=self.stopping.is_set,
                            wait=self.stopping.wait, ready=lambda: gateway_ready(self.root))
                last_error = None
            except Exception as error:
                name = type(error).__name__
                if name != last_error:
                    self.log("定时任务暂停本轮：" + name)
                last_error = name
            self.stopping.wait(10)
