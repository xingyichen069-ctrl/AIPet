#!/usr/bin/env python3
"""
qq_ctl.py —— 启停 QQ 桥，装/卸开机自启

═══════════════════════════════════════════════════════════════
  为什么需要一个控制脚本
═══════════════════════════════════════════════════════════════

开机自启是**无窗口**跑的（pythonw.exe），没有黑框可以关。
进程名是 pythonw.exe，任务管理器里也认不出哪个是它。
所以停它必须靠 PID 文件 —— 这个脚本就是读那个文件干活的。

═══════════════════════════════════════════════════════════════
  自启放在哪
═══════════════════════════════════════════════════════════════

用**启动文件夹**，不用注册表：

    %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup

理由是你随时能在「启动」里看见它、右键删掉。注册表项是隐形的，
过半年你自己都不记得装过什么。

═══ 用法 ═══
    python tools/qq_ctl.py status       # 现在什么状态
    python tools/qq_ctl.py start        # 后台跑（无窗口）
    python tools/qq_ctl.py stop         # 停
    python tools/qq_ctl.py restart
    python tools/qq_ctl.py install      # 装开机自启
    python tools/qq_ctl.py uninstall    # 卸掉
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import qq_bot as QB  # noqa: E402

STARTUP = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
           / "Start Menu" / "Programs" / "Startup")
LNK = STARTUP / "小日和 QQ.lnk"
BRIDGE = ROOT / "src" / "qq_bridge.py"
LOG = ROOT / "data" / "qq.log"


# ★ 解释器可能在两个地方。0.3.x 的 Windows 便携版自带 runtime/，
#   源码版才用 .venv/。两边都要认，否则装到便携版上会去找一个不存在的 .venv，
#   然后悄悄退回系统 Python —— 那个多半没装依赖，QQ 起不来还不报错。
_CANDIDATES = (
    ("runtime", "pythonw.exe"), ("runtime", "python.exe"),
    (".venv/Scripts", "pythonw.exe"), (".venv/Scripts", "python.exe"),
    ("runtime", "pythonw"), ("runtime", "python"),
    (".venv/bin", "pythonw"), (".venv/bin", "python"),
)


def _python() -> Path:
    """优先用项目自带的 pythonw（无窗口）。"""
    for sub, exe in _CANDIDATES:
        p = ROOT / sub / exe
        if p.exists():
            return p
    return Path(sys.executable)


def _pythonc() -> Path:
    """带控制台的那个，用于 --visible 和需要看输出的场合。"""
    for sub, exe in _CANDIDATES:
        if exe == "python.exe":
            p = ROOT / sub / exe
            if p.exists():
                return p
    return Path(sys.executable)


def status() -> int:
    pid = QB.read_pid()
    st = QB.read_status()
    print("QQ 桥")
    if pid:
        print(f"  进程     在跑（PID {pid}）")
    else:
        print("  进程     没在跑")
    print(f"  网关     {st.get('state')}"
          f"{('　' + st['detail']) if st.get('detail') else ''}")
    if st.get("fresh"):
        print(f"  最后活动 {st.get('age', 0):.0f} 秒前")
    print(f"  自启      {'已装' if LNK.exists() else '没装'}"
          + (f"　{LNK}" if LNK.exists() else ""))
    print(f"  日志      {LOG}")
    return 0 if pid else 1


def start(visible: bool = False) -> int:
    if QB.read_pid():
        print("已经在跑了。要重启用 restart。")
        return 0

    py = _pythonc() if visible else _python()
    if visible and os.name == "nt":
        # 可见模式：开一个新窗口，能看日志也能 Ctrl+C
        subprocess.Popen(
            ["cmd", "/c", "start", "小日和 QQ", str(py), str(BRIDGE), "run"],
            cwd=str(ROOT))
    else:
        # ★ DETACHED_PROCESS 是关键。只用 CREATE_NO_WINDOW 的话，
        #   子进程仍然是调用者的子进程 —— 谁调用它谁死了它就跟着死。
        #   自启的意义就是"不依赖别的进程"，所以必须真正脱离。
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        with open(os.devnull, "rb") as nul_in, \
                open(os.devnull, "wb") as nul_out:
            subprocess.Popen(
                [str(py), str(BRIDGE), "run"],
                cwd=str(ROOT), creationflags=flags,
                stdin=nul_in, stdout=nul_out, stderr=nul_out,
                close_fds=True, start_new_session=(os.name != "nt"))

    import time
    for _ in range(20):
        time.sleep(0.5)
        if QB.read_pid():
            break
    pid = QB.read_pid()
    if pid:
        print(f"起来了（PID {pid}）。{'窗口模式' if visible else '后台无窗口'}。")
        print(f"日志：{LOG}")
        return 0
    print("没起来。手动跑一次看报错：")
    print(f"    {_pythonc()} src\\qq_bridge.py run")
    return 1


def stop() -> int:
    pid = QB.read_pid()
    if not pid:
        print("本来就没在跑。")
        return 0
    QB.kill_pid(pid)
    import time
    for _ in range(10):
        time.sleep(0.3)
        if not QB.read_pid():
            break
    try:
        QB.PID_FILE.unlink()
    except OSError:
        pass
    QB.write_status("stopped", "手动停的")
    print(f"停了（PID {pid}）。")
    return 0


def install() -> int:
    if not STARTUP.exists():
        print(f"找不到启动文件夹：{STARTUP}")
        return 1

    icon = ROOT / "assets" / "character.ico"

    # ★ 走临时 .ps1 文件，不用 -Command 拼字符串。
    #   Windows 用户名里可能有单引号（比如 O'Brien），那会把 PowerShell 的
    #   单引号字符串直接截断，报"方法调用中缺少 )"。
    #   文件还能写成 UTF-8 BOM，中文路径也不会乱码。
    def q(s: Path | str) -> str:
        return "'" + str(s).replace("'", "''") + "'"

    lines = [
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(%s)" % q(LNK),
        "$s.TargetPath = %s" % q(_python()),
        # 参数里的路径带空格（装到哪都可能带，不能赌）
        "$s.Arguments = '\"%s\" run'" % BRIDGE,
        "$s.WorkingDirectory = %s" % q(ROOT),
        "$s.Description = '小日和的 QQ 接入'",
    ]
    if icon.exists():
        lines.append("$s.IconLocation = %s" % q(icon))
    lines.append("$s.Save()")

    tmp = ROOT / "data" / "_install_autostart.ps1"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("\n".join(lines), encoding="utf-8-sig")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(tmp)],
            capture_output=True, text=True, timeout=30)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    if r.returncode != 0 or not LNK.exists():
        print(f"建快捷方式失败：{(r.stderr or r.stdout or '')[:250]}")
        return 1
    print(f"已装开机自启 → {LNK}")
    print(f"  跑的是：{_python()} src\\qq_bridge.py run（无窗口）")
    print("  停它用：python tools\\qq_ctl.py stop")
    return 0


def uninstall() -> int:
    if not LNK.exists():
        print("本来就没装。")
        return 0
    LNK.unlink()
    print(f"已卸掉：{LNK}")
    return 0


def main() -> None:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if cmd == "status":
        sys.exit(status())
    elif cmd == "start":
        sys.exit(start(visible="--visible" in sys.argv))
    elif cmd == "stop":
        sys.exit(stop())
    elif cmd == "restart":
        stop()
        sys.exit(start(visible="--visible" in sys.argv))
    elif cmd == "install":
        sys.exit(install())
    elif cmd == "uninstall":
        sys.exit(uninstall())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
