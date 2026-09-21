#!/usr/bin/env python3
"""
pet_ctl.py —— 从外面启停桌宠

平时用不着：右键桌宠 → 退出 就够了。这个是**退不掉的时候用的**。

什么时候会退不掉：

  - 她正卡在一次网络请求里，而取消信号要等 socket 超时（30 秒）才生效
    （`quit_safely` 现在有 5 秒上限兜底，正常不会再卡）
  - 鼠标穿透开着，窗口点不到；托盘图标又被 Windows 11 收进了折叠区
  - 事件循环本身僵住 —— 这种情况下程序内的任何兜底都跑不起来，
    只能从外面杀

PID 从 `data/desktop.lock` 读（Qt 的 QLockFile 写的）。正常退出会删掉它，
被强杀则会留下 —— 留着的那个下次启动会被 Qt 判定为失效锁，不影响启动。

用法：
    python tools/pet_ctl.py status
    python tools/pet_ctl.py stop
    python tools/pet_ctl.py selftest
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "data" / "desktop.lock"

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def read_pid() -> int:
    """锁文件里的 PID。读不到返回 0。"""
    try:
        return int(LOCK.read_text(encoding="utf-8", errors="replace").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return 0


def alive(pid: int) -> bool:
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


def status() -> int:
    pid = read_pid()
    if not pid:
        print("桌宠      没在跑")
        return 1
    if alive(pid):
        print(f"桌宠      在跑（PID {pid}）")
        return 0
    print(f"桌宠      没在跑，但留下了锁文件（PID {pid} 已经不在了）")
    print("          不影响下次启动，也可以 python tools/pet_ctl.py stop 清掉")
    return 1


def stop() -> int:
    pid = read_pid()
    if not pid:
        print("桌宠没在跑。")
        return 0

    if alive(pid):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (subprocess.SubprocessError, OSError) as e:
            print(f"杀不掉：{e}")
            return 1
        if alive(pid):
            print(f"发过指令了，但 PID {pid} 还在。再跑一次，或者看看是不是权限问题。")
            return 1
        print(f"桌宠停了（PID {pid}）。")
    else:
        print(f"进程已经不在了（PID {pid}），只清掉锁文件。")

    # ★ 锁文件要清掉：留着的话下次启动 Qt 会拿它判断"已经在跑"。
    #   正常情况下 QLockFile 能识别失效锁自己接管，但那是它的事，
    #   我们从外面杀的这一刀它未必知道，别赌。
    try:
        LOCK.unlink()
        print("锁文件已清。")
    except OSError:
        pass
    return 0


def selftest() -> int:
    """
    自己造假数据验，**不看这台机器上桌宠在不在跑** ——
    依赖环境的自检，换台机器就红，那种红没人会去查。
    """
    import os
    import tempfile

    fails = 0

    def check(label: str, cond: bool, extra: str = ""):
        nonlocal fails
        print(f"  {'[OK]' if cond else '[!!]'} {label}" + (f"   {extra}" if extra else ""))
        if not cond:
            fails += 1

    print("桌宠控制自检\n")

    tmp = Path(tempfile.mkdtemp())
    real = LOCK

    def with_lock(text: str | None) -> None:
        p = tmp / "desktop.lock"
        if text is None:
            if p.exists():
                p.unlink()
        else:
            p.write_text(text, encoding="utf-8")
        globals()["LOCK"] = p

    try:
        with_lock(None)
        check("没有锁文件 → read_pid 返回 0", read_pid() == 0)

        with_lock("不是数字\n")
        check("锁文件内容不是数字 → 也返回 0，不抛异常", read_pid() == 0)

        with_lock("")
        check("锁文件是空的 → 返回 0", read_pid() == 0)

        with_lock(f"{os.getpid()}\npython\nHOST\nuuid\n")
        check("读得出 PID", read_pid() == os.getpid(), f"PID {read_pid()}")
        check("当前进程判为活着", alive(os.getpid()) is True)

        with_lock("999999\npython\nHOST\nuuid\n")
        check("不存在的 PID 判为不在", alive(999999) is False)
        check("PID 0 直接返回假，不去 tasklist", alive(0) is False)

        # stop 在进程已经不在时应该只清锁、不报错
        globals()["LOCK"] = tmp / "desktop.lock"
        (tmp / "desktop.lock").write_text("999999\n", encoding="utf-8")
        rc = stop()
        check("进程已死时 stop 返回 0", rc == 0)
        check("stop 把残留锁清掉了", not (tmp / "desktop.lock").exists())
    finally:
        globals()["LOCK"] = real

    print()
    print("全部通过" if not fails else f"{fails} 项未通过")
    return fails


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "status":
        sys.exit(status())
    if cmd == "stop":
        sys.exit(stop())
    if cmd == "selftest":
        sys.exit(selftest())
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
