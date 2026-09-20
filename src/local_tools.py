#!/usr/bin/env python3
"""
local_tools.py —— 小日和能调用的本地工具

背景：用户问过它"现在几点"，它答"我看不到你机器上的时间"。
那是诚实（BOUNDARIES.md 要求不假装），但也是个能力缺口。
这个文件补上。

五个工具：

    get_time      当前日期时间、星期几
    get_system    电量、开机时长、系统版本
    web_search    联网搜索（走 tools.py，带缓存）
    recall        检索记忆库
    remember      写入记忆库

DeepSeek 的工具调用有两个坑（官方文档写的）：

  1. **携带 tools 的请求，后续所有请求必须完整回传 reasoning_content**，
     即使那一轮没有实际调用工具。不回传会 400。
  2. 思考模式下工具调用会有多轮"思考→调用→再思考"，
     max_tokens 要留够，否则会在中途被截断。

用法（自测）：
    python src/local_tools.py            # 跑一遍所有工具
    python src/local_tools.py time       # 只跑一个
"""

from __future__ import annotations

import json
import contextlib
import contextvars
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

# 工具调用可能同时来自桌宠、本地聊天窗口、QQ 普通对话和后台代码任务。
# 用 contextvar 传递任务根目录和身份，避免用一个全局变量串错会话。
_TOOL_CONTEXT = contextvars.ContextVar("aipet_tool_context", default={})


@contextlib.contextmanager
def bind_context(**values):
    """给本轮工具调用绑定上下文；退出后自动恢复上层上下文。"""
    old = dict(_TOOL_CONTEXT.get() or {})
    old.update(values)
    token = _TOOL_CONTEXT.set(old)
    try:
        yield old
    finally:
        _TOOL_CONTEXT.reset(token)


def tool_context() -> dict:
    return dict(_TOOL_CONTEXT.get() or {})


@dataclass(frozen=True)
class ToolPolicy:
    """同一份工具授权同时约束展示和执行。"""

    allowed: frozenset[str] | None = None

    def allows(self, name: str) -> bool:
        return self.allowed is None or name in self.allowed

    def visible(self, specs: list[dict]) -> list[dict]:
        if self.allowed is None:
            return list(specs)
        return [spec for spec in specs
                if spec.get("function", {}).get("name") in self.allowed]


def make_policy(blocked_tools: set[str] | None = None,
                allowed_tools: set[str] | None = None) -> ToolPolicy:
    """从可信调用上下文生成策略；模型参数不会参与授权。"""
    if blocked_tools is None and allowed_tools is None:
        return ToolPolicy()
    known = {spec.get("function", {}).get("name") for spec in SPECS}
    names = set(allowed_tools) if allowed_tools is not None else known
    names &= known
    names -= set(blocked_tools or ())
    return ToolPolicy(frozenset(names))

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ═══════════════════════════════════════════════════════════════
#  工具实现
# ═══════════════════════════════════════════════════════════════

def get_time() -> str:
    """当前日期和时间。任何时候需要知道"现在"都该调它，不要猜。"""
    now = M.now()
    hour = now.hour
    if hour < 5:
        part = "凌晨"
    elif hour < 9:
        part = "早上"
    elif hour < 12:
        part = "上午"
    elif hour < 14:
        part = "中午"
    elif hour < 18:
        part = "下午"
    elif hour < 23:
        part = "晚上"
    else:
        part = "深夜"

    return (f"{now:%Y年%m月%d日} {WEEKDAY[now.weekday()]} "
            f"{now:%H:%M}（{part}）")


