#!/usr/bin/env python3
"""
vision.py —— 让小日和能看图

═══════════════════════════════════════════════════════════════
  为什么单独一个文件
═══════════════════════════════════════════════════════════════

小日和的脑子（brain.py）走的是 DeepSeek，**那是纯文本的**，
图片进去她只能干看着。这个文件接一个**带视觉的模型**补上这块。

**它不参与对话。** 只干一件事：把图里的东西读成文字，交回给 brain。
所以这里没有人格、没有记忆、没有工具循环——就是个读图函数。

分离的另一个理由：换服务、换模型、换 endpoint，只动这一个文件。

═══════════════════════════════════════════════════════════════
  配哪家的都行
═══════════════════════════════════════════════════════════════

只要对方是 **OpenAI 兼容的 `/chat/completions`，并且支持 `image_url`
这种消息格式**，就能接。本地跑的（Ollama、vLLM、LM Studio）、
云上的（各家多模态 API）、自己学校或公司部署的，都行。

data/secrets.json 里填三个值：

    vision_base_url      到 /v1 为止，比如 https://你的服务/v1
    vision_api_key       对方的 key（本地部署通常不校验，填任意非空串）
    vision_model         模型名，比如 qwen-vl / gpt-4o / llava

未配置或接口失败时返回明确失败结果，桌面不会把错误说明当成识别内容保存。
兼容的字符串入口仍返回可读错误说明；识别成功不代表模型文字一定准确。

> 怎么确认对方支不支持读图：拿一张有字的图调一次，
> 能读出内容就行。只支持纯文本的接口会报参数错误。

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
        data = json.loads(SECRETS.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def config() -> dict:
    d = load_secrets()
    def field(name):
        value = d.get(name, "")
        return value.strip() if isinstance(value, str) else ""
    return {
        "key": field("vision_api_key"),
        "base": field("vision_base_url").rstrip("/"),
        "model": field("vision_model"),
    }


def available() -> tuple[bool, str]:
    """能不能看图。第二个返回值是原因，不能看时给她照实说。"""
    return _availability(config())


def _availability(c: dict) -> tuple[bool, str]:
    if not c["base"] or not c["key"]:
        return False, ("没配读图的接口（data/secrets.json 里缺 "
                       "vision_base_url / vision_api_key）")
    if not c["model"]:
        return False, "没填 vision_model（要一个带视觉的模型名）"
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


def _failure(message: str, cancelled: bool = False) -> dict:
    return {"ok": False, "text": "", "error": message, "cancelled": cancelled}


def read_result(path: str | Path, question: str = "", timeout: float = 90,
                *, cancelled=None) -> dict:
    """Return an explicit result so an error cannot become attachment content.

    Cancellation discards the result and prevents the next request; it cannot
    undo a request already accepted by the provider.
    """
    p = Path(path)
    stopped = cancelled or (lambda: False)
    if stopped():
        return _failure("已取消读取。", True)
    if p.suffix.lower() not in MIME:
        return _failure(f"{p.name} 不是图片（支持 {'、'.join(sorted(MIME))}）。")
    if not p.exists():
        return _failure(f"找不到这个文件：{p}")
    if p.is_dir():
        return _failure(f"{p} 是目录，不是图片")
    try:
        # Read one bounded snapshot: validate exactly the bytes sent, even if
        # the source file is replaced while the worker is reading it.
        with p.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return _failure(f"图片超过 {MAX_BYTES // 1048576} MB 上限，请先压缩或截小。")
        if not _looks_like_image(raw[:12]):
            return _failure(f"{p.name} 的扩展名是图片，但内容不是（文件头对不上）。不发。")
        c = config()
        ok, why = _availability(c)
        if not ok:
            return _failure(f"看不了图 —— {why}。")
        data_url = f"data:{MIME[p.suffix.lower()]};base64," + base64.b64encode(raw).decode()
        body = {
            "model": c["model"], "max_tokens": 1200, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": question or DEFAULT_QUESTION},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
        }
        req = urllib.request.Request(
            f"{c['base']}/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {c['key']}"},
        )
        if stopped():
            return _failure("已取消读取。", True)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
        if stopped():
            return _failure("已取消读取。", True)
        if len(payload) > 2 * 1024 * 1024:
            return _failure("读图接口返回内容过大，请检查接口或缩小图片。")
        choice = json.loads(payload.decode("utf-8"))["choices"][0]
        text = choice["message"]["content"]
        if choice.get("finish_reason") == "length":
            return _failure("读图结果被接口截断，请将图片拆小后重试。")
        if not isinstance(text, str) or not text.strip():
            return _failure("读图接口没有返回可用文字。")
        return {"ok": True, "text": text.strip(), "error": "", "cancelled": False}
    except urllib.error.HTTPError as exc:
        # Provider error bodies can echo request data; do not turn them into
        # saved material or expose credentials in a diagnostic message.
        return _failure(f"读图接口报错 {exc.code}，请检查接口配置、权限和额度后重试。")
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return _failure("读图接口配置或返回格式不正确，请核对视觉服务。")
    except OSError:
        return _failure("读图失败：文件无法读取，或接口连接失败/超时，请检查后重试。")


def read(path: str | Path, question: str = "", timeout: float = 90) -> str:
    """Compatibility text interface for existing tools and command-line use."""
    result = read_result(path, question, timeout)
    return result["text"] if result["ok"] else result["error"]


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

    # ★ 目录名要带图片后缀才走得到 is_dir 那条分支。
    #   用 src/ 这种没后缀的目录测，会在第一道闸门（后缀白名单）就被拒，
    #   结果是这条用例永远绿 —— 测的不是它说自己在测的东西。
    dir_like = M.ROOT / "data" / "cache" / "_vision_selftest_dir.png"
    try:
        dir_like.mkdir(parents=True, exist_ok=True)
        check("目录不会被当成图", "是目录" in read(dir_like))
    finally:
        try:
            dir_like.rmdir()
        except OSError:
            pass

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
