#!/usr/bin/env python3
"""
qq_bot.py —— 自己连 QQ 网关

═══════════════════════════════════════════════════════════════
  为什么非自己连不可
═══════════════════════════════════════════════════════════════

Cherry Studio 的 QQ 适配器**拿到了**发送者的身份：

    await this.processMessage(msg, chatId,
        msg.author.member_openid ?? msg.author.id,
        msg.author.username ?? "");

但它的 processIncoming 里只有这么一行：

    let textWithAttachments = message.text;

userId 和 userName 就在作用域里，**从来没被用过**。
所以模型看到的永远是裸文本，任何 MCP 工具都救不回来 ——
身份根本没进 prompt。要拿到它，只能自己持有这条连接。

注意：同一个 bot app 只能有一条网关连接。跑这个之前
必须停用 Cherry Studio 的 QQ 通道，否则事件会随机分配，
表现为"有时回有时不回"。工具在 tools/backup_cherry_qq.py。

═══════════════════════════════════════════════════════════════
  为什么不用 qq-botpy
═══════════════════════════════════════════════════════════════

官方 SDK 在 Python 3.14 上起不来：botpy/client.py 里
`asyncio.get_event_loop()` 在 3.14 已经是硬错误（不再隐式建 loop），
而且 Client.run() 会霸占调用线程 —— 跟 Qt 的事件循环天然打架。
它最后一次发版是 2024-03。

改用 PySide6 自带的 QtWebSockets：零新依赖，和桌宠共用一个事件循环。

═══ 用法 ═══
    python src/qq_bot.py selftest      # 离线自检，不联网
    python src/qq_bot.py run --debug   # 真连，只收不发，打印事件
    python src/qq_bot.py token         # 看 token 拿不拿得到
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402
import qq_text as QT  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

API_BASE = "https://api.bot.qq.com"
TOKEN_FILE = M.ROOT / "data" / "qq_token.json"
LOG_FILE = M.ROOT / "data" / "qq.log"
STATUS_FILE = M.ROOT / "data" / "qq_status.json"
PID_FILE = M.ROOT / "data" / "qq.pid"


def write_pid() -> None:
    """
    记下自己的进程号。

    ★ 开机自启是无窗口跑的（pythonw），没有黑框可以关。
      没有这个东西，想停就只能开任务管理器翻进程 —— 而且
      进程名是 pythonw.exe，翻不出哪个是它。
    """
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def read_pid() -> int:
    """读进程号。进程已经死了就返回 0，顺便把文件清掉。"""
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    if pid <= 0 or not _alive(pid):
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return 0
    return pid


def _alive(pid: int) -> bool:
    """这个进程还在吗。Windows 上没有 os.kill(pid, 0) 那套，用 tasklist。"""
    import subprocess
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in (r.stdout or "")
    except (subprocess.SubprocessError, OSError):
        return False


def kill_pid(pid: int) -> bool:
    import subprocess
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def write_status(state: str, detail: str = "", gw=None) -> None:
    """
    把连接状态落盘，给桌宠的右键菜单看。

    为什么不走进程间通信：桌宠和 QQ 桥是两个独立进程，
    而且 QQ 桥可能根本没跑。一个小 json 文件比 socket 简单得多，
    读的人也不怕对方已经死了 —— 看时间戳就知道是不是活的。
    """
    try:
        STATUS_FILE.write_text(json.dumps({
            "state": state,
            "detail": detail,
            "pid": os.getpid(),
            "at": M.now_iso(),
            "online": bool(gw.is_online()) if gw is not None else False,
            "attempts": gw.attempts if gw is not None else 0,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_status(stale_after: float = 180) -> dict:
    """
    读连接状态。超过 stale_after 秒没更新就算不新鲜 ——
    进程被 kill 掉的时候来不及写"我停了"，只能靠时间戳判断。
    """
    try:
        d = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "off", "detail": "没在跑", "fresh": False}

    try:
        age = (M.now() - M.parse_ts(d["at"])).total_seconds()
    except (KeyError, ValueError):
        age = 1e9
    d["fresh"] = age < stale_after
    d["age"] = age
    if not d["fresh"]:
        d["state"] = "off"
        d["detail"] = f"最后活动 {age/60:.0f} 分钟前"
    return d

INTENT_GROUP_AND_C2C = 1 << 25          # 33554432

# 网关 opcode
OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY = 0, 1, 2
OP_RESUME, OP_RECONNECT, OP_INVALID = 6, 7, 9
OP_HELLO, OP_HEARTBEAT_ACK = 10, 11

# ── 关闭码重试矩阵（抄官方文档的表）──────────────────────────
# 能不能 RESUME / 能不能重新 IDENTIFY，官方逐个码给了答案
CLOSE_FATAL = {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}
CLOSE_IDENTIFY_OK = {4006, 4007, 4008, 4009} | set(range(4900, 4914))
CLOSE_RESUME_OK = {4008, 4009} | set(range(4900, 4914))

_token_lock = threading.RLock()


# 自检期间关掉落盘。默认开。
#
# ★ 为什么需要这个开关：自检会灌假事件，日志里会出现
#   "群友 '小明'：你好"、"主人已认证：'主人'" 这种行。
#   而那些**不是真的发生过**。qq.log 是要拿来排查
#   "谁被认证成主人了"的地方，掺进假的就没法信了。
_logging = True


def log(msg: str) -> None:
    line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if not _logging:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════
#  凭据 + token
# ═══════════════════════════════════════════════════════════════

def secrets() -> tuple[str, str]:
    """从 data/secrets.json 读 app_id / client_secret。"""
    p = M.ROOT / "data" / "secrets.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    return str(d.get("qq_appid", "")), str(d.get("qq_secret", ""))


def _http(method: str, url: str, body: dict | None = None,
          token: str = "", timeout: float = 10) -> dict:
    """所有 HTTP 都从这儿走。返回解析后的 json，失败抛异常。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"QQBot {token}"
    if body is None and method == "GET":
        data = None
        headers.pop("Content-Type", None)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return {"_http_error": e.code, **json.loads(raw)}
        except json.JSONDecodeError:
            return {"_http_error": e.code, "message": raw[:200]}
    except (urllib.error.URLError, OSError) as e:
        raise ConnectionError(f"连不上 {url}：{e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw[:400]}


def _api(method: str, path: str, body: dict | None = None,
         timeout: float = 12) -> dict:
    """
    带鉴权的 API 调用。**401 就当场刷新 token 重试一次。**

    ★ 这是 token 失效的唯一可靠兜底。本地时钟判断不了服务端认不认 ——
      另一个进程断开、机器人重连、平台主动作废，本地都看不出来。
      见 access_token() 的说明。
    """
    r = _http(method, f"{API_BASE}{path}", body,
              token=access_token(), timeout=timeout)
    if r.get("_http_error") == 401:
        log("服务端说 token 无效（本地看着还没过期），强制刷新重试")
        invalidate_token()
        r = _http(method, f"{API_BASE}{path}", body,
                  token=access_token(force=True), timeout=timeout)
    return r


def fetch_token(appid: str, secret: str) -> tuple[str, float]:
    """
    拿 access_token。返回 (token, 到期时间戳)。

    ★ 官方文档里 expires_in 是**字符串** "7200"，不是数字。
      直接 time.time() + expires_in 会 TypeError —— 而且这个错误
      要等两小时后第一次刷新才炸，也就是上线之后才发现。
    """
    r = _http("POST", f"{API_BASE}/app/getAppAccessToken",
              {"appId": appid, "clientSecret": secret})
    tok = r.get("access_token")
    if not tok:
        raise RuntimeError(f"拿不到 token：{r}")
    exp = r.get("expires_in", 7200)
    try:
        exp = int(exp)                      # ← 字符串在这里转过来
    except (TypeError, ValueError):
        exp = 7200
    return tok, time.time() + exp


def access_token(force: bool = False) -> str:
    """
    带缓存 + 落盘的 token。

    官方说 7200 秒内重复请求**不会**返回新 token，
    只在到期前 60 秒内请求才刷新。所以缓存是必须的，
    不然重启一次就白等一次。

    ★ 但"本地看着还没过期"不等于"服务端认"。实测踩到过：
      磁盘上那个 token 本地算还有 3.9 分钟，服务端已经回 401 了 ——
      中间另一个进程（Cherry Studio）断开时把它作废了。
      所以真正的兜底是 _api() 里的"401 就刷新重试"，
      这里的 5 分钟余量只是减少撞上的概率。
    """
    with _token_lock:
        with _cache_lock:
            now = time.time()
            if not force and _token_cache["token"] and _token_cache["exp"] - 300 > now:
                return _token_cache["token"]
            if not force and not _token_cache["token"] and TOKEN_FILE.exists():
                try:
                    d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
                    if float(d.get("expires_at", 0)) - 300 > now:
                        _token_cache.update(d)
                        return _token_cache["token"]
                except (OSError, json.JSONDecodeError, ValueError, TypeError):
                    pass

            appid, secret = secrets()
            if not appid or not secret:
                raise RuntimeError(
                    "没配 QQ 凭据。跑一下 tools/backup_cherry_qq.py 从 Cherry Studio 抄过来。")
            tok, exp = fetch_token(appid, secret)
            _token_cache.update({"token": tok, "exp": exp})
            try:
                TOKEN_FILE.write_text(
                    json.dumps({"access_token": tok, "expires_at": exp},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError:
                pass
            return tok


def invalidate_token() -> None:
    with _token_lock:
        with _cache_lock:
            _token_cache.update({"token": "", "exp": 0.0})
        try:
            TOKEN_FILE.unlink()
        except OSError:
            pass


_token_cache: dict = {"token": "", "exp": 0.0}
_cache_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
#  事件
# ═══════════════════════════════════════════════════════════════

@dataclass
class QQEvent:
    kind: str = ""              # group_at | c2c | group_add_robot | ...
    msg_id: str = ""
    content: str = ""
    ts: datetime = field(default_factory=M.now)
    scene: str = "group"        # group | c2c
    group_openid: str = ""
    member_openid: str = ""     # 群里的人
    user_openid: str = ""       # 单聊的人
    union_openid: str = ""      # 跨场景统一，可能为空
    username: str = ""          # 昵称，可能为空串
    member_role: str = ""       # member | admin | owner

    # 富媒体。实测的形态（2026-09-16，群里发图）：
    #   content = " "（一个空格），attachments = [{content_type, filename, url, ...}]
    #   url 带 rkey 签名，可以直接 GET —— **但它会过期**，
    #   拿到就得马上下载，不能排队等。
    attachments: list[dict] = field(default_factory=list)

    @property
    def images(self) -> list[dict]:
        """附件里的图片。"""
        return [a for a in self.attachments
                if str(a.get("content_type", "")).startswith("image/")]

    @property
    def documents(self) -> list[dict]:
        """
        附件里的 Word 文档。

        ★ 靠**文件名后缀**认，不靠 content_type：QQ 对文件的类型标注
          没实测过，但 filename 是它自己给的、也一定在。认错了顶多
          是没读成，不会误伤别的。
        """
        out = []
        for a in self.attachments:
            name = str(a.get("filename") or "")
            if Path(name).suffix.lower() in (".docx", ".doc"):
                out.append(a)
        return out

    @property
    def speaker_id(self) -> str:
        """这个人在这条场景下的稳定 ID。"""
        return self.member_openid or self.user_openid

    @property
    def age_seconds(self) -> float:
        return (M.now() - self.ts).total_seconds()


def parse_event(t: str, d: dict) -> QQEvent | None:
    """把网关事件转成 QQEvent。不关心的类型返回 None。"""
    if t == "GROUP_AT_MESSAGE_CREATE":
        a = d.get("author") or {}
        return QQEvent(
            kind="group_at",
            msg_id=d.get("id", ""),
            content=(d.get("content") or "").strip(),
            ts=_ts(d.get("timestamp")),
            scene="group",
            group_openid=d.get("group_openid", ""),
            member_openid=a.get("member_openid") or a.get("id", ""),
            union_openid=a.get("union_openid", "") or "",
            username=(a.get("username") or "").strip(),
            member_role=a.get("member_role", "") or "",
            attachments=list(d.get("attachments") or []),
        )
    if t == "C2C_MESSAGE_CREATE":
        a = d.get("author") or {}
        return QQEvent(
            kind="c2c",
            msg_id=d.get("id", ""),
            content=(d.get("content") or "").strip(),
            ts=_ts(d.get("timestamp")),
            scene="c2c",
            user_openid=a.get("user_openid") or a.get("id", ""),
            union_openid=a.get("union_openid", "") or "",
            username=(a.get("username") or "").strip(),
            attachments=list(d.get("attachments") or []),
        )
    if t in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_MSG_REJECT",
             "GROUP_MSG_RECEIVE"):
        return QQEvent(kind=t.lower(), group_openid=d.get("group_openid", ""),
                       ts=_ts(d.get("timestamp")))
    if t in ("FRIEND_ADD", "FRIEND_DEL", "C2C_MSG_REJECT", "C2C_MSG_RECEIVE"):
        return QQEvent(kind=t.lower(), scene="c2c",
                       user_openid=(d.get("openid") or ""),
                       ts=_ts(d.get("timestamp")))
    return None


def _ts(s: str | None) -> datetime:
    if not s:
        return M.now()
    try:
        # RFC3339，带时区
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return M.now()


# ═══════════════════════════════════════════════════════════════
#  去重 / 序号
# ═══════════════════════════════════════════════════════════════

class Dedupe:
    """
    QQ 说"为确保消息可达，相同 msg_id 可能重复推送"。

    顺便管 msg_seq：同一个 msg_id 的每次回复要递增，
    重了会吃 40054005 消息被去重。
    被动回复每条消息最多 5 次，所以这里也当计数器用。
    """

    MAX_ENTRIES = 800
    MAX_REPLIES = 5

    def __init__(self):
        self._seen: dict[str, float] = {}
        self._seq: dict[str, int] = {}

    def seen(self, msg_id: str) -> bool:
        """True = 这条已经处理过，该丢。"""
        if not msg_id:
            return False
        self._trim()
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = time.time()
        return False

    def next_seq(self, msg_id: str) -> int:
        """下一个 msg_seq。超过 5 次返回 -1，表示别再发了。"""
        if not msg_id:
            return 1
        n = self._seq.get(msg_id, 0) + 1
        if n > self.MAX_REPLIES:
            return -1
        self._seq[msg_id] = n
        return n

    def _trim(self) -> None:
        if len(self._seen) <= self.MAX_ENTRIES:
            return
        for k in sorted(self._seen, key=self._seen.get)[:self.MAX_ENTRIES // 4]:
            self._seen.pop(k, None)
            self._seq.pop(k, None)


# ═══════════════════════════════════════════════════════════════
#  出站
# ═══════════════════════════════════════════════════════════════

def _post(path: str, body: dict, timeout: float = 12) -> dict:
    """
    发消息的唯一出口。

    ★ sanitize 在这里，不在 bridge 里。这是刻意的 ——
      它是最后一米。就算 prompt 组装出了 bug、某个新工具忘了过滤、
      模型自己编了个链接，也从这里漏不出去。
    """
    if "content" in body:
        clean, notes = QT.sanitize(body["content"])
        if notes:
            log(f"出站清洗：{'、'.join(notes)}")
        body = {**body, "content": clean}
        if not clean:
            return {"_skipped": "清洗后没内容了"}

    try:
        return _api("POST", path, body)
    except (ConnectionError, RuntimeError) as e:
        return {"_error": str(e)}


def send_group(group_openid: str, content: str, msg_id: str = "",
               msg_seq: int | None = None) -> dict:
    body: dict = {"content": content, "msg_type": 0}
    if msg_id:
        body["msg_id"] = msg_id
        body["msg_seq"] = msg_seq if msg_seq is not None else 1
    return _post(f"/v2/groups/{group_openid}/messages", body)


def send_c2c(user_openid: str, content: str, msg_id: str = "",
             msg_seq: int | None = None) -> dict:
    body: dict = {"content": content, "msg_type": 0}
    if msg_id:
        body["msg_id"] = msg_id
        body["msg_seq"] = msg_seq if msg_seq is not None else 1
    return _post(f"/v2/users/{user_openid}/messages", body)


def send_active(ev: QQEvent, content: str) -> dict:
    """主动发文字，不携带过期的 msg_id，供后台长任务回传。"""
    clean, notes = QT.sanitize(content or "")
    if notes:
        log(f"主动出站清洗：{'、'.join(notes)}")
    chunks = QT.split_messages(clean) if clean else []
    if not chunks:
        return {"_skipped": "清洗后没内容了"}
    result: dict = {}
    for chunk in chunks:
        result = (send_group(ev.group_openid, chunk) if ev.scene == "group"
                  else send_c2c(ev.user_openid, chunk))
        if result.get("_error") or result.get("_http_error"):
            return result
    return result


MEDIA_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MEDIA_HASH_PREFIX_BYTES = 10_002_432
MEDIA_PART_DEFAULT = 5 * 1024 * 1024


def _raw_put(url: str, data: bytes, timeout: float = 60) -> dict:
    """把一个分片 PUT 到 QQ 预签名地址；这里不能带 QQBot 鉴权头。"""
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/octet-stream",
                 "Content-Length": str(len(data))},
        method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return {"ok": True}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        return {"_http_error": e.code, "message": detail}
    except (urllib.error.URLError, OSError) as e:
        return {"_error": f"分片上传失败：{e}"}


def _file_hashes(path: Path) -> tuple[str, str, str]:
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    prefix = hashlib.md5()
    prefix_left = MEDIA_HASH_PREFIX_BYTES
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
            if prefix_left > 0:
                head = chunk[:prefix_left]
                prefix.update(head)
                prefix_left -= len(head)
    return md5.hexdigest(), sha1.hexdigest(), prefix.hexdigest()


def _media_type(path: Path) -> int:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return 1
    if suffix in {".mp4", ".mov", ".webm", ".mkv"}:
        return 2
    if suffix in {".silk", ".mp3", ".wav", ".ogg", ".m4a"}:
        return 3
    return 4


def upload_file(ev: QQEvent, source: str | Path) -> dict:
    """按 QQ 富媒体分片流程上传本地文件，返回含 file_info 的结果。"""
    path = Path(source).expanduser()
    try:
        path = path.resolve()
        size = path.stat().st_size
    except (OSError, RuntimeError) as e:
        return {"_error": f"找不到产物：{e}"}
    if not path.is_file():
        return {"_error": "产物不是文件"}
    if size <= 0:
        return {"_error": "空文件不能上传"}
    if size > MEDIA_MAX_UPLOAD_BYTES:
        return {"_error": f"文件超过 QQ 单文件上限 {MEDIA_MAX_UPLOAD_BYTES // 1048576} MB"}

    try:
        md5, sha1, md5_10m = _file_hashes(path)
    except OSError as e:
        return {"_error": f"读取产物失败：{e}"}

    if ev.scene == "group":
        ident = ev.group_openid
        scope = f"/v2/groups/{ident}"
    else:
        ident = ev.user_openid
        scope = f"/v2/users/{ident}"
    if not ident:
        return {"_error": "消息没有 QQ 会话标识，不能上传产物"}

    file_type = _media_type(path)
    prepare = _api("POST", scope + "/upload_prepare", {
        "file_type": file_type,
        "file_size": str(size),
        "file_name": path.name,
        "md5": md5,
        "sha1": sha1,
        "md5_10m": md5_10m,
    }, timeout=30)
    if prepare.get("_http_error") or prepare.get("_error"):
        return prepare

    upload_id = str(prepare.get("upload_id") or "")
    if not upload_id:
        return {"_error": f"QQ 预上传没有返回 upload_id：{prepare}"}
    try:
        block_size = max(1, int(prepare.get("block_size") or MEDIA_PART_DEFAULT))
    except (TypeError, ValueError):
        block_size = MEDIA_PART_DEFAULT

    parts = prepare.get("parts") or []
    if not isinstance(parts, list) or not parts:
        # 兼容仍返回单个 upload_url 的旧实现；新接口通常直接给 parts。
        one = str(prepare.get("upload_url") or "")
        if one:
            parts = [{"part_index": i + 1, "upload_url": one}
                     for i in range(math.ceil(size / block_size))]
    if not parts:
        return {"_error": f"QQ 预上传没有返回分片地址：{prepare}"}

    try:
        with path.open("rb") as f:
            for i, part in enumerate(parts):
                if not isinstance(part, dict):
                    return {"_error": "QQ 分片地址格式不正确"}
                url = str(part.get("upload_url") or part.get("presigned_url") or "")
                if not url:
                    return {"_error": f"第 {i + 1} 个分片没有预签名地址"}
                index = int(part.get("part_index", i + 1))
                chunk = f.read(block_size)
                if not chunk:
                    break
                put = _raw_put(url, chunk)
                if put.get("_error") or put.get("_http_error"):
                    return {"_error": f"第 {index} 个分片上传失败：{put}"}
                finish = _api("POST", scope + "/upload_part_finish", {
                    "upload_id": upload_id,
                    "part_index": index,
                    "block_size": len(chunk),
                    "md5": hashlib.md5(chunk).hexdigest(),
                }, timeout=30)
                if finish.get("_http_error") or finish.get("_error"):
                    return {"_error": f"第 {index} 个分片确认失败：{finish}"}
    except OSError as e:
        return {"_error": f"读取分片失败：{e}"}

    result = _api("POST", scope + "/files", {
        "file_type": file_type,
        "srv_send_msg": False,
        "file_name": path.name,
        "upload_id": upload_id,
    }, timeout=30)
    if result.get("_http_error") or result.get("_error"):
        return result
    if not result.get("file_info"):
        return {"_error": f"QQ 合并上传没有返回 file_info：{result}"}
    return result


def send_media(ev: QQEvent, source: str | Path) -> dict:
    """上传并主动发送一个任务产物。"""
    uploaded = upload_file(ev, source)
    if uploaded.get("_error") or uploaded.get("_http_error"):
        return uploaded
    body = {"msg_type": 7,
            "media": {"file_info": uploaded.get("file_info", "")}}
    path = (f"/v2/groups/{ev.group_openid}/messages" if ev.scene == "group"
            else f"/v2/users/{ev.user_openid}/messages")
    return _post(path, body)


def reply(ev: QQEvent, content: str, dedupe: "Dedupe | None" = None) -> dict:
    """回一条。自动选群/单聊、自动分配 msg_seq。"""
    seq = None
    if dedupe is not None and ev.msg_id:
        seq = dedupe.next_seq(ev.msg_id)
        if seq < 0:
            log(f"这个消息已经回过 {Dedupe.MAX_REPLIES} 次了，不发了")
            return {"_skipped": "超过被动回复次数上限"}

    # 被动回复有时限。超过就别回了 —— 会吃 40034128，
    # 而且用户早就走开了，突然冒一句更奇怪。
    if ev.msg_id and ev.age_seconds > PASSIVE_LIMIT_S:
        log(f"消息已经过了 {ev.age_seconds:.0f} 秒，超过被动回复窗口，放弃")
        return {"_skipped": "超过被动回复时限"}

    if ev.scene == "group":
        return send_group(ev.group_openid, content, ev.msg_id, seq)
    return send_c2c(ev.user_openid, content, ev.msg_id, seq)


# 群聊官方是 5 分钟。取 240 秒，留一分钟给发送本身。
PASSIVE_LIMIT_S = 240

# ── 看门狗 ──
# 每隔这么久体检一次连接，连续 WATCH_MISS_LIMIT 次不在线就强制重连。
# 20 秒 × 3 = 一分钟内发现断线 —— 对聊天够快了，也不会误伤握手中的瞬态。
WATCH_INTERVAL_MS = 20000
WATCH_MISS_LIMIT = 3


# ═══════════════════════════════════════════════════════════════
#  网关
# ═══════════════════════════════════════════════════════════════

def gateway_url() -> str:
    """
    ★ 别写死 wss 地址。官方让先问一次 /gateway，它给什么用什么。
    """
    r = _api("GET", "/gateway")
    url = r.get("url")
    if not url:
        raise RuntimeError(f"拿不到网关地址：{r}")
    return url


class QQGateway:
    """
    QQ 网关连接。用 QtWebSockets，和桌宠共用一个事件循环。

    不用 QObject/Signal 是为了能脱开 Qt 单测 —— 回调就够了，
    接线的事交给 qq_bridge。

        gw = QQGateway(on_event=..., on_state=...)
        gw.start()          # 需要已经有 QCoreApplication
    """

    def __init__(self, on_event=None, on_state=None, on_fatal=None):
        self.on_event = on_event
        self.on_state = on_state
        self.on_fatal = on_fatal

        self.ws = None
        self.seq: int | None = None
        self.session_id: str = ""
        self.heartbeat_ms = 40000
        self._hb_timer = None
        self._attempts = 0
        self._closing = False
        self.dedupe = Dedupe()
        # 见过但没处理的事件类型。用来在日志里只报一次，不刷屏。
        self._seen_types: set[str] = set()
        self._started = False

        # ── 看门狗 ──
        # ★ 为什么必须有它：实测踩过 —— 网关断的时候 Qt 只发了
        #   errorOccurred(RemoteHostClosedError)，**没发 disconnected**。
        #   而重连挂在 disconnected 上，于是连接死了、进程还活着、
        #   心跳还在写状态文件，看起来一切正常但 QQ 上她永远不回话。
        #   光靠信号是堵不住的，得有个定期体检的。
        self._watch_timer = None
        self._miss = 0            # 连续几次体检发现不在线
        self._reconnecting = False  # 防止重复排重连

    # ── 状态回调 ──
    def _state(self, s: str, detail: str = "") -> None:
        log(f"网关状态：{s}{('  ' + detail) if detail else ''}")
        write_status(s, detail, self)
        if self.on_state:
            try:
                self.on_state(s, detail)
            except Exception as e:
                log(f"状态回调出错：{e}")

    def _fatal(self, msg: str) -> None:
        """
        不可重试的错误（凭据不对、机器人被下架、intents 没权限）。

        ★ 不退出进程。原来这里最后会 app.quit()，结果是：出一次错，
          桥就没了，而且开机自启只在登录时跑一次 —— 也就是 QQ 会一直死
          到你下次重启电脑，还看不出来为什么。
          现在改成停在这儿、把原因写进状态文件，桌宠右键菜单能看见。
        """
        self._started = False               # 让看门狗和重连都停下
        self._reconnecting = False
        if self._watch_timer:
            self._watch_timer.stop()
        if self._hb_timer:
            self._hb_timer.stop()
        log(f"致命错误（不再重试）：{msg}")
        self._state("fatal", msg[:80])
        if self.on_fatal:
            try:
                self.on_fatal(msg)
            except Exception:
                pass

    # ── 连接 ──
    def start(self) -> None:
        from PySide6.QtCore import QTimer
        from PySide6.QtWebSockets import QWebSocket
        self._started = True
        self._closing = False
        write_pid()
        try:
            url = gateway_url()
        except ConnectionError as e:
            # ★ 网络抖动不该让整个桥退出。原来这里走 _fatal，
            #   而 bridge 的 on_fatal 是 app.quit() —— 一次断网就把
            #   进程结束了，之后再也不会自己起来。
            log(f"拿不到网关地址（网络问题）：{e}")
            self._reconnecting = False           # 放行，让下面能排下一轮
            self._reconnect(resume=False)
            return
        except RuntimeError as e:
            if "凭据" in str(e):
                self._fatal(str(e))              # 没配凭据，重试一万次也没用
                return
            log(f"拿不到网关地址：{e}")
            self._reconnecting = False
            self._reconnect(resume=False)
            return
        self._state("connecting", url)
        # 上一轮那个 socket 要收掉，不然重连几次会攒一堆僵尸对象
        if self.ws is not None:
            try:
                self.ws.close()
                self.ws.deleteLater()
            except Exception:
                pass
        self.ws = QWebSocket()
        self.ws.textMessageReceived.connect(self._on_text)
        self.ws.connected.connect(self._on_open)
        self.ws.disconnected.connect(self._on_close)
        # 错误信号也排一次重连 —— 不能只等 disconnected，因为
        # 它们不一定成对出现（见 __init__ 里看门狗的说明）。
        self.ws.errorOccurred.connect(self._on_error)
        self._hb_timer = QTimer()
        self._hb_timer.timeout.connect(self._send_heartbeat)
        if self._watch_timer is None:
            self._watch_timer = QTimer()
            self._watch_timer.timeout.connect(self._watch)
        self._watch_timer.start(WATCH_INTERVAL_MS)
        self.ws.open(url)

    def stop(self) -> None:
        self._closing = True
        self._started = False
        if self._hb_timer:
            self._hb_timer.stop()
        if self._watch_timer:
            self._watch_timer.stop()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        self._state("stopped")

    def is_online(self) -> bool:
        """
        WebSocket 真的连着吗。

        ★ 枚举必须从 QAbstractSocket 取。PySide6 6.x 起枚举是作用域化的，
          `QWebSocket.ConnectedState` 在 6.11 上**不存在** —— 而它抛的是
          AttributeError，被 try/except 一吞就变成"永远返回 False"。
          症状是桌宠右键菜单一直显示"连接中"，但消息其实收得到。
        """
        if not self.ws:
            return False
        try:
            from PySide6.QtNetwork import QAbstractSocket
        except ImportError:
            return False
        try:
            return self.ws.state() == QAbstractSocket.SocketState.ConnectedState
        except (RuntimeError, AttributeError):
            # 底层 C++ 对象已经被删掉了（窗口关了 / 重连中途）
            return False

    def _send(self, payload: dict) -> None:
        if not self.ws:
            return
        try:
            self.ws.sendTextMessage(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            log(f"发送失败：{e}")

    # ── 收 ──
    def _on_open(self) -> None:
        self._state("open", "等 HELLO")
        self._attempts = 0
        self._miss = 0
        self._reconnecting = False       # 这一轮尝试结束了，允许下次再排

    def _on_text(self, raw: str) -> None:
        try:
            p = json.loads(raw)
        except json.JSONDecodeError:
            log(f"网关发来的不是 JSON：{raw[:120]}")
            return

        op = p.get("op")
        if p.get("s") is not None:
            self.seq = p["s"]

        if op == OP_HELLO:
            d = p.get("d") or {}
            self.heartbeat_ms = int(d.get("heartbeat_interval", 40000))
            self._start_heartbeat()
            if self.session_id and self.seq is not None:
                self._send_resume()
            else:
                self._send_identify()

        elif op == OP_DISPATCH:
            t = p.get("t", "")
            d = p.get("d") or {}
            if t == "READY":
                self.session_id = d.get("session_id", "")
                bot = (d.get("user") or {}).get("username", "?")
                self._state("ready", f"以 {bot} 的身份上线")
            elif t == "RESUMED":
                self._state("ready", "会话已恢复")
            else:
                self._dispatch(t, d)

        elif op == OP_HEARTBEAT_ACK:
            pass

        elif op == OP_RECONNECT:
            self._state("reconnect", "服务端要求重连")
            self._reconnect(resume=True)

        elif op == OP_INVALID:
            self._state("invalid", "会话失效，重新握手")
            self.session_id = ""
            self.seq = None
            self._reconnect(resume=False)

    def _dispatch(self, t: str, d: dict) -> None:
        # ★ 所有进入的事件类型都先记一笔，一个不漏。
        #   起因：群里发了带图的消息，日志里只剩文字，探针也一声没响 ——
        #   连"不认识的事件类型"都没报。那说明图片要么走了别的类型，
        #   要么压根没推过来。不在这里记下所有类型，就分不清是哪一种。
        #   （每种只报一次，免得刷屏）
        if t not in self._seen_types:
            self._seen_types.add(t)
            log(f"事件类型（首次见到）：{t}｜顶层字段：{sorted(d)[:14]}")

        ev = parse_event(t, d)
        if ev is None:
            return

        # ★ 空内容的极可能就是图片：QQ 把图放在富媒体字段里，正文是空的。
        #   这里必须在**解析之后、交出去之前**看一眼原始 d ——
        #   QQEvent 只留了 content，附件字段在 parse_event 那步就没了。
        if not (ev.content or "").strip():
            _keys = sorted(d)
            log(f"⚠ 空内容事件 kind={ev.kind}｜原始字段：{_keys}")
            for _k in ("content", "attachments", "message_type", "media", "file_info"):
                if _k in d:
                    _v = json.dumps(d[_k], ensure_ascii=False)[:500]
                    log(f"    {_k} = {_v}")

        if ev.msg_id and self.dedupe.seen(ev.msg_id):
            log(f"重复推送，丢弃：{ev.msg_id[:24]}…")
            return
        log(f"收到 {ev.kind}｜{ev.username or '(无昵称)'}"
            f"｜{(ev.content or '')[:40]}")
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception as e:
                log(f"事件处理出错：{type(e).__name__}: {e}")

    # ── 握手 ──
    def _send_identify(self) -> None:
        self._send({"op": OP_IDENTIFY, "d": {
            # ★ 前缀是 "QQBot "。官方两份文档打架，另一份写的是旧的
            #   "Bot {appid}.{app_token}"。用 getAppAccessToken 拿到的
            #   token，前缀必须是 QQBot。
            "token": f"QQBot {access_token()}",
            # 只订群 @ 和单聊。多传没有权限的位会被网关直接关连接（4013/4014）。
            "intents": INTENT_GROUP_AND_C2C,
            "shard": [0, 1],
            "properties": {"$os": "windows", "$browser": "aipet", "$device": "aipet"},
        }})
        self._state("identify", "已发鉴权")

    def _send_resume(self) -> None:
        self._send({"op": OP_RESUME, "d": {
            "token": f"QQBot {access_token()}",
            "session_id": self.session_id,
            "seq": self.seq,
        }})
        self._state("resume", "尝试恢复会话")

    def _start_heartbeat(self) -> None:
        if self._hb_timer:
            self._hb_timer.start(max(5000, self.heartbeat_ms))

    def _send_heartbeat(self) -> None:
        self._send({"op": OP_HEARTBEAT, "d": self.seq})
        # ★ 顺手刷新状态文件的时间戳。
        #   只在状态"变化"时写的话，一个稳定连着几小时的连接会被
        #   读的一方（桌宠右键菜单）当成过期 —— 心跳是现成的周期信号，
        #   拿它当"我还活着"的脉搏正好。
        write_status("ready" if self.is_online() else "connecting",
                     "心跳中", self)

    # ── 看门狗 ──
    def _on_error(self, err) -> None:
        self._state("error", str(err))
        # 连接级的错误：直接排一次重连，别等 disconnected
        # （它不一定来）。_reconnect 内部有 _reconnecting 去重。
        self._reconnect(resume=False)

    def _watch(self) -> None:
        """
        定期体检。信号堵不住的窟窿由这里兜。

        WATCH_MISS_LIMIT 次连续不在线就强制重连。为什么不是一次就重连：
        WS 正常握手时也会有短暂的不在线瞬间，一次就动手会误伤。
        """
        if self._closing or not self._started:
            return
        if self.is_online():
            self._miss = 0
            return
        self._miss += 1
        if self._miss < WATCH_MISS_LIMIT:
            log(f"看门狗：连接不在线（第 {self._miss}/{WATCH_MISS_LIMIT} 次）")
            return
        self._miss = 0
        log(f"看门狗：连续 {WATCH_MISS_LIMIT} 次体检都不在线，强制重连")
        self._reconnect(resume=False)

    # ── 断线 ──
    def _on_close(self, code: int, reason: str) -> None:
        if self._closing:
            return
        self._state("closed", f"{code} {reason}")

        if code in CLOSE_FATAL:
            hint = {
                4013: "intents 没有权限 —— 去 QQ 开放平台确认群 @ 消息的权限开了",
                4014: "intents 不合法或分片数不对",
                4914: "机器人已下架",
                4915: "机器人已封禁",
            }.get(code, "不可重试")
            self._fatal(f"网关关闭 {code}：{hint}（{reason}）")
            return

        # 4008 / 4009 / 4900-4913 可以 resume，其余要重新 identify
        can_resume = code in CLOSE_RESUME_OK and bool(self.session_id)
        if not can_resume and code not in CLOSE_IDENTIFY_OK:
            log(f"关闭码 {code} 不在重试表里，按可重连处理")
        self._reconnect(resume=can_resume)

    def _reconnect(self, resume: bool = False) -> None:
        from PySide6.QtCore import QTimer
        if self._closing or not self._started:
            return
        # ★ 已经排过了就别再排。errorOccurred 和 disconnected 常常前后脚
        #   一起来，看门狗也可能同时判定不在线 —— 没有这道闸，
        #   一次掉线会排出好几个 start()，互相把对方的 socket 关掉。
        if self._reconnecting:
            return
        self._reconnecting = True
        if self._hb_timer:
            self._hb_timer.stop()
        if not resume:
            self.session_id = ""
            self.seq = None

        self._attempts += 1
        # 指数退避 + 抖动。加抖动是因为多个 bot 同时掉线时
        # 固定间隔会让它们同时重连，一起被限流。
        delay = min(60.0, (2 ** min(self._attempts, 6)) * 0.5)
        delay *= 0.7 + random.random() * 0.6
        self._state("waiting", f"{delay:.1f} 秒后第 {self._attempts} 次重连")
        QTimer.singleShot(int(delay * 1000), self.start)

    @property
    def attempts(self) -> int:
        return self._attempts


# ═══════════════════════════════════════════════════════════════
#  自检
# ═══════════════════════════════════════════════════════════════

def selftest() -> int:
    fails = 0

    def check(label, cond, extra=""):
        nonlocal fails
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + extra) if extra else ''}")
        if not cond:
            fails += 1

    _quiet_logging()          # 自检不往生产日志里写假事件
    print("QQ 网关自检（离线，不联网）\n")

    # ── 常量 ──
    check("intents = 1<<25", INTENT_GROUP_AND_C2C == 33554432)
    check("token 前缀是 QQBot", "QQBot " .strip() == "QQBot")

    # ── 事件解析 ──
    g = parse_event("GROUP_AT_MESSAGE_CREATE", {
        "id": "ROBOT1.0_abc", "content": " 你好 ",
        "group_openid": "GRP1",
        "timestamp": "2026-09-15T10:00:00+08:00",
        "author": {"id": "AAA", "member_openid": "AAA", "username": "小明",
                   "member_role": "owner", "union_openid": "UNI1"},
    })
    check("解析群消息", g is not None and g.kind == "group_at")
    check("去掉内容两边空白", g.content == "你好")
    check("拿到 member_openid", g.member_openid == "AAA")
    check("拿到昵称", g.username == "小明")
    check("拿到群内角色", g.member_role == "owner")
    check("拿到 union_openid", g.union_openid == "UNI1")
    check("时间是带时区的 datetime", g.ts.tzinfo is not None)
    check("speaker_id 用 member_openid", g.speaker_id == "AAA")

    g2 = parse_event("GROUP_AT_MESSAGE_CREATE", {
        "id": "X", "content": "hi", "group_openid": "GRP",
        "author": {"id": "BBB"},                      # 没有 username
    })
    check("昵称缺失时是空串不是 None", g2.username == "")
    check("member_openid 缺失时退回 id", g2.member_openid == "BBB")
    check("union_openid 缺失时是空串", g2.union_openid == "")

    c = parse_event("C2C_MESSAGE_CREATE", {
        "id": "Y", "content": "在吗",
        "author": {"user_openid": "UUU", "username": ""},
    })
    check("解析单聊", c is not None and c.scene == "c2c")
    check("单聊用 user_openid", c.speaker_id == "UUU")
    check("单聊昵称可以为空", c.username == "")

    check("不认识的事件返回 None", parse_event("SOMETHING_ELSE", {}) is None)

    # ── 去重 ──
    d = Dedupe()
    check("第一次见不算重复", not d.seen("M1"))
    check("第二次见算重复", d.seen("M1"))
    check("空 msg_id 不算重复", not d.seen("") and not d.seen(""))
    check("msg_seq 从 1 开始", d.next_seq("M2") == 1)
    check("msg_seq 递增", d.next_seq("M2") == 2)
    for _ in range(3):
        d.next_seq("M2")
    check(f"超过 {Dedupe.MAX_REPLIES} 次返回 -1", d.next_seq("M2") == -1)
    check("不同消息各自计数", d.next_seq("M3") == 1)

    # 疯狂灌入不爆
    for i in range(3000):
        d.seen(f"BULK{i}")
    check("大量去重记录会自我修剪", len(d._seen) <= Dedupe.MAX_ENTRIES + 10,
          f"{len(d._seen)} 条")

    # ── 关闭码决策 ──
    check("4013(intents) 是致命的", 4013 in CLOSE_FATAL)
    check("4914(下架) 是致命的", 4914 in CLOSE_FATAL)
    check("4009 可 resume", 4009 in CLOSE_RESUME_OK)
    check("4006 可 identify 不可 resume",
          4006 in CLOSE_IDENTIFY_OK and 4006 not in CLOSE_RESUME_OK)
    check("4900 可 resume", 4900 in CLOSE_RESUME_OK)
    check("致命码不在可重连集合里", not (CLOSE_FATAL & CLOSE_IDENTIFY_OK))

    # ── expires_in ──
    # 这个不用真联网：直接验转换逻辑
    for raw, want in [("7200", 7200), (7200, 7200), (None, 7200), ("abc", 7200)]:
        try:
            got = int(raw) if raw is not None else 7200
        except (TypeError, ValueError):
            got = 7200
        check(f"expires_in={raw!r} → {want}", got == want)

    # ── 出站清洗 ──
    # 用一个假的 _http 拦下来，看真正要发出去的 body 长什么样
    sent: list[dict] = []
    real_http = globals()["_http"]
    globals()["_http"] = lambda m, u, b=None, token="", timeout=10: (
        sent.append(b) or {"id": "ok"})
    globals()["access_token"] = lambda force=False: "FAKE"
    try:
        _post("/v2/groups/G/messages",
              {"content": "看 https://a.com/x 这个 **重点**", "msg_type": 0})
    finally:
        globals()["_http"] = real_http

    check("出站 body 被洗过", sent and "http" not in sent[0]["content"],
          f"→ {sent[0]['content'] if sent else '(没发出去)'}")
    check("洗过之后仍然有内容", sent and sent[0]["content"].strip())

    # ── 超时判断 ──
    old = QQEvent(kind="group_at", msg_id="M", content="x",
                  ts=M.now() - __import__("datetime").timedelta(hours=1))
    check("一小时前的消息算超时", old.age_seconds > PASSIVE_LIMIT_S)
    fresh = QQEvent(kind="group_at", msg_id="M", content="x")
    check("刚到的消息不算超时", fresh.age_seconds < PASSIVE_LIMIT_S)

    # ── is_online 的枚举 ──
    # 这个测试存在的理由：写错枚举名会抛 AttributeError，
    # 而外面那层 try/except 会把它变成"永远 False"，症状是
    # 一切正常但状态显示"连接中"。不测就发现不了。
    try:
        from PySide6.QtNetwork import QAbstractSocket
        from PySide6.QtWebSockets import QWebSocket
        check("SocketState 枚举取自 QAbstractSocket",
              hasattr(QAbstractSocket, "SocketState")
              and QAbstractSocket.SocketState.ConnectedState is not None)
        check("★ QWebSocket.ConnectedState 在 6.11 上不存在（所以不能这么写）",
              not hasattr(QWebSocket, "ConnectedState"))
        gw0 = QQGateway()
        check("没连的时候 is_online 为 False", gw0.is_online() is False)
    except ImportError as e:
        check("QtWebSockets 可用", False, str(e))

    # ── 状态文件 ──
    import json as _json
    old_status = STATUS_FILE.read_text(encoding="utf-8") if STATUS_FILE.exists() else None
    try:
        write_status("ready", "自检写入", None)
        st = read_status()
        check("状态能写能读", st["state"] == "ready")
        check("刚写的算新鲜", st["fresh"] is True)
        # 时间戳拨到过去 = 进程被 kill 来不及写
        d = _json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        d["at"] = (M.now() - __import__("datetime").timedelta(minutes=10)
                   ).isoformat(timespec="seconds")
        STATUS_FILE.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
        st2 = read_status()
        check("★ 超时未更新算没在跑", st2["state"] == "off" and not st2["fresh"],
              f"{st2.get('detail')}")
    finally:
        if old_status is None:
            try:
                STATUS_FILE.unlink()
            except OSError:
                pass
        else:
            STATUS_FILE.write_text(old_status, encoding="utf-8")

    # ── 断线自愈 ──
    # 这段是拿真事故换来的：网关断的时候 Qt 只发了 errorOccurred，
    # 没发 disconnected。重连挂在 disconnected 上，于是连接死了、
    # 心跳还在、状态文件还新鲜，看起来一切正常但 QQ 上永远不回话。
    check("看门狗间隔合理", 5000 <= WATCH_INTERVAL_MS <= 120000,
          f"{WATCH_INTERVAL_MS}ms × {WATCH_MISS_LIMIT} 次")
    check("看门狗一分钟内能发现断线",
          WATCH_INTERVAL_MS * WATCH_MISS_LIMIT <= 120000,
          f"{WATCH_INTERVAL_MS * WATCH_MISS_LIMIT / 1000:.0f} 秒")

    try:
        from PySide6 import QtCore
    except ImportError:
        QtCore = None

    if QtCore is not None:
        scheduled: list = []
        real_shot = QtCore.QTimer.singleShot
        QtCore.QTimer.singleShot = staticmethod(
            lambda ms, fn=None: scheduled.append(ms))
        try:
            gw = QQGateway()
            gw._started = True

            # 一次掉线里 errorOccurred 和 disconnected 常常前后脚来
            gw._reconnect(resume=False)
            gw._reconnect(resume=False)
            gw._reconnect(resume=False)
            check("★ 重复排重连只排一次（三个信号一起来也只连一次）",
                  len(scheduled) == 1, f"排了 {len(scheduled)} 次")

            gw._on_open()
            gw._reconnect(resume=False)
            check("连上之后允许再排", len(scheduled) == 2)

            # 看门狗：要连续几次才动手，不能一次就误伤
            gw2 = QQGateway()
            gw2._started = True
            gw2.is_online = lambda: False          # type: ignore
            for _ in range(WATCH_MISS_LIMIT - 1):
                gw2._watch()
            check(f"★ 不满 {WATCH_MISS_LIMIT} 次不动手（不误伤握手瞬态）",
                  not gw2._reconnecting)
            gw2._watch()
            check(f"连续 {WATCH_MISS_LIMIT} 次不在线就强制重连",
                  gw2._reconnecting and len(scheduled) == 3)

            # 在线就把计数清零
            gw3 = QQGateway()
            gw3._started = True
            gw3.is_online = lambda: False          # type: ignore
            gw3._watch(); gw3._watch()
            gw3.is_online = lambda: True           # type: ignore
            gw3._watch()
            check("恢复在线后计数清零", gw3._miss == 0)

            # 致命错误不能自杀
            gw4 = QQGateway()
            gw4._started = True
            gw4._hb_timer = QtCore.QTimer()
            gw4._watch_timer = QtCore.QTimer()
            gw4._fatal("机器人已下架")
            check("★ 致命错误不退出进程（退出就再也没人拉起来）",
                  gw4._started is False and not gw4._watch_timer.isActive())
            st = read_status()
            check("致命原因写进了状态文件（桌宠菜单看得见）",
                  st.get("state") == "fatal" and "下架" in str(st.get("detail")),
                  f"{st.get('state')} / {st.get('detail')}")
        finally:
            QtCore.QTimer.singleShot = real_shot
            try:
                STATUS_FILE.unlink()
            except OSError:
                pass
    else:
        check("PySide6 可用（跳过 Qt 相关检查）", False)

    # ── 401 自动刷新重试 ──
    # 实测踩过：本地算还有 3.9 分钟，服务端已经 401 了。
    # 所以这条必须成立 —— 它是 token 失效的唯一可靠兜底。
    calls: list[str] = []

    def fake_http(method, url, body=None, token="", timeout=10):
        calls.append(token)
        if token == "OLD":
            return {"_http_error": 401, "message": "鉴权失败"}
        return {"url": "wss://ok"}

    real_http2 = globals()["_http"]
    real_tok = globals()["access_token"]
    invalided: list[bool] = []
    globals()["_http"] = fake_http
    globals()["access_token"] = lambda force=False: ("OLD" if not force else "NEW")
    globals()["invalidate_token"] = lambda: invalided.append(True)
    try:
        r = _api("GET", "/gateway")
    finally:
        globals()["_http"] = real_http2
        globals()["access_token"] = real_tok
    check("401 会自动刷新 token 重试", r.get("url") == "wss://ok", f"{r}")
    check("确实用了新 token", calls == ["OLD", "NEW"], f"{calls}")
    check("旧 token 被作废", len(invalided) == 1)

    # 不是 401 就不要瞎刷新
    calls2: list[str] = []

    def fake_http2(method, url, body=None, token="", timeout=10):
        calls2.append(token)
        return {"_http_error": 404, "message": "not found"}

    globals()["_http"] = fake_http2
    globals()["access_token"] = lambda force=False: "OLD"
    try:
        _api("GET", "/gateway")
    finally:
        globals()["_http"] = real_http2
        globals()["access_token"] = real_tok
    check("非 401 不重试", calls2 == ["OLD"], f"{calls2}")

    print(f"\n{'全部通过' if fails == 0 else str(fails) + ' 项失败'}")
    return 1 if fails else 0


def _quiet_logging() -> None:
    """自检时不往生产日志里写假事件。见 _logging 的说明。"""
    global _logging
    _logging = False


# ═══════════════════════════════════════════════════════════════
#  命令行
# ═══════════════════════════════════════════════════════════════

def _run(debug_only: bool = False) -> int:
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication(sys.argv)

    appid, secret = secrets()
    if not appid:
        print("没配 QQ 凭据。先跑：python tools/backup_cherry_qq.py")
        return 1
    print(f"app_id {appid}")
    try:
        gateway_url()
        print("网关地址拿得到 ✓")
    except (RuntimeError, ConnectionError) as e:
        print(f"连不上：{e}")
        return 1

    def on_event(ev: QQEvent):
        print(f"\n{'='*56}")
        print(f"  类型      {ev.kind}   场景 {ev.scene}")
        print(f"  昵称      {ev.username!r}")
        print(f"  成员 ID   {ev.member_openid or '(无)'}")
        print(f"  单聊 ID   {ev.user_openid or '(无)'}")
        print(f"  统一 ID   {ev.union_openid or '(空)'}   ← 可能为空，不能当主键")
        print(f"  群角色    {ev.member_role or '(无)'}")
        print(f"  群        {ev.group_openid or '(无)'}")
        print(f"  内容      {ev.content}")
        if debug_only:
            print("  （--debug：只收不发）")
        print('='*56, flush=True)

    gw = QQGateway(on_event=on_event,
                   on_state=lambda s, d: print(f"  · {s} {d}"),
                   on_fatal=lambda m: (print(f"  ✗ {m}"), app.quit()))
    gw.start()
    print("\n已连接，在群里 @ 一下机器人试试。Ctrl+C 退出。\n")
    return app.exec()


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
    elif args[0] == "selftest":
        sys.exit(selftest())
    elif args[0] == "token":
        try:
            t = access_token()
            print(f"token 拿到了：{t[:8]}…（{len(t)} 字符）")
            print(f"网关：{gateway_url()}")
        except (RuntimeError, ConnectionError) as e:
            print(f"失败：{e}")
            sys.exit(1)
    elif args[0] == "run":
        sys.exit(_run(debug_only="--debug" in args))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