def get_system() -> str:
    """机器状态：电量、开机多久、系统版本。查电量或机器情况时用。"""
    lines = [f"系统：{platform.system()} {platform.release()}"]

    # 电量（Windows）。失败就算了，不要因为读不到电量整个工具报错。
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$b=Get-CimInstance Win32_Battery;"
             "if($b){'{0}|{1}' -f $b.EstimatedChargeRemaining,"
             "$b.BatteryStatus}else{'none'}"],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (r.stdout or "").strip()
        if out and out != "none":
            pct, status = (out.split("|") + ["?"])[:2]
            state = {"1": "放电中", "2": "接着电源"}.get(status, "未知")
            lines.append(f"电量：{pct}%（{state}）")
        elif out == "none":
            lines.append("电量：读不到（可能是台式机）")
    except (subprocess.SubprocessError, OSError):
        lines.append("电量：读不到")

    # 开机时长
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[int]((Get-Date)-(Get-CimInstance Win32_OperatingSystem)"
             ".LastBootUpTime).TotalHours"],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        h = (r.stdout or "").strip()
        if h.isdigit():
            h = int(h)
            lines.append(f"已开机：{h // 24} 天 {h % 24} 小时" if h >= 24
                         else f"已开机：{h} 小时")
    except (subprocess.SubprocessError, OSError, ValueError):
        pass

    return "\n".join(lines)


def web_search(query: str, kind: str = "text", max_results: int = 5) -> str:
    """联网搜索。需要时效性信息时用。kind 可以是 text 或 news。"""
    try:
        import tools
        return tools.as_prompt_block(query, int(max_results), kind)
    except Exception as e:
        return f"搜索失败：{e}"


def recall(query: str, limit: int = 8) -> str:
    """检索记忆库。想知道"用户以前说过什么"时用。"""
    try:
        import thinking as T
        options = T.apply_to_memory(query=query)
        hits = M.retrieve(query, top_k=int(limit), retrieval=options["retrieval"])
        if not hits:
            return "（没找到相关记忆）"
        return "\n".join(
            f"- [{M.parse_ts(e['ts']):%m月%d日}] {e['text']} "
            f"{'★' * e.get('importance', 3)}"
            for e in hits)
    except Exception as e:
        return f"检索失败：{e}"


def remember(text: str, importance: int = 3, tags: str = "",
             decay: str = "normal", speaker: str = "owner") -> str:
    """
    把值得长期记住的事写进记忆库。
    重要性：1=琐事 3=一般 5=很重要。decay: permanent/slow/normal。

    speaker: "owner"（主人说的，桌面宠物默认）或 "guest"（群里其他人说的）。
    外人记忆会自动降权、封顶重要度、加速衰减，而且不会被写进用户档案。
    """
    try:
        tl = [t.strip() for t in (tags or "").split(",") if t.strip()]
        e = M.add(text, int(importance), tl, "", decay, "tool", speaker=speaker)
        if not e:
            return "未记录（内容为空或触发隐私过滤）"
        tag = "" if e.get("speaker") == "owner" else "［标为群友记忆］"
        return f"已记住：{e['text']}{tag}"
    except Exception as e:
        return f"写入失败：{e}"


def mood(action: str = "get", key: str = "", hours: float = 0,
         why: str = "", force: bool = False) -> str:
    """
    心理点：挑一个状态停一会儿，到点自动散。
    action: get 看现在的 / list 看有哪些 / set 挑一个 / clear 提前收。

    force=True 跳过冷却——主人明确开口要换的时候用。
    """
    try:
        import mood as MD
        a = (action or "get").lower()
        if a == "list":
            return "\n".join(f"· {k}（{v['hours']}h）—— {v.get('feel','')}"
                             for k, v in MD.catalog().items())
        if a == "clear":
            return "散了。" if MD.clear("她自己收的") else "本来就没停在哪。"
        if a == "log":
            # ★ 每轮记一笔要走这儿，别拿 fs_write 自己拼 JSON。
            #   实测踩过：SOUL.md 里那句"追加进 data/mood_log.jsonl"
            #   没有配套工具，她就自己拼了一段写进沙箱里的 data\ ——
            #   沙箱路径和 mood.py 真正用的 AIPet\data\ 不是一个地方，
            #   字段名也对不上（time/mood vs ts/key），等于白记。
            row = MD.write_log(why or "", source="她自己")
            return f"记下了：{row['key'] or '平常'}"
        if a == "set":
            if not key:
                return "要给一个 key，先用 action=list 看。"
            try:
                e = MD.set_mood(key, hours or None, why, force=force)
            except ValueError as exc:
                return f"没设成：{exc}"
            return f"已停在「{e['key']}」，{M.parse_ts(e['until']):%H:%M} 左右自己散。"
        return MD.status()
    except Exception as e:
        return f"心理点读写失败：{e}"


