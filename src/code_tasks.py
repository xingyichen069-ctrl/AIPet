#!/usr/bin/env python3
"""
后台代码任务。

QQ 普通对话必须在被动回复窗口内结束；写程序、跑脚本、制图或整理文件
则可能需要几十秒甚至更久。这个模块把这两条路径分开：主人明确提出产物
任务后，给模型一个独立工作目录，模型可以用 fs_*、web_search 和 run_python
反复检查结果，最后再用 QQ 主动消息回传。

这里的目录隔离是 AIPet 工具层的边界，不是操作系统安全沙箱。第一阶段只
允许主人调用，后续仍应把不可信代码迁移到真正的受限运行环境。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import memory as M


TASKS_ROOT = M.ROOT / "data" / "code_tasks"
INDEX_FILE = TASKS_ROOT / "index.json"
MAX_PERSISTED_TASKS = 80
MAX_ARTIFACTS = 24
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_MEDIA_SEND = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _redact(text: str, limit: int = 100) -> str:
    """用于状态索引的短标题，避免把凭据样式的文本落盘。"""
    s = re.sub(r"(?i)(?:sk|key|token|secret)[-_ ]?[A-Za-z0-9]{8,}", "[已隐藏]", text or "")
    s = re.sub(r"(?i)bearer\s+[A-Za-z0-9._-]+", "Bearer [已隐藏]", s)
    return " ".join(s.split())[:limit]


def _conversation_key(ctx: dict) -> str:
    key = str(ctx.get("conversation_key") or "").strip()
    if key:
        return key
    ev = ctx.get("event")
    if ev is None:
        return ""
    scene = str(getattr(ev, "scene", "") or "")
    ident = (getattr(ev, "group_openid", "") if scene == "group"
             else getattr(ev, "user_openid", ""))
    return f"{scene}:{ident}" if ident else ""


def _actor_id(ctx: dict) -> str:
    return str(ctx.get("actor_id") or "").strip()


def _event_snapshot(ev: Any) -> dict:
    if ev is None:
        return {}
    return {
        "scene": str(getattr(ev, "scene", "") or ""),
        "group_openid": str(getattr(ev, "group_openid", "") or ""),
        "user_openid": str(getattr(ev, "user_openid", "") or ""),
        "member_openid": str(getattr(ev, "member_openid", "") or ""),
        "speaker_id": str(getattr(ev, "speaker_id", "") or ""),
    }


@dataclass
class CodeTask:
    task_id: str
    conversation_key: str
    actor_id: str
    actor_name: str
    event: Any = None
    root: Path = field(default_factory=lambda: TASKS_ROOT)
    state: str = "queued"
    title: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    artifacts: list[str] = field(default_factory=list)
    last_reply: str = ""
    pending: deque[str] = field(default_factory=deque, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    worker_started: bool = field(default=False, repr=False)
    sent_artifacts: set[str] = field(default_factory=set, repr=False)

    def public(self) -> dict:
        return {
            "task_id": self.task_id,
            "conversation_key": self.conversation_key,
            "actor_id": self.actor_id,
            "actor_name": self.actor_name,
            "state": self.state,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "artifacts": list(self.artifacts),
            "event": _event_snapshot(self.event),
        }


class TaskManager:
    """进程内任务注册表；每个任务的模型调用运行在自己的 daemon 线程。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, CodeTask] = {}
        self._load()

    # ── 索引 ────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            raw = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("task_id") or "")
            if not re.fullmatch(r"ct-[a-f0-9]{10}", task_id):
                continue
            root = TASKS_ROOT / task_id
            state = str(item.get("state") or "interrupted")
            if state in {"queued", "running"}:
                state = "interrupted"
            task = CodeTask(
                task_id=task_id,
                conversation_key=str(item.get("conversation_key") or ""),
                actor_id=str(item.get("actor_id") or ""),
                actor_name=str(item.get("actor_name") or ""),
                root=root,
                state=state,
                title=str(item.get("title") or ""),
                created_at=str(item.get("created_at") or _now()),
                updated_at=str(item.get("updated_at") or _now()),
                artifacts=[str(x) for x in item.get("artifacts") or []][:MAX_ARTIFACTS],
            )
            self._tasks[task_id] = task

    def _persist(self) -> None:
        with self._lock:
            items = sorted(self._tasks.values(),
                           key=lambda t: t.updated_at, reverse=True)[:MAX_PERSISTED_TASKS]
            body = [t.public() for t in items]
        try:
            TASKS_ROOT.mkdir(parents=True, exist_ok=True)
            tmp = INDEX_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(INDEX_FILE)
        except OSError:
            # 任务本身仍可继续；状态索引只是恢复和查询的辅助。
            pass

    # ── 查询与权限 ──────────────────────────────────────────
    def _owned(self, task: CodeTask, ctx: dict) -> bool:
        return bool(ctx.get("is_owner")) and bool(_actor_id(ctx)) \
            and task.actor_id == _actor_id(ctx) \
            and task.conversation_key == _conversation_key(ctx)

    def _find(self, ctx: dict, task_id: str = "") -> CodeTask | None:
        if task_id:
            task = self._tasks.get(task_id.strip())
            return task if task and self._owned(task, ctx) else None
        candidates = [t for t in self._tasks.values() if self._owned(t, ctx)]
        return max(candidates, key=lambda t: t.updated_at, default=None)

    def _new_id(self) -> str:
        return "ct-" + uuid.uuid4().hex[:10]

    def _root_for(self, task_id: str) -> Path:
        return TASKS_ROOT / task_id

    # ── 对外操作 ────────────────────────────────────────────
    def submit(self, ctx: dict, instruction: str) -> str:
        if not ctx.get("is_owner"):
            return "只有已认证的主人可以启动本地代码任务。"
        key, actor = _conversation_key(ctx), _actor_id(ctx)
        if not key or not actor:
            return "当前消息没有完整的 QQ 会话身份，暂时不能启动代码任务。"
        text = str(instruction or "").strip()
        if not text:
            return "请把要实现的功能说完整，我才能启动代码任务。"

        task_id = self._new_id()
        task = CodeTask(task_id=task_id, conversation_key=key,
                        actor_id=actor, actor_name=str(ctx.get("actor_name") or "主人"),
                        event=ctx.get("event"), root=self._root_for(task_id),
                        title=_redact(text))
        task.pending.append(text)
        with self._lock:
            self._tasks[task_id] = task
        self._persist()
        self._start_worker(task)
        return (f"已接下本地代码任务 {task_id}，我会在独立目录里执行并检查结果。"
                "完成后会主动回传；需要你决定或补充密钥时会在这里问。")

    def continue_task(self, ctx: dict, instruction: str, task_id: str = "") -> str:
        if not ctx.get("is_owner"):
            return "只有已认证的主人可以继续本地代码任务。"
        text = str(instruction or "").strip()
        if not text:
            return "请说明要继续做什么，或直接说「任务状态」。"
        with self._lock:
            task = self._find(ctx, task_id)
            if task is None:
                return "当前 QQ 会话没有找到属于你的本地代码任务。"
            if task.state == "cancelled":
                return f"任务 {task.task_id} 已取消；要重新做请重新启动。"
            if task.event is None:
                task.event = ctx.get("event")
            task.pending.append(text)
            task.updated_at = _now()
        self._persist()
        self._start_worker(task)
        return f"已把后续要求加入 {task.task_id}，会继续使用原来的任务目录。"

    def status(self, ctx: dict, task_id: str = "") -> str:
        with self._lock:
            task = self._find(ctx, task_id)
            if task is None:
                return "当前 QQ 会话没有找到属于你的本地代码任务。"
            return self._status_text(task)

    def cancel(self, ctx: dict, task_id: str = "") -> str:
        with self._lock:
            task = self._find(ctx, task_id)
            if task is None:
                return "当前 QQ 会话没有找到属于你的本地代码任务。"
            task.cancel_event.set()
            task.pending.clear()
            task.state = "cancelled"
            task.updated_at = _now()
            tid = task.task_id
        self._persist()
        return f"已请求取消任务 {tid}。正在运行的那一步结束后会停止。"

    def _status_text(self, task: CodeTask) -> str:
        state = {"queued": "排队中", "running": "执行中", "waiting": "等待补充",
                 "completed": "已完成", "failed": "失败", "cancelled": "已取消",
                 "interrupted": "被中断"}.get(task.state, task.state)
        rows = [f"任务 {task.task_id}：{state}"]
        if task.title:
            rows.append(f"目标：{task.title}")
        if task.artifacts:
            rows.append("产物：" + "、".join(task.artifacts[:12]))
        rows.append(f"目录：data/code_tasks/{task.task_id}")
        return "\n".join(rows)

    # ── 后台执行 ────────────────────────────────────────────
    def _start_worker(self, task: CodeTask) -> None:
        with self._lock:
            if task.worker_started:
                return
            task.worker_started = True
            task.state = "queued"
            task.updated_at = _now()
        threading.Thread(target=self._worker, args=(task,),
                         name=f"aipet-code-{task.task_id}", daemon=True).start()

    def _worker(self, task: CodeTask) -> None:
        try:
            self._send_text(task, f"代码任务 {task.task_id} 已开始，使用独立工作目录。")
            while True:
                cancelled = False
                idle = False
                with self._lock:
                    if task.cancel_event.is_set():
                        task.state = "cancelled"
                        task.worker_started = False
                        task.updated_at = _now()
                        cancelled = True
                    elif not task.pending:
                        task.worker_started = False
                        task.updated_at = _now()
                        idle = True
                    else:
                        instruction = task.pending.popleft()
                        task.state = "running"
                        task.updated_at = _now()
                if cancelled:
                    self._persist()
                    self._send_text(task, f"任务 {task.task_id} 已取消。")
                    return
                if idle:
                    self._persist()
                    return
                self._persist()

                reply, info = self._ask(task, instruction)
                if task.cancel_event.is_set():
                    with self._lock:
                        task.state = "cancelled"
                        task.last_reply = ""
                        task.worker_started = False
                        task.updated_at = _now()
                    self._persist()
                    self._send_text(task, f"任务 {task.task_id} 已取消。")
                    return

                state, clean = _parse_status(reply)
                if not clean and not info.get("error"):
                    state = "failed"
                    clean = "模型没有返回可用结果。可以继续描述要求，或检查模型服务配置。"
                with self._lock:
                    task.state = state
                    task.last_reply = clean[-1200:]
                    task.artifacts = self._artifacts(task)
                    task.updated_at = _now()
                self._persist()

                if info.get("error") and not clean:
                    clean = f"模型调用失败：{info['error']}"
                    with self._lock:
                        task.state = "failed"
                        task.last_reply = clean
                    self._persist()
                if clean:
                    self._send_text(task, f"任务 {task.task_id}：\n{clean}")
                self._send_artifacts(task)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                task.state = "failed"
                task.last_reply = f"{type(e).__name__}: {e}"
                task.worker_started = False
                task.updated_at = _now()
            self._persist()
            self._send_text(task, f"任务 {task.task_id} 执行失败：{type(e).__name__}: {e}")

    def _ask(self, task: CodeTask, instruction: str) -> tuple[str, dict]:
        import brain as B
        import local_tools as LT

        system = _task_system(task)
        ctx = {
            "source": "code_task",
            "event": task.event,
            "task": task,
            "task_id": task.task_id,
            "fs_root": str(task.root),
            "is_owner": True,
            "actor_id": task.actor_id,
            "actor_name": task.actor_name,
            "conversation_key": task.conversation_key,
        }
        blocked = {"code_task", "remember", "mood", "agreement",
                   "quiet_company"}
        with LT.bind_context(**ctx):
            reply, _reasoning, info = B.ask_with_system(
                instruction, system, level="serious", max_tokens=12000,
                block_tools=blocked,
                cancelled=task.cancel_event.is_set)
        return (reply or "").strip(), info or {}

    def _artifacts(self, task: CodeTask) -> list[str]:
        if not task.root.is_dir():
            return []
        out: list[str] = []
        try:
            for path in sorted(task.root.rglob("*")):
                if (not path.is_file() or ".aipet_runtime" in path.parts
                        or path.name.startswith(".")):
                    continue
                try:
                    if path.stat().st_size > MAX_ARTIFACT_BYTES:
                        continue
                    out.append(str(path.relative_to(task.root)).replace("\\", "/"))
                except (OSError, ValueError):
                    continue
                if len(out) >= MAX_ARTIFACTS:
                    break
        except OSError:
            pass
        return out

    def _send_text(self, task: CodeTask, text: str) -> None:
        if task.event is None or not text:
            return
        try:
            import qq_bot as QB
            result = QB.send_active(task.event, text)
            if result.get("_error") or result.get("_http_error"):
                QB.log(f"[code-task] 主动回传失败：{result}")
        except Exception as e:  # noqa: BLE001
            try:
                import qq_bot as QB
                QB.log(f"[code-task] 主动回传异常：{type(e).__name__}: {e}")
            except Exception:
                pass

    def _send_artifacts(self, task: CodeTask) -> None:
        if task.event is None:
            return
        try:
            import qq_bot as QB
            sent = 0
            for rel in task.artifacts:
                if rel in task.sent_artifacts or sent >= MAX_MEDIA_SEND:
                    continue
                path = task.root / rel
                if not path.is_file():
                    continue
                result = QB.send_media(task.event, path)
                if result.get("_error") or result.get("_http_error"):
                    QB.log(f"[code-task] 产物回传失败 {rel}：{result}")
                    continue
                task.sent_artifacts.add(rel)
                sent += 1
        except Exception as e:  # noqa: BLE001
            try:
                import qq_bot as QB
                QB.log(f"[code-task] 产物回传异常：{type(e).__name__}: {e}")
            except Exception:
                pass

    def latest_for(self, ctx: dict) -> CodeTask | None:
        with self._lock:
            return self._find(ctx)


