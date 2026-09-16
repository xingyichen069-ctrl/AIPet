#!/usr/bin/env python3
"""
vision.py —— 让小日和能看图

═══════════════════════════════════════════════════════════════
  为什么单独一个文件
═══════════════════════════════════════════════════════════════

小日和的脑子（brain.py）走的是 DeepSeek，**那是纯文本的**，
图片进去她只能干看着。这个文件用 SJTU 校园部署的 Qwen 补上这块。

**它不参与对话。** 只干一件事：把图里的东西读成文字，交回给 brain。
所以这里没有人格、没有记忆、没有工具循环——就是个读图函数。

分离的另一个理由：换部署、换模型、换 endpoint，只动这一个文件。

═══════════════════════════════════════════════════════════════
  凭据
═══════════════════════════════════════════════════════════════

data/secrets.json：

    sjtu_api_key         sk-...
    sjtu_base_url        https://models.sjtu.edu.cn/api/v1
    sjtu_vision_model    qwen3.8-27b

没配就是「不会看图」，不是报错——她会照实说自己看不了，
不会编一张图出来（BOUNDARIES.md 那条）。

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════

    python src/vision.py                 # 自检（造一张图，真调一次）
    python src/vision.py 看图 <路径>       # 命令行读一张
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SECRETS = M.ROOT / "data" / "secrets.json"

# 单张图的上限。base64 会膨胀三分之一，太大的图光是编码就卡住，
# 而且多半也不需要那么高的分辨率。
MAX_BYTES = 8 * 1024 * 1024

# 给模型看的图格式。别的扩展名一律按内容猜。
MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}

DEFAULT_QUESTION = (
    "把这张图里的内容读出来。如果图里有文字，逐字抄下来，保留原有的分行；"
    "如果没有文字，就平实地描述画面里有什么。不要评价，不要推测用途。"
)


# ═══════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════

def load_secrets() -> dict:
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def config() -> dict:
    d = load_secrets()
    return {
        "key": d.get("sjtu_api_key", ""),
        "base": (d.get("sjtu_base_url") or "").rstrip("/"),
        "model": d.get("sjtu_vision_model") or "qwen3.8-27b",
    }


def available() -> tuple[bool, str]:
    """能不能看图。第二个返回值是原因，不能看时给她照实说。"""
    c = config()
    if not c["base"] or not c["key"]:
        return False, "没配读图的凭据（data/secrets.json 里缺 sjtu_api_key / sjtu_base_url）"
    return True, ""


# ═══════════════════════════════════════════════════════════════
#  读图
# ═══════════════════════════════════════════════════════════════

_MAGIC = (
    b"\x89PNG",          # png
    b"\xff\xd8\xff",     # jpeg
    b"GIF8",             # gif
    b"BM",               # bmp
    b"RIFF",             # webp（RIFF....WEBP）
)


def _looks_like_image(head: bytes) -> bool:
    return any(head.startswith(m) for m in _MAGIC)


def _data_url(p: Path) -> str:
    mime = MIME.get(p.suffix.lower(), "image/png")
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def read(path: str | Path, question: str = "", timeout: float = 90) -> str:
    """
    读一张图。返回文字，或者一句说明为什么没读成。

    ★ 失败一律返回**说明文字**而不是抛异常。调用方是模型，
      它拿到异常多半会编一段看起来像结果的描述 —— 那比说"看不了"糟糕得多。
    """
    p = Path(path)

    # ★ 后缀白名单。不是「顺手校验一下」——这是防数据外泄的闸门。
    #   它会把这文件整份 base64 编码发到校外服务器上。没有这道检查，
    #   see_image("data/secrets.json") 读不出内容，但密钥已经出门了。
    if p.suffix.lower() not in MIME:
        return (f"{p.name} 不是图片（支持 {'、'.join(sorted(MIME))}）。"
                f"我只能看图片，别的文件读了也认不出来。")

    if not p.exists():
        return f"找不到这个文件：{p}"
    if p.is_dir():
        return f"{p} 是目录，不是图片"
    try:
        size = p.stat().st_size
    except OSError as e:
        return f"读不了这个文件：{e}"

    # ★ 再嗅一下文件头。后缀能改，内容是改不了的 ——
    #   有人把 secrets.json 改名成 .png，后缀那道闸门就放它过去了，
    #   然后整份 base64 发到校外。这里按真实格式再判一次。
    try:
        head = p.open("rb").read(12)
    except OSError as e:
        return f"读不了这个文件：{e}"
    if not _looks_like_image(head):
        return f"{p.name} 的扩展名是图片，但内容不是（文件头对不上）。不发。"
    if size > MAX_BYTES:
        return (f"这张图 {size / 1048576:.1f} MB，超过 {MAX_BYTES // 1048576} MB 上限。"
                f"先压一下或者截小点。")

    ok, why = available()
    if not ok:
        return f"看不了图 —— {why}。"

    c = config()
    body = {
        "model": c["model"],
        "max_tokens": 1200,
        "temperature": 0,          # 读图要的是稳定，不是发挥
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question or DEFAULT_QUESTION},
                {"type": "image_url", "image_url": {"url": _data_url(p)}},
            ],
        }],
    }
    req = urllib.request.Request(
        f"{c['base']}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {c['key']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        text = (d["choices"][0]["message"]["content"] or "").strip()
        return text or "（模型没说出内容）"
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        return f"读图接口报错 {e.code}：{detail}"
    except Exception as e:
        return f"读图失败：{type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════
#  自检
# ═══════════════════════════════════════════════════════════════

def selftest() -> int:
    fails = 0

    def check(label: str, cond: bool, extra: str = ""):
        nonlocal fails
        print(f"  {'[OK]' if cond else '[!!]'} {label}" + (f"   {extra}" if extra else ""))
        if not cond:
            fails += 1

    print("读图自检\n")

    ok, why = available()
    check("凭据配好了", ok, why or f"{config()['model']} @ {config()['base']}")

    check("不存在的文件给说明而不是异常",
          "找不到" in read(M.ROOT / "不存在的图.png"))

    check("目录不会被当成图",
          "是目录" in read(M.ROOT / "src"))

    if not ok:
        print("\n没配凭据，跳过真调。")
        return fails

    # 造一张图真读一次
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (560, 150), "white")
        d = ImageDraw.Draw(img)
        try:
            f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
        except OSError:
            f = ImageFont.load_default()
        d.text((24, 28), "下午三点交报告", fill="black", font=f)
        d.text((24, 82), "三号楼 512", fill="black", font=f)
        tmp = M.ROOT / "data" / "cache" / "_vision_selftest.png"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        img.save(tmp)
    except Exception as e:
        check("能造出测试图", False, f"{type(e).__name__}: {e}")
        return fails

    try:
        got = read(tmp, "逐字念出图里的文字，只输出文字本身。")
        print(f"\n  读回来：{got!r}")
        check("读出了第一行", "交报告" in got)
        check("读出了第二行", "512" in got)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    print()
    print("全部通过" if not fails else f"{fails} 项未通过")
    return fails


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "selftest":
        sys.exit(selftest())
    if args[0] in ("看图", "read") and len(args) > 1:
        print(read(args[1], args[2] if len(args) > 2 else ""))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