# ═══════════════════════════════════════════════════════════════
#  文件读写（沙箱）
# ═══════════════════════════════════════════════════════════════

# 沙箱根 —— 她能读写的那一个目录。想换成别处（或者换回自己的目录），
# 在 data/config.json 里设 tools.fs_root，写绝对路径。
#
# 默认值放在用户主目录下，不写死任何盘符：写死的话那是作者本机的目录结构，
# 对别人没意义，还会被提交进公开仓库。
DEFAULT_FS_ROOT = Path.home() / "AIPet"

# 走 docx_read 解析的扩展名。别的都是当纯文本读。
# ★ .doc 也列在这儿：它解不出内容，但要是不列，就会被当纯文本读成
#   一堆乱码 —— 那比明说"这是老格式，另存为 .docx"糟糕得多。
DOCX_SUFFIX = {".docx", ".doc"}

# 单次读写的上限。不设的话，读一个 100MB 的日志会直接把 prompt 撑爆 ——
# 而且模型看不出"这是因为太大"，只会开始胡编。
FS_READ_MAX = 200_000
FS_WRITE_MAX = 200_000


def _fs_root() -> Path:
    """沙箱根。允许被 config 覆盖，读不到就用默认值。"""
    # 后台代码任务有自己的工作目录。它优先于全局 tools.fs_root，
    # 这样同一时间跑多个任务时不会互相读写。
    bound = tool_context().get("fs_root")
    if bound:
        try:
            return Path(bound).expanduser().resolve()
        except (OSError, RuntimeError):
            pass
    p = M.ROOT / "data" / "config.json"
    try:
        r = (json.loads(p.read_text(encoding="utf-8")).get("tools") or {}).get("fs_root")
        if r:
            return Path(str(r)).expanduser().resolve()
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    try:
        return DEFAULT_FS_ROOT.expanduser().resolve()
    except OSError:
        return DEFAULT_FS_ROOT


def _sandbox(rel: str) -> tuple[Path | None, str]:
    """
    把模型给的路径收进沙箱。收不进去就返回 (None, 原因)。

    ★ 三道闸，缺一不可：
      1. 他很可能照着用户的话写成 "<沙箱根的名字>\\讲" —— 先剥掉这层前缀
         当相对路径，而不是直接拒。拒了他会换个写法接着试，很吵。
      2. resolve() 之后必须仍在沙箱里 —— 这一步同时挡住 ".." 和符号链接。
      3. 空字符串 = 沙箱根。
    """
    root = _fs_root()
    raw = (rel or "").strip().strip("\"'")
    s = raw.replace("\\", "/")
    if s in ("", ".", "/"):
        return root, ""

    # ★ 带盘符的绝对路径：只收落在沙箱里的，不在就明确拒。
    #   别把它剥成相对路径 —— 那样"存到 D:\其他目录"会悄悄变成
    #   "存到沙箱里"，用户按他说的路径去找，什么都找不到。
    if re.match(r"^[A-Za-z]:", s):
        try:
            p = Path(raw).resolve()
        except (OSError, RuntimeError) as e:
            return None, f"这个路径解析不了：{e}"
        if p != root and root not in p.parents:
            return None, f"越界了。我只能动 {root} 里面的东西，碰不到 {rel}。"
        return p, ""

    # 无盘符：当成沙箱内的相对路径。
    # 模型很可能照抄用户的话，写成 "<沙箱根的名字>/xxx" —— 把那层前缀剥掉。
    # 前缀跟着沙箱根走，不写死某个目录名：换个根它自己就跟着换。
    s = s.lstrip("/")
    pre = root.name + "/"
    if pre != "/" and s.lower().startswith(pre.lower()):
        s = s[len(pre):]

    try:
        target = (root / s).resolve()
    except (OSError, RuntimeError) as e:
        return None, f"这个路径解析不了：{e}"

    if target != root and root not in target.parents:
        return None, f"越界了。我只能动 {root} 里面的东西，碰不到 {rel}。"
    return target, ""