def _parse_status(reply: str) -> tuple[str, str]:
    state = "completed"
    clean_lines: list[str] = []
    for line in (reply or "").splitlines():
        marker = re.search(r"TASK_STATUS\s*[:：]\s*(DONE|WAITING|FAILED)", line, re.I)
        if marker:
            state = {"DONE": "completed", "WAITING": "waiting",
                     "FAILED": "failed"}[marker.group(1).upper()]
            continue
        clean_lines.append(line)
    return state, "\n".join(clean_lines).strip()


def _task_system(task: CodeTask) -> str:
    persona = M.persona_text()
    instructions = f"""## 本地代码任务执行协议

你正在替主人执行一个真实的本地代码任务，任务编号是 `{task.task_id}`。
工作根目录是：`{task.root}`。所有代码、输入副本和输出产物都放在这个目录内，
不要写入仓库、用户主目录或任务目录之外。需要查看或保存文件时使用 fs_list、
fs_read、fs_write、fs_mkdir；QQ 附件需要长期留在任务目录时使用 keep_image；
需要运行代码时使用 run_python。run_python 的 stdout、
stderr 和文件变化是真实结果，必须根据它们继续修复和验证，不能凭空说“已经成功”。

你可以按需使用 web_search 获取公开资料。不要猜 API key，也不要把环境变量、配置
文件或密钥内容打印出来。缺少外部服务凭据时，停止在可继续的状态，明确告诉主人需要
由 bot 管理员提供哪一种凭据；不要把凭据写入任务产物或回复。

先理解要求，再直接实现；能运行就运行，遇到错误就修复后重跑。最终回复要说明做了什么、
验证结果和产物相对路径。完成时最后单独写 `TASK_STATUS: DONE`；需要主人补充信息或
决定时写 `TASK_STATUS: WAITING`；确实失败且暂时无法继续时写 `TASK_STATUS: FAILED`。

这是后台任务，不要调用 code_task，也不要修改长期记忆、心理状态或陪伴约定。
"""
    return "\n\n---\n\n".join(x for x in (persona, instructions) if x)


_MANAGER: TaskManager | None = None
_MANAGER_LOCK = threading.Lock()


def manager() -> TaskManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = TaskManager()
        return _MANAGER
