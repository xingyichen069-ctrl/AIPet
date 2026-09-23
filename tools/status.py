#!/usr/bin/env python3
"""
status.py —— 一眼看清桌宠和 QQ 桥是不是活着

给开发时用的。双击 `查看状态.bat` 就能看，不用记命令。

它只**读**，不启停任何东西。想启动/停止用 `tools/qq_ctl.py`。

═══════════════════════════════════════════════════════════════
  它查什么
═══════════════════════════════════════════════════════════════

    桌宠进程    在跑没、PID、跑了多久
    QQ 桥进程   在跑没、PID、跑了多久
    QQ 网关     连上没、最后活动多久前、重连过几次
    进程一致性  状态文件里写的 PID 和实际在跑的 PID 是不是同一个
    代理        git / 搜索走哪条路，通不通
    API key     配了没（只显示头尾几位，不打印完整的）
    自启        开机启动项装了没
    记忆        多少条、最近一条是什么时候

★ 「进程一致性」那条是这儿最值得看的。QQ 桥被杀掉时来不及写"我停了"，
  状态文件会留在那儿，看起来像还连着 —— 实际上早就断了。

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════

    python tools/status.py              # 打一次
    python tools/status.py --watch      # 每 5 秒刷新，Ctrl+C 退出
    python tools/status.py --watch 2    # 改成每 2 秒
    python tools/status.py --log        # 顺便打 QQ 日志最后 15 行
    python tools/status.py --log 40     # 打 40 行
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

LOCK = ROOT / "data" / "desktop.lock"
QQ_LOG = ROOT / "data" / "qq.log"


# ═══════════════════════════════════════════════════════════════
#  探针
# ═══════════════════════════════════════════════════════════════

def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in (r.stdout or "")
    except (subprocess.SubprocessError, OSError):
        return False


def _started(pids: list[int]) -> dict[int, float]:
    """
    一次问出这几个进程的启动时间（epoch 秒）。

    逐个查要开好几个 PowerShell，一次问完快得多。查不到就返回空 dict，
    调用方别把"查不到"当成"没在跑"。
    """
    pids = [p for p in pids if p > 0]
    if not pids:
        return {}
    q = ("Get-CimInstance Win32_Process | Where-Object { "
         + " -or ".join(f"$_.ProcessId -eq {p}" for p in pids)
         + " } | ForEach-Object { \"$($_.ProcessId) $(([DateTimeOffset]$_.CreationDate)"
           ".ToUnixTimeSeconds())\" }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", q],
                           capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (subprocess.SubprocessError, OSError):
        return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].lstrip("-").isdigit():
            out[int(parts[0])] = float(parts[1])
    return out


def _uptime(start: float | None) -> str:
    if not start:
        return ""
    secs = time.time() - start
    if secs < 0:
        return ""
    if secs < 90:
        return f"{secs:.0f} 秒"
    if secs < 5400:
        return f"{secs / 60:.0f} 分钟"
    if secs < 86400 * 2:
        return f"{secs / 3600:.1f} 小时"
    return f"{secs / 86400:.1f} 天"


def _pet() -> dict:
    """
    桌宠那半边。

    PID 从 data/desktop.lock 读 —— 那是 QLockFile 写的，正常退出会删掉。
    被强杀就会留下，所以「文件在但进程死了」是能区分出来的，别混为一谈。
    """
    if not LOCK.exists():
        return {"state": "off", "note": "没在跑"}
    try:
        pid = int(LOCK.read_text(encoding="utf-8", errors="replace").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return {"state": "bad", "note": "锁文件读不出 PID"}
    if not _alive(pid):
        return {"state": "stale", "pid": pid,
                "note": "锁文件还在但进程已经没了（上次没正常退出，不影响下次启动）"}
    return {"state": "on", "pid": pid}


def _qq() -> dict:
    """QQ 桥那半边。状态文件的读法复用 qq_bot，别另写一份。"""
    try:
        import qq_bot as QB
    except Exception as e:
        return {"state": "bad", "note": f"导入 qq_bot 失败：{e}"}

    out: dict = {"state": "off", "note": "没在跑"}

    pid_file = QB.read_pid()
    st = QB.read_status()
    pid_status = st.get("pid")

    if pid_file and _alive(pid_file):
        out.update({"state": "on", "pid": pid_file})
    elif pid_file:
        out.update({"state": "stale", "pid": pid_file,
                    "note": "PID 文件还在但进程已经没了"})
    elif pid_status and _alive(pid_status):
        # PID 文件被清过但状态是新的，也能算在跑
        out.update({"state": "on", "pid": pid_status})

    out["gw"] = st.get("state", "off")
    out["detail"] = st.get("detail", "")
    out["age"] = st.get("age")
    out["fresh"] = bool(st.get("fresh"))
    out["attempts"] = st.get("attempts")
    out["status_pid"] = pid_status

    # ★ 最值得看的一条：状态文件说的 PID 和真正在跑的 PID 对不上，
    #   说明那份状态是上一个进程留下的，别信它。
    if out["state"] == "on" and pid_status and pid_status != out.get("pid"):
        out["mismatch"] = f"状态文件里写的是 PID {pid_status}，实际在跑的是 {out.get('pid')}"

    if out["state"] == "on" and not st.get("fresh"):
        out["stale_status"] = True

    return out


def _proxy() -> str:
    try:
        import proxy as PX
        p = PX.detect()
        return p or "直连（没检测到代理）"
    except Exception as e:
        return f"检测失败：{type(e).__name__}"


def _api_key() -> str:
    """只回显头尾几位 —— 这是要显示在屏幕上的，别把 key 整条打出来。"""
    try:
        import brain as B
        k = B.api_key()
    except Exception as e:
        return f"读不到：{type(e).__name__}"
    if not k:
        return "没配（跑 python src/brain.py setkey sk-xxxx）"
    if len(k) <= 12:
        return "已配"
    return f"已配（{k[:7]}…{k[-4:]}，{B.base_url()}）"


def _autostart() -> str:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return "查不到（没有 APPDATA）"
    startup = (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
               / "Programs" / "Startup")
    hits = []
    if startup.is_dir():
        for p in sorted(startup.glob("*.lnk")):
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            t = raw.decode("latin-1", "ignore").lower()
            if "aipet" in t or "小日和".encode("utf-16-le").decode("latin-1") in t:
                hits.append(p.stem)
    return ("装了：" + "、".join(hits)) if hits else "没装"


def _memory() -> str:
    try:
        import memory as M
        es = M.load_journal()
        if not es:
            return "0 条"
        try:
            last = M.parse_ts(es[-1]["ts"])
            ago = (M.now() - last).total_seconds()
            when = (f"{ago / 60:.0f} 分钟前" if ago < 5400
                    else f"{ago / 3600:.1f} 小时前" if ago < 86400 * 2
                    else f"{ago / 86400:.1f} 天前")
        except Exception:
            when = "时间读不出"
        return f"{len(es)} 条，最近 {when}"
    except Exception as e:
        return f"读不到：{type(e).__name__}"


# ═══════════════════════════════════════════════════════════════
#  渲染
# ═══════════════════════════════════════════════════════════════

MARK = {"on": "[OK]", "off": "[--]", "stale": "[!!]", "bad": "[!!]"}


def render(log_lines: int = 0) -> str:
    pet, qq = _pet(), _qq()
    starts = _started([pet.get("pid", 0), qq.get("pid", 0)])

    lines: list[str] = []
    add = lines.append

    add("")
    add("  小日和 · 运行状态          "
        f"{datetime.now():%Y-%m-%d %H:%M:%S}")
    add("  " + "─" * 58)

    # 桌宠
    if pet["state"] == "on":
        up = _uptime(starts.get(pet["pid"]))
        add(f"  {MARK['on']} 桌宠     在跑     PID {pet['pid']}"
            + (f"    已运行 {up}" if up else ""))
    else:
        add(f"  {MARK.get(pet['state'], '[!!]')} 桌宠     "
            f"{'没在跑' if pet['state'] == 'off' else pet['note']}"
            + (f"（PID {pet.get('pid')}）" if pet.get("pid") else ""))
        if pet["state"] == "off":
            add("              双击 启动桌宠.bat 起来")

    # QQ 桥
    if qq["state"] == "on":
        up = _uptime(starts.get(qq["pid"]))
        add(f"  {MARK['on']} QQ 桥    在跑     PID {qq['pid']}"
            + (f"    已运行 {up}" if up else ""))
        gw = qq.get("gw") or "?"
        tail = f"（{qq['detail']}）" if qq.get("detail") else ""
        add(f"       网关    {gw}{tail}")
        if qq.get("age") is not None and qq.get("fresh"):
            add(f"       最后活动 {qq['age']:.0f} 秒前")
        if qq.get("attempts"):
            add(f"       重连过 {qq['attempts']} 次")
    else:
        add(f"  {MARK.get(qq['state'], '[!!]')} QQ 桥    "
            f"{'没在跑' if qq['state'] == 'off' else qq['note']}"
            + (f"（PID {qq.get('pid')}）" if qq.get("pid") else ""))
        if qq["state"] == "off":
            add("              python tools/qq_ctl.py start 起来")

    # 告警
    if qq.get("mismatch"):
        add("")
        add(f"  [!!] {qq['mismatch']}")
        add("       那份状态是上一个进程留下的，网关那一行别信。")
        add("       重启：python tools/qq_ctl.py restart")
    if qq.get("stale_status"):
        add("")
        add("  [!!] 网关状态超过 3 分钟没更新了 —— 进程活着但可能已经断线。")

    add("")
    add(f"       代理    {_proxy()}")
    add(f"       API key {_api_key()}")
    add(f"       自启    {_autostart()}")
    add(f"       记忆    {_memory()}")

    if log_lines:
        add("")
        add(f"  QQ 日志最后 {log_lines} 行（{QQ_LOG.name}）")
        add("  " + "─" * 58)
        for line in _tail(log_lines):
            add("  " + line)

    add("")
    return "\n".join(lines)


def _tail(n: int) -> list[str]:
    try:
        text = QQ_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [f"读不到日志：{e}"]
    return [l.rstrip() for l in text.splitlines()[-n:]]


# ═══════════════════════════════════════════════════════════════
#  自检
# ═══════════════════════════════════════════════════════════════

def selftest() -> int:
    """
    把「文件在但进程死了」和「PID 对不上」这两条造出来验一遍。

    这两条是这个工具存在的理由 —— 别的几行都是顺带看看，
    它们要是不会触发，脚本就只是个好看的时间戳。
    """
    import tempfile

    fails = 0

    def check(label: str, cond: bool, extra: str = ""):
        nonlocal fails
        print(f"  {'[OK]' if cond else '[!!]'} {label}" + (f"   {extra}" if extra else ""))
        if not cond:
            fails += 1

    print("运行状态自检\n")

    tmpdir = Path(tempfile.mkdtemp())
    real_lock = LOCK

    def write_lock(text: str) -> Path:
        p = tmpdir / "desktop.lock"
        p.write_text(text, encoding="utf-8")
        return p

    try:
        globals()["LOCK"] = tmpdir / "不存在.lock"
        check("桌宠没在跑", _pet()["state"] == "off")

        globals()["LOCK"] = write_lock("999999\npythonw\nHOST\nuuid\n")
        r = _pet()
        check("★ 锁文件在、进程死了 → 认出来是残留",
              r["state"] == "stale" and r["pid"] == 999999)

        globals()["LOCK"] = write_lock(f"{os.getpid()}\npython\nHOST\nuuid\n")
        check("活着的 PID → 算在跑", _pet()["state"] == "on")

        globals()["LOCK"] = write_lock("不是数字\n")
        check("锁文件读不出 PID 也不崩", _pet()["state"] == "bad")

        # QQ 那半边：把 qq_bot 的两个读口换掉，不碰真文件
        try:
            import qq_bot as QB
            real_read_pid, real_read_status = QB.read_pid, QB.read_status
            me = os.getpid()

            QB.read_pid = lambda: me
            QB.read_status = lambda stale_after=180: {
                "state": "ready", "detail": "心跳中", "pid": 424242,
                "at": "2026-01-01T00:00:00+08:00", "fresh": True, "age": 12.0}
            q = _qq()
            check("★ 状态文件的 PID 和实际对不上 → 报出来",
                  bool(q.get("mismatch")) and q["state"] == "on",
                  q.get("mismatch", ""))

            QB.read_status = lambda stale_after=180: {
                "state": "off", "detail": "最后活动 8 分钟前", "pid": me,
                "at": "2026-01-01T00:00:00+08:00", "fresh": False, "age": 480.0}
            q = _qq()
            check("★ 进程活着但状态不新鲜 → 报出来", bool(q.get("stale_status")))

            QB.read_pid = lambda: None
            QB.read_status = lambda stale_after=180: {"state": "off", "fresh": False}
            check("QQ 没在跑", _qq()["state"] == "off")

            QB.read_pid, QB.read_status = real_read_pid, real_read_status
        except ImportError as e:
            check("能 import qq_bot", False, str(e))

        text = render()
        check("能渲染出整页", "运行状态" in text and "QQ 桥" in text)
        check("渲染里不出现完整 key",
              not any(len(w) > 20 and w.startswith("sk-") for w in text.split()))
    finally:
        globals()["LOCK"] = real_lock

    print()
    print("全部通过" if not fails else f"{fails} 项未通过")
    return fails


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def _any_bad() -> bool:
    """有没有需要人管的事。给 --watch 和外部调用判断用。"""
    return _pet()["state"] != "on" or _qq()["state"] != "on"


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "selftest":
        sys.exit(selftest())

    log_lines = 0
    if "--log" in args:
        i = args.index("--log")
        log_lines = 15
        if i + 1 < len(args) and args[i + 1].isdigit():
            log_lines = int(args[i + 1])

    if "--watch" in args:
        i = args.index("--watch")
        every = 5.0
        if i + 1 < len(args) and args[i + 1].replace(".", "", 1).isdigit():
            every = max(1.0, float(args[i + 1]))
        try:
            while True:
                print("\033[2J\033[H", end="")          # 清屏，光标回左上
                print(render(log_lines))
                print(f"  每 {every:g} 秒刷新，Ctrl+C 退出")
                time.sleep(every)
        except KeyboardInterrupt:
            print("\n  停了。")
            return

    print(render(log_lines))
    sys.exit(1 if _any_bad() else 0)


if __name__ == "__main__":
    main()