def fs_list(path: str = "") -> str:
    """列目录。"""
    target, err = _sandbox(path)
    if err:
        return err
    if not target.exists():
        return f"没有这个路径：{path or '（根目录）'}"
    if target.is_file():
        try:
            return f"{target} 是个文件，不是目录（{target.stat().st_size} 字节）"
        except OSError:
            return f"{target} 是个文件"
    try:
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as e:
        return f"列不了：{e}"

    if not items:
        return f"{target} 是空的"
    rows = []
    for p in items[:100]:
        try:
            rows.append(f"  {p.name}/" if p.is_dir() else f"  {p.name}  ({p.stat().st_size} 字节)")
        except OSError:
            rows.append(f"  {p.name}")
    tail = f"\n（还有 {len(items) - 100} 项没列出来）" if len(items) > 100 else ""
    return f"{target}（{len(items)} 项）：\n" + "\n".join(rows) + tail


def fs_read(path: str) -> str:
    """
    读文件。纯文本直接读，docx 走 docx_read（含里面的图）。

    ★ docx 要在大小检查**之前**处理：压缩包本身可能只有几十 KB，
      解出来的文字却能撑爆 prompt —— 卡文件大小没意义，卡的是解出来的东西。
      docx_read 自己按 MAX_CHARS 截。
    """
    target, err = _sandbox(path)
    if err:
        return err
    if not target.exists():
        return f"没有这个文件：{path}"
    if target.is_dir():
        return f"{path} 是目录，要看里面有什么该用 fs_list"

    if target.suffix.lower() in DOCX_SUFFIX:
        try:
            import docx_read
            return docx_read.as_prompt_block(target)
        except Exception as e:                       # noqa: BLE001
            return f"读 docx 失败：{type(e).__name__}: {e}"

    try:
        n = target.stat().st_size
        if n > FS_READ_MAX:
            return (f"{path} 有 {n} 字节，超过单次读取上限 {FS_READ_MAX}。"
                    f"告诉我你想从里面找什么，或者换个小的。")
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"读不了：{e}"
    return f"{path}（{n} 字节）：\n{text}"


def fs_write(path: str, content: str = "", append: bool = False) -> str:
    """写文本文件。父目录不存在会自动建。"""
    target, err = _sandbox(path)
    if err:
        return err
    if target.is_dir():
        return f"{path} 是目录，不能当文件写"
    data = content or ""
    size = len(data.encode("utf-8"))
    if size > FS_WRITE_MAX:
        return f"内容有 {size} 字节，超过单次写入上限 {FS_WRITE_MAX}。分几次写。"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if append else "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
    except OSError as e:
        return f"写不了：{e}"
    return f"已{'追加到' if append else '写入'} {target}（{size} 字节）"


def fs_mkdir(path: str) -> str:
    """建目录。"""
    target, err = _sandbox(path)
    if err:
        return err
    if target.exists():
        return f"已经有了：{target}"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"建不了：{e}"
    return f"已建 {target}"


# ═══════════════════════════════════════════════════════════════
#  本地代码执行
# ═══════════════════════════════════════════════════════════════

RUN_CODE_MAX = 120_000
RUN_CODE_TIMEOUT = 180
RUN_OUTPUT_MAX = 6_000


def _snapshot_files(root: Path) -> dict[str, tuple[int, int]]:
    """返回任务目录的轻量快照，供 run_python 报告实际产物。"""
    out: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return out
    try:
        items = root.rglob("*")
    except OSError:
        return out
    for p in items:
        try:
            if not p.is_file() or ".aipet_runtime" in p.parts:
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            st = p.stat()
            out[rel] = (int(st.st_size), int(st.st_mtime_ns))
        except (OSError, ValueError):
            continue
    return out


def _changed_files(before: dict, after: dict) -> list[str]:
    return sorted(k for k, v in after.items()
                  if before.get(k) != v)[:100]


def run_python(code: str, timeout: int = 120) -> str:
    """
    在当前代码任务目录运行一段 Python，并返回真实 stdout/stderr 与产物。

    这是第一阶段的执行器：只给后台代码任务使用，脚本工作目录已经绑定到
    该任务的沙箱。不会把代码任务工具暴露给普通访客，也不会把 API key
    自动注入子进程环境。
    """
    ctx = tool_context()
    root = _fs_root()
    if not ctx.get("task_id"):
        return "拒绝执行：run_python 只能由后台代码任务调用。"
    code = str(code or "")
    if not code.strip():
        return "没有可执行的 Python 代码。"
    if len(code.encode("utf-8")) > RUN_CODE_MAX:
        return f"代码超过 {RUN_CODE_MAX} 字节上限，请拆成几步。"
    try:
        root.mkdir(parents=True, exist_ok=True)
        runtime = root / ".aipet_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        script = runtime / f"run_{uuid.uuid4().hex[:12]}.py"
        script.write_text(code, encoding="utf-8", newline="\n")
    except OSError as e:
        return f"准备运行目录失败：{e}"

    try:
        before = _snapshot_files(root)
        try:
            limit = max(1, min(int(timeout or 120), RUN_CODE_TIMEOUT))
        except (TypeError, ValueError):
            limit = 120
        env = dict(os.environ)
        # 任务脚本需要正常的 Python/系统环境，但不应因为子进程继承环境就
        # 看到宿主机里可能存在的 API key、密码或 token。
        for key in list(env):
            upper = key.upper()
            if any(word in upper for word in
                   ("API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "PRIVATE_KEY")):
                env.pop(key, None)
        env["PYTHONUTF8"] = "1"
        env.pop("PYTHONSTARTUP", None)
        try:
            p = subprocess.run(
                [sys.executable, str(script)], cwd=str(root), env=env,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=limit,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = (p.stdout or "")[-RUN_OUTPUT_MAX:]
            err = (p.stderr or "")[-RUN_OUTPUT_MAX:]
            code_line = f"退出码：{p.returncode}"
        except subprocess.TimeoutExpired as e:
            out = str(e.stdout or "")[-RUN_OUTPUT_MAX:]
            err = (str(e.stderr or "") + f"\n超过 {limit} 秒，已终止")[-RUN_OUTPUT_MAX:]
            code_line = "退出码：超时"
        except OSError as e:
            out, err, code_line = "", str(e), "退出码：启动失败"
        after = _snapshot_files(root)
        changed = _changed_files(before, after)
        rows = [code_line]
        if out.strip():
            rows.append("stdout：\n" + out.strip())
        if err.strip():
            rows.append("stderr：\n" + err.strip())
        if changed:
            rows.append("任务目录中新增或修改的文件：\n" + "\n".join(f"- {x}" for x in changed))
        else:
            rows.append("任务目录没有发现新增或修改的文件。")
        return "\n\n".join(rows)
    finally:
        try:
            script.unlink()
        except OSError:
            pass


def code_task(action: str = "start", instruction: str = "",
              task_id: str = "") -> str:
    """创建或控制一个后台本地代码任务；只允许可信主人上下文调用。"""
    ctx = tool_context()
    trusted_local = ctx.get("source") == "local" and bool(ctx.get("is_owner"))
    trusted_qq = ctx.get("source") == "qq" and bool(ctx.get("is_owner")) \
        and ctx.get("event") is not None
    if not (trusted_local or trusted_qq):
        return "当前对话没有可用的可信主人任务上下文。"
    try:
        from code_tasks import manager
        tm = manager()
        action = (action or "start").strip().lower()
        if action in {"start", "new", "create"}:
            if not str(instruction or "").strip():
                event = ctx.get("event")
                instruction = str(getattr(event, "content", "") or "")
                if not instruction:
                    instruction = str(ctx.get("query") or "")
            return tm.submit(ctx, instruction)
        if action in {"continue", "resume", "next"}:
            return tm.continue_task(ctx, instruction, task_id)
        if action in {"status", "get"}:
            return tm.status(ctx, task_id)
        if action in {"cancel", "stop"}:
            return tm.cancel(ctx, task_id)
        return "未知任务操作。可用：start、continue、status、cancel。"
    except Exception as e:  # noqa: BLE001
        return f"任务调度失败：{type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════
#  看图
# ═══════════════════════════════════════════════════════════════

def keep_image(name: str, dest: str = "") -> str:
    """
    把 QQ 中转目录里的图**复制**一份到沙箱，长期留着。

    ★ 是复制不是移动，这是有意的：中转目录是「临时收件箱」，
      它自己的那份 7 天后照删；沙箱里的这份副本归用户管，
      清理只扫中转目录，碰不到它。
      两边各管各的，谁也不影响谁。

    ★ 为什么需要它：QQ 发来的图存在 data/qq_media/，7 天后自动删。
      用户说「把这张图存到 xxx」时，光靠 fs_write 做不到 ——
      它只能写文本，存不了二进制。
    """
    import re as _re

    src_dir = M.ROOT / "data" / "qq_media"
    want = (name or "").strip().strip("\"'")
    if not want:
        return "要给一个文件名。"

    # 允许给完整文件名，也允许只给后半截（她看到的可能是我拼的那串）
    cands = []
    if src_dir.is_dir():
        for f in src_dir.iterdir():
            if f.is_file() and (f.name == want or f.name.endswith(want)
                                or want in f.name):
                cands.append(f)
    if not cands:
        return (f"中转目录里没有叫「{want}」的图。"
                f"可能已经过期清掉了，或者名字记错了 —— 用 fs_list 看看 "
                f"data/qq_media 里有什么。")

    src = max(cands, key=lambda p: p.stat().st_mtime)
    safe = _re.sub(r"[^\w.\-]", "_", src.name)[:80]
    target, err = _sandbox(dest or f"QQ图片/{safe}")
    if err:
        return err
    if target.is_dir():
        target = target / safe
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    except OSError as e:
        return f"存不过去：{e}"
    return f"存好了：{target}（{target.stat().st_size} 字节）"


def see_image(path: str, question: str = "") -> str:
    """
    读一张图。走 vision.py 配的那个视觉接口，不是她自己的脑子 ——
    DeepSeek 是纯文本的，图进去她只能干看着。

    ★ **不设沙箱**：图可以在这台电脑的任何一个地方。理由是从对话窗拖进来的
      文件本来就在各处（桌面、下载、截图文件夹），限制在沙箱里等于
      让拖图残废一半。

      两道闸门替它把关：
        1. vision.read 有后缀白名单 —— 不是图片一律拒，绝不上传。
           （这条最要紧：它会把文件整份 base64 发到校外服务器，
             没有它，see_image("secrets.json") 读不出内容但密钥已经出门了）
        2. QQ 侧对非主人整个屏蔽掉这个工具，见 brain.build_payload 的 block。
    """
    import vision as V
    raw = (path or "").strip().strip("\"'")
    if not raw:
        return "要给一个图片路径。"
    return V.read(Path(raw).expanduser(), question)


# ═══════════════════════════════════════════════════════════════
#  工具定义（OpenAI / DeepSeek 格式）
# ═══════════════════════════════════════════════════════════════

SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前日期、星期和时间。任何需要知道'现在几点''今天几号'"
                           "的场合都必须调它——你无法凭记忆知道当前时间。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system",
            "description": "获取用户机器的状态：电量、开机时长、系统版本。"
                           "用户问电量、问电脑情况时用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索最新信息。涉及新闻、时事、你不确定的事实、"
                           "或训练数据之后才发生的事时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "kind": {"type": "string", "enum": ["text", "news"],
                             "description": "text 普通搜索，news 新闻"},
                    "max_results": {"type": "integer",
                                    "description": "返回几条，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "code_task",
            "description":
                "把明确要求写程序、运行代码、生成图片/报告/文件或反复调试的工作"
                "交给后台本地代码任务。只在用户确实要一个可交付产物时调用；"
                "普通问答、解释代码或闲聊不要调用。任务会在独立目录里运行，"
                "完成后主动回到当前本地窗口或 QQ 会话。action=start 创建新任务，continue 继续最近任务，"
                "status 查询状态，cancel 取消任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["start", "continue", "status", "cancel"],
                               "description": "默认 start"},
                    "instruction": {"type": "string",
                                    "description": "要执行或继续执行的具体要求"},
                    "task_id": {"type": "string",
                                "description": "可选任务编号；省略时使用当前会话最近任务"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description":
                "在当前后台代码任务的独立目录运行 Python，返回真实 stdout/stderr"
                "和新增或修改的文件。只用于 code_task 已创建的实际产物任务，"
                "不要在普通聊天里调用，也不要把 API key 写进代码或输出。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要运行的 Python 代码"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 180,
                                "description": "超时秒数，默认 120"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "检索长期记忆库，查用户以前说过什么、答应过什么。"
                           "涉及用户个人情况、历史对话时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "limit": {"type": "integer", "description": "最多几条，默认 8"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "把用户提到的、值得长期记住的事写进记忆库。"
                           "适合记：事实、承诺、偏好、忌讳、重要日期。"
                           "不适合记：闲聊、一次性的琐事。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "要记的内容，写成陈述句，如'用户不喜欢被催'"},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 5,
                                   "description": "1=琐事 3=一般 5=很重要"},
                    "tags": {"type": "string", "description": "标签，逗号分隔"},
                    "decay": {"type": "string",
                              "enum": ["permanent", "slow", "normal"],
                              "description": "permanent 永不遗忘（生日）；"
                                             "slow 长期（目标偏好）；normal 日常"},
                    "speaker": {"type": "string",
                                "enum": ["owner", "guest"],
                                "description":
                                    "说话人。**默认 owner**（你的主人）。"
                                    "在群聊里，如果这句话是群里其他人说的，"
                                    "必须传 guest ——否则会把别人的事"
                                    "记成你主人的，那是错的，不只是权重低。"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mood",
            "description":
                "你的「心理点」——你自己挑一个状态停一会儿，到点自动散，"
                "不用手动恢复。这是外套不是内核：只改默认语气，不改判断力，"
                "该冷该硬该较真时随时翻得回去。"
                "action=get 看现在停在哪儿；list 看有哪些；set 挑一个；clear 提前收。"
                "★ 由你自己决定要不要挑，被什么真触到了才挑，不要每轮都换。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["get", "list", "set", "clear", "log"],
                               "description": "默认 get。log = 每轮回完记一笔"
                                              "（由头写在 why 里，见 SOUL.md 那条规矩）"},
                    "key": {"type": "string",
                            "description":
                                "状态名：起雾 / 软毛 / 低电量 / 手痒 / 较真 / 偏心 / 走神"},
                    "hours": {"type": "number",
                              "description": "停多久，默认按该状态的常规时长（1-8 小时）"},
                    "why": {"type": "string",
                            "description": "由头——什么让你想停在这儿"},
                    "force": {
                        "type": "boolean",
                        "description":
                            "跳过冷却和每日上限。只在主人明确开口要求换的时候用"
                            "（'可爱一点''正常点''收一收'这类）。他有权。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_list",
            "description":
                f"列出本地沙箱目录里的内容。用户让你看看某个文件夹有什么、"
                f"或者你不确定东西放哪了时用。只能看 {_fs_root()} 里面的。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "相对路径，比如 '讲' 或 '讲/照片'。留空 = 根目录"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_read",
            "description":
                f"读本地的一个文本文件。用户让你看看文件里写了什么时用。"
                f"只能读 {_fs_root()} 里面的。"
                f"纯文本、代码、Markdown 都能读；**.docx 也能读** —— "
                f"正文和里面的图片都会转成文字。老的 .doc 读不了，要对方另存为 .docx。"
                f"真正的图片（.png/.jpg）不要拿它试，那个用 see_image。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_write",
            "description":
                f"把文字写进本地的文本文件。用户让你记录、整理、保存内容时用。"
                f"父目录不存在会自动建。只能写 {_fs_root()} 里面的。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "相对路径，比如 '讲/笔记.md'"},
                    "content": {"type": "string", "description": "要写入的正文"},
                    "append": {"type": "boolean",
                               "description": "true = 追加到文件末尾；默认 false = 覆盖"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_mkdir",
            "description": f"在沙箱里建一个目录。只能建在 {_fs_root()} 里面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keep_image",
            "description":
                "把 QQ 发来的图存进本地，不让它过期消失。"
                "QQ 收到的图放在中转目录里，7 天后自动删 —— 用户说"
                "「这张图存下来」「放到某个文件夹」「留着以后看」时必须调它，"
                "光用 fs_write 存不了图（那只能写文字）。"
                "不确定文件的准确名字就先 fs_list 看一眼 data/qq_media。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "中转目录里的文件名，可以只给后半截"},
                    "dest": {"type": "string",
                             "description":
                                 "存到哪儿（沙箱内相对路径）。"
                                 "留空就放 QQ图片/ 下，文件名照旧"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "see_image",
            "description":
                "看一张图。你自己是纯文本的，图里有什么必须调它才知道——"
                "不要靠猜，也不要根据文件名编内容。"
                "用户让你看某个截图/照片时用。"
                "路径可以在这台电脑的任何地方（桌面、下载、截图文件夹都行）。"
                "★ 但只能看图片：别拿它去读 .json / .md / .txt 这类文件，"
                "那不是它的用途，读了也只会被拒。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "图片的相对路径，比如 '截图/a.png'"},
                    "question": {"type": "string",
                                 "description":
                                     "想问关于这张图的什么。留空就是「把图里的内容读出来」"},
                },
                "required": ["path"],
            },
        },
    },
]

from companion import SPECS as COMPANION_SPECS, tool_call as companion_tool_call
SPECS.extend(COMPANION_SPECS)

DISPATCH = {
    "agreement": lambda a: companion_tool_call("agreement", a),
    "quiet_company": lambda a: companion_tool_call("quiet_company", a),
    "mood": lambda a: mood(a.get("action", "get"), a.get("key", ""),
                           a.get("hours", 0), a.get("why", ""),
                           bool(a.get("force", False))),
    "get_time": lambda a: get_time(),
    "get_system": lambda a: get_system(),
    "web_search": lambda a: web_search(a.get("query", ""), a.get("kind", "text"),
                                       a.get("max_results", 5)),
    "code_task": lambda a: code_task(a.get("action", "start"),
                                      a.get("instruction", ""),
                                      a.get("task_id", "")),
    "run_python": lambda a: run_python(a.get("code", ""), a.get("timeout", 120)),
    "recall": lambda a: recall(a.get("query", ""), a.get("limit", 8)),
    "remember": lambda a: remember(a.get("text", ""), a.get("importance", 3),
                                   a.get("tags", ""), a.get("decay", "normal"),
                                   a.get("speaker", "owner")),
    "fs_list": lambda a: fs_list(a.get("path", "")),
    "fs_read": lambda a: fs_read(a.get("path", "")),
    "fs_write": lambda a: fs_write(a.get("path", ""), a.get("content", ""),
                                    bool(a.get("append", False))),
    "fs_mkdir": lambda a: fs_mkdir(a.get("path", "")),
    "see_image": lambda a: see_image(a.get("path", ""), a.get("question", "")),
    "keep_image": lambda a: keep_image(a.get("name", ""), a.get("dest", "")),
}


def call(name: str, args: dict, *, policy: ToolPolicy | None = None,
         allowed_tools: set[str] | None = None) -> str:
    policy = policy or tool_context().get("tool_policy")
    if policy is None and allowed_tools is not None:
        policy = make_policy(allowed_tools=allowed_tools)
    if policy is not None and not policy.allows(name):
        return f"工具未获本轮授权：{name}"
    fn = DISPATCH.get(name)
    if fn is None:
        return f"未知工具：{name}"
    try:
        return fn(args or {})
    except Exception as e:
        return f"工具执行失败：{type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════
#  自测
# ═══════════════════════════════════════════════════════════════

def selftest(only: str | None = None) -> int:
    fails = 0
    for name, fn in DISPATCH.items():
        if only and name != only:
            continue
        if name in ("code_task", "run_python"):
            print(f"  — {name:<12} 需要可信主人代码任务上下文，跳过离线自测")
            continue
        try:
            out = fn({"query": "测试"} if name in ("web_search", "recall") else {})
            # 联网工具这里要测的是"后端抽风时会不会优雅降级"，不是"必须搜到东西"。
            # 拿"测试"两个字去搜，DDGS 本来就常常返回空 —— 那是正常结果，
            # 不是失败。之前这条会随机变红，就是这么来的。
            ok = bool(out) and ("失败" not in out[:20] or "搜索无结果" in out)
            print(f"  {'✓' if ok else '✗'} {name:<12} {out.splitlines()[0][:64]}")
            if not ok:
                fails += 1
        except Exception as e:
            print(f"  ✗ {name:<12} {type(e).__name__}: {e}")
            fails += 1
    return fails


if __name__ == "__main__":
    print("本地工具自测\n")
    n = selftest(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"\n{'全部通过' if n == 0 else str(n) + ' 项失败'}")
    sys.exit(1 if n else 0)
