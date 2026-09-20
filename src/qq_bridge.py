#!/usr/bin/env python3
"""
qq_bridge.py —— 把 QQ 来的消息接到小日和的脑子里

═══════════════════════════════════════════════════════════════
  它在链路里的位置
═══════════════════════════════════════════════════════════════

    QQ 群 @小日和
        ↓
    qq_bot.QQGateway        拿原始事件（含 member_openid + 昵称）
        ↓
    qq_bridge（这个文件）    认出是谁 → 组装 → 喊 brain → 回话
        ↓
    brain.py                人格 + 记忆 + 工具（和桌宠同一套）
        ↓
    qq_bot.reply()          出站（再过一遍平台限制兜底）

分离的理由：qq_bot 可以脱网单测协议，qq_bridge 可以灌假事件
单测组装逻辑，不用联网也不用 API key。

═══════════════════════════════════════════════════════════════
  群里为什么不能用高思考档位
═══════════════════════════════════════════════════════════════

QQ 规定被动回复必须**5 分钟内**发出，否则吃 40034128。

而 thinking.json 里的 deep/max 档，思维链能跑好几分钟。
在桌宠里没问题（你等得起），群里就是稳定超时。

所以群里固定走 daily，而且 max_tokens 在**调用之前**就定死（GROUP_MAX_TOKENS）——
不能指望"生成完了再截断"，那样时间已经花掉了。

★ 但**额度不是时间闸**。给多少 token 和"会不会超时"是两件事：
  token 额度决定思维链会不会被拦腰砍断，时间由 REPLY_BUDGET_S 管。
  超时的答案会被丢弃、不发出，所以放宽额度不会去撞 40034128，
  代价只是"想太久"的那条被丢掉（日志里留一行）。

═══ 用法 ═══
    python src/qq_bridge.py selftest      # 灌假事件，不联网
    python src/qq_bridge.py run           # 真正跑（连网+回话）
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402
import people as P  # noqa: E402
import qq_bot as QB  # noqa: E402
import qq_text as QT  # noqa: E402
from persona_manager import PersonaManager  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── 群聊的硬约束 ────────────────────────────────────────────
# 被动回复 5 分钟。留一分钟给发送本身，取 240 秒。
REPLY_BUDGET_S = 240

# 群里不给 deep/max。理由见文件头。
GROUP_LEVEL = "daily"

# ★ max_tokens 是**思维链和正文共享的**。
#   踩过两次：
#     设成 300 —— 思维链吃掉 158~288，正文只剩几十 token，想到一半被砍断，
#       出来就是"逻辑不通"。
#     设成 700 —— 群友发一道带图的题，读图的文字一进上下文，思维链涨到
#       1091 字，700 又被吃光，正文一个字都没有，整条消息哑掉。
#
#   所以放宽到 10500（原值的 15 倍）。放宽之后思维链不再动不动被截断。
#
#   ★ 时间上没有风险：下面 REPLY_BUDGET_S 那道 240 秒的闸是独立的，
#     生成超时会被丢弃、根本不发出去，撞不到 QQ 的 5 分钟窗口。
#     代价是"想太久"的答案会被丢掉 —— 日志里会留一行，不是静默失败。
GROUP_MAX_TOKENS = 10500

# 单聊没有 5 分钟的紧迫感，但输出长度限制是一样的
C2C_LEVEL = None            # None = 跟随 thinking.json
C2C_MAX_TOKENS = 10500

# ★ 给模型看的长度上限，必须和 qq_text.QQ_SAFE_BYTES 对得上。
#   以前这里写 500 字、那边按 1000 字节截断 —— 500 个中文字是 1500 字节，
#   她老老实实照 500 字写，反而会被砍掉三分之二。
#   现在统一成 300 字（约 900 字节，留一点余量）。
REPLY_CHARS_HINT = 300

# 群里最多记多长的临时笔记
NOTE_MAX_CHARS = 120

# 同一个会话最多排几条。她一次能想 4~90 秒，这期间同群来的消息会排进队列；
# 排到这个数还轮不上，说明前一条卡死了，那就丢最早的保最新的。
MAX_PENDING = 5

# ── 对话历史 ────────────────────────────────────────────────
# ★ 没有这个她会"接不上话"。
#   实测：群里刚数完"一、二、三…"，下一条"再来再来"，
#   因为没有前文，她凭空编了个上下文接着数"六、七"，
#   内容还是从人格文档里随便抓的。看起来就是"逻辑不通"。
#
#   记忆库救不了这个 —— 检索是按关键词的，"再来再来"四个字
#   命中不了任何东西。上下文就是上下文，得单独存。
HIST_MAX = 12               # 每个会话留几条
HIST_TTL_MIN = 30           # 超过这么久没说话就当换了话题
HIST_FILE = M.ROOT / "data" / "qq_history.json"

# 不回复的低信息量消息
IGNORE_EXACT = {"", "。", ".", "？", "?", "！", "!", "…", "。。。", "test"}


def log(msg: str) -> None:
    QB.log(f"[bridge] {msg}")


# ═══════════════════════════════════════════════════════════════
#  对话历史
# ═══════════════════════════════════════════════════════════════

_hist_lock = threading.Lock()


def conv_key(ev: QB.QQEvent) -> str:
    """按会话分开。群和单聊是两回事，不同群也不该串。"""
    return (f"group:{ev.group_openid}" if ev.scene == "group"
            else f"c2c:{ev.user_openid}")


def _hist_load() -> dict:
    try:
        import json
        return json.loads(HIST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _hist_save(d: dict) -> None:
    import json
    try:
        HIST_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    except OSError:
        pass


def hist_for(key: str) -> list[dict]:
    """
    这个会话最近说的话。

    ★ 隔着太久就丢掉。半小时没说话，多半已经换话题了 ——
      把陈年上下文喂回去，她会对着新问题答旧话。
    """
    persona_id = PersonaManager(M.ROOT).active_id()
    # 未标记的旧记录仍保留在文件里，但不再作为当前人格的对话注入。
    items = [item for item in (_hist_load().get(key) or [])
             if item.get("persona_id") == persona_id]
    if not items:
        return []
    try:
        last = M.parse_ts(items[-1]["ts"])
    except (KeyError, ValueError):
        return []
    if (M.now() - last).total_seconds() > HIST_TTL_MIN * 60:
        return []
    return items[-HIST_MAX:]


def hist_append(key: str, role: str, name: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    with _hist_lock:
        d = _hist_load()
        items = d.get(key) or []
        items.append({
            "role": role,                     # user | assistant
            "name": name,
            "text": text[:300],
            "ts": M.now_iso(),
            "persona_id": PersonaManager(M.ROOT).active_id(),
        })
        d[key] = items[-HIST_MAX * 2:]        # 存多一点，读的时候再截
        # 顺手清掉太老的会话，别让文件无限长
        cutoff = M.now().timestamp() - 6 * 3600
        for k in list(d):
            try:
                if M.parse_ts(d[k][-1]["ts"]).timestamp() < cutoff:
                    del d[k]
            except (KeyError, ValueError, IndexError):
                del d[k]
        _hist_save(d)


def hist_block(ev: QB.QQEvent) -> str:
    """把历史拼成注入文本。没有就返回空串。"""
    items = hist_for(conv_key(ev))
    if not items:
        return ""
    lines = ["## 刚才聊的（旧的在上）", ""]
    for it in items:
        who = "你" if it["role"] == "assistant" else (it.get("name") or "他")
        # 换行缩进一下，免得跟下一条黏在一起
        body = it["text"].replace("\n", " ")
        lines.append(f"{who}：{body}")
    lines += [
        "",
        "★ 这是**刚才**的对话，按时间顺序。他说的下一句要接在这后面理解 ——"
        "比如他只说「再来」两个字，意思在上文里，不要自己编一个上下文。",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  身份
# ═══════════════════════════════════════════════════════════════

def identify(ev: QB.QQEvent) -> dict:
    """
    认出说话的是谁。返回一个给 prompt 用的身份包。

    三种情况，措辞差别很大 —— "这是谁"直接决定她该怎么接话。
    """
    oid = ev.speaker_id
    if not oid:
        return {"kind": "unknown", "id": "", "name": "群友", "block": ""}

    owner = False
    if ev.scene == "group":
        owner = P.is_owner(ev.group_openid, ev.member_openid)
    else:
        owner = oid in P.owner_openids()

    if oid:
        P.touch(oid, ev.username, ev.member_role, ev.group_openid,
                ev.union_openid, is_owner=owner)

    name = P.label(oid)
    return {
        "kind": "owner" if owner else "guest",
        "id": oid,
        "name": name,
        "is_owner": owner,
        "block": P.block(oid, is_owner_flag=owner, group_openid=ev.group_openid),
    }


# ═══════════════════════════════════════════════════════════════
#  组装
# ═══════════════════════════════════════════════════════════════

def build_prompt(ev: QB.QQEvent, who: dict) -> str:
    """
    拼出发给模型的东西。

    ★ 身份放在**最前面**。它是底色 —— 后面所有内容都要按
      "谁在说话"来理解。放在末尾容易被当成补充说明。
    """
    # 用 get 而不是 [] —— 身份块缺失是可以接受的降级，
    # 但不该让整个回复崩掉（一条消息回不了比少一句上下文严重得多）。
    parts = [who.get("block") or ""]

    # ★ 历史放在身份之后、当前消息之前。
    #   这个顺序是有讲究的：先知道"谁在说"，再看"刚才说了什么"，
    #   最后才是"现在这句" —— 跟人读聊天记录的次序一致。
    hist = hist_block(ev)
    if hist:
        parts.append(hist)

    where = "群里" if ev.scene == "group" else "私聊"
    lines = ["", f"## 他{where}对你说", "", ev.content]

    if ev.scene == "group":
        lines += [
            "",
            "★ 这是群聊。他只 @ 了你一个人，但群里还有别人看得到你的回复。",
            f"★ 回复要短 —— QQ 限 {REPLY_CHARS_HINT} 中文字以内，而且群里刷得快。",
            "★ 群里有别人在看。他说的话、你说的话，都不是私密的。",
        ]
    else:
        lines += ["", "★ 这是私聊，只有他看得到。可以放松一点。",
                  f"★ 回复仍然要短，QQ 限 {REPLY_CHARS_HINT} 中文字以内。"]

    parts.append("\n".join(lines))
    return "\n".join(parts)


# 兼容接口有时会把“实时比分”直接当成普通聊天，不发起 web_search 工具调用。
# 这类问题不能靠模型自觉补救：先在桥接层检索，再让模型负责整理结果。
_SEARCH_INTENT = re.compile(
    r"实时|即时|比分|赛况|赛程|最新|刚刚|今天.*(比赛|新闻|天气|价格)|"
    r"新闻|天气|股价|汇率|票价|公告")


def prefetch_search(query: str) -> tuple[str, bool]:
    """为明显的时效性问题预取搜索结果，返回 (结果, 是否成功)。"""
    if not _SEARCH_INTENT.search(query or ""):
        return "", False
    try:
        import local_tools as LT
        kind = "news" if re.search(r"新闻|最新|刚刚|实时|比分|赛况", query) else "text"
        result = LT.web_search(query, kind=kind, max_results=5)
        if not result or result.startswith("搜索失败"):
            return "", False
        return result, True
    except Exception as e:
        log(f"预取搜索失败：{type(e).__name__}: {e}")
        return "", False


def build_system(ev: QB.QQEvent) -> tuple[str, dict]:
    """
    人格 + 记忆 + 平台约束。

    走 brain.build_system 的话会带上桌宠的 system_block（思考档位的
    那一堆说明），那在群里是多余的。这里自己拼，只保留有用的部分。
    """
    import brain as B
    import thinking as T

    level = GROUP_LEVEL if ev.scene == "group" else C2C_LEVEL
    # 让记忆检索用上这个档位的预算（和桌宠那条路一致）
    T.apply_to_memory(level, ev.content)

    persona = M.persona_text()
    ctx = M.build_context(ev.content)

    platform = (
        "## 你现在在 QQ 上说话\n\n"
        "三条硬限制，违反了整条消息发不出去：\n"
        f"1. 不发链接。要提来源就说名字。\n"
        f"2. {REPLY_CHARS_HINT} 个中文字以内。超了会被硬截断，"
        f"句子会断在半截 —— 所以宁可说少点，说完。\n"
        "3. 不用 Markdown。「」比 ** 好用。\n\n"
        "代码层面还有一道兜底会再洗一遍，但被洗过的消息会缺东西，"
        "不如你自己就别写。\n\n"
        "★ 短不等于敷衍。把一件事**说清楚**比说得多重要 ——"
        "该给的理由给完，然后停。\n\n"
        "★ 遇到实时、最新、比分、赛况、新闻、天气或价格问题，必须使用"
        " web_search；只有工具明确失败时，才能说查不到。不要凭空说联网后端没装好。"
    )

    system = "\n\n---\n\n".join(p for p in [persona, ctx, platform] if p)
    return system, {"level": level}


# ═══════════════════════════════════════════════════════════════
#  图片
# ═══════════════════════════════════════════════════════════════

MEDIA_DIR = M.ROOT / "data" / "qq_media"

# 下载上限。QQ 那边原图能到几 MB，读图接口也吃不下更大的。
MEDIA_MAX_BYTES = 8 * 1024 * 1024

# 留多久。这些图只是中转一下给她看，看完就没用了 ——
# 攒着既占地方，也是把别人发的东西留在了本地。
# 7 天是给「她当时没看懂、过两天想再翻」留的余量。
# ★ 但如果她用 keep_image 把图挪进沙箱了，那就不归这儿管了 ——
#   挪出去 = 用户明确要留，清理只扫这个中转目录。
MEDIA_KEEP_DAYS = 7
MEDIA_KEEP_HOURS = MEDIA_KEEP_DAYS * 24

# 一条消息最多读几张。有人一口气发九宫格的话，全读一遍又慢又贵。
MEDIA_MAX_PER_MSG = 3


def _sweep_media() -> None:
    """删掉过期的中转图。下载时顺手做，不用另起定时任务。"""
    if not MEDIA_DIR.is_dir():
        return
    cutoff = time.time() - MEDIA_KEEP_HOURS * 3600
    for f in MEDIA_DIR.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _download(url: str, dest: Path, timeout: float = 30) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AIPet/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(MEDIA_MAX_BYTES + 1)
        if len(data) > MEDIA_MAX_BYTES:
            log(f"图片超过 {MEDIA_MAX_BYTES // 1048576} MB，不要了")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception as e:
        log(f"下载图片失败：{type(e).__name__}: {e}")
        return False


def read_images(ev: QB.QQEvent) -> str:
    """
    把这条消息里的图读成文字。没有图、或者全读失败，返回空串。

    ★ URL 带 rkey 签名，**会过期**。所以是收到就下载，不排队、不缓存 URL。
      实测那条 url 长这样：
        https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=...&rkey=...&spec=0

    ★ 读图走 vision.py 配的那个接口，**不是她的脑子** —— DeepSeek 是纯文本的。
      所以群里任何人发的图都会经过那个服务商，和本地记忆不是一回事。
      配之前先想清楚你要把它指向哪里。
    """
    imgs = ev.images
    if not imgs:
        return ""

    _sweep_media()
    parts: list[str] = []
    for i, a in enumerate(imgs[:MEDIA_MAX_PER_MSG], 1):
        url = str(a.get("url") or "")
        if not url:
            continue
        raw_name = str(a.get("filename") or f"img{i}.jpg")
        safe = re.sub(r"[^\w.\-]", "_", Path(raw_name).name)[:60] or f"img{i}.jpg"
        dest = MEDIA_DIR / f"{ev.msg_id[:12]}_{safe}"

        if not _download(url, dest):
            parts.append(f"【第 {i} 张图没取到】")
            continue

        import vision as V
        text = V.read(dest, "把这张图里的内容读出来。有文字就逐字抄下来、保留分行；"
                            "没有文字就平实描述画面里有什么。不要评价，不要推测用途。")
        log(f"读了第 {i} 张图（{dest.name}）：{text[:60]}")
        parts.append(f"【第 {i} 张图（{safe}）】\n{text}")

    return "\n\n".join(parts)


def read_documents(ev: QB.QQEvent) -> str:
    """
    把这条消息里的 Word 文档读成文字。没有、或全失败，返回空串。

    和读图一样：URL 带 rkey 签名会过期，所以是收到就下载、不缓存。

    .docx 里的**图片也会被读出来**（走同一个视觉接口）—— 正文和图片里
    的文字一起交给她。老的 .doc 解不了，会回一句让人另存为。

    下载的文件落在中转目录里，交给 _sweep_media 按同一套规则清理。
    """
    docs = ev.documents
    if not docs:
        return ""

    _sweep_media()
    parts: list[str] = []
    for i, a in enumerate(docs[:MEDIA_MAX_PER_MSG], 1):
        url = str(a.get("url") or "")
        raw_name = str(a.get("filename") or f"doc{i}.docx")
        safe = re.sub(r"[^\w.\-]", "_", Path(raw_name).name)[:60] or f"doc{i}.docx"
        if not url:
            parts.append(f"【文档 {safe} 没取到：消息里没有下载地址】")
            continue

        dest = MEDIA_DIR / f"{ev.msg_id[:12]}_{safe}"
        if not _download(url, dest):
            parts.append(f"【文档 {safe} 没取到】")
            continue

        try:
            import docx_read
            text = docx_read.as_prompt_block(dest)
        except Exception as e:                       # noqa: BLE001
            text = f"读不了这个文档：{type(e).__name__}: {e}"
        log(f"读了文档（{safe}）：{text[:80]}")
        parts.append(f"【文档 {safe}】\n{text}")

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════
#  认领
# ═══════════════════════════════════════════════════════════════

CLAIM_WORDS = ("认领", "绑定", "我是主人", "/claim")


def claim_reply(ev: QB.QQEvent, who: dict) -> str | None:
    """
    处理认领相关的话。不该处理就返回 None。

    两条路：
      单聊里发本地生成的口令 → 认证成主人（之后所有群自动生效）
      群里发口令            → 给这一个群单独绑定（兜底，见 people.try_claim）
    """
    text = (ev.content or "").strip()

    # ── 单聊：用本地生成的口令认证主人 ──
    if ev.scene == "c2c":
        if not any(w in text for w in CLAIM_WORDS):
            return None
        code = P.extract_code(text)
        if not code:
            return None
        st = P.claim_state()
        if not st:
            return "口令过期了。在电脑上跑 `python src/people.py claim` 重新拿一个。"
        if code != st["code"]:
            return "不对。"
        d = P.qq_snapshot()
        d["claim"]["used"] = True
        P.save_qq(d)
        P.add_owner(ev.speaker_id, ev.username, "c2c")
        log(f"主人已认证：{ev.username!r} ({ev.speaker_id[:8]}…)")
        return ("行。记下了。\n"
                "以后你在哪个群 @ 我，我都认得出来 —— 不用一个群一个群认。")

    # ── 群聊：口令单群绑定（兜底）──
    if ev.scene == "group" and any(w in text for w in CLAIM_WORDS):
        code = P.extract_code(text)
        if len(code) < P.CLAIM_LEN:
            return None
        ok, msg = P.try_claim(code, ev.group_openid, ev.member_openid, ev.username)
        log(f"群内认领 {ev.group_openid[:8]}… by {ev.username!r}：{'成功' if ok else msg}")
        return msg

    return None


# ═══════════════════════════════════════════════════════════════
#  指令口
# ═══════════════════════════════════════════════════════════════

# ★ 只在主人那儿开。群里谁都能发的话，档位就成了公共设施；
#   而且 deep/max 在群里还会拖长响应，更容易撞 40034128。
LEVEL_ALIAS = {
    "auto": "auto", "自动": "auto",
    "frugal": "frugal", "省电": "frugal",
    "daily": "daily", "日常": "daily",
    "serious": "serious", "认真": "serious",
    "deep": "deep", "深究": "deep",
    "max": "max", "极限": "max",
}
LEVEL_ORDER = ("auto", "frugal", "daily", "serious", "deep", "max")

CMD_RE = re.compile(r"^[/／]?(档位|思考|level|thinking)\s*[:：]?\s*(\S*)$", re.I)

# 查更新。不带参数，所以正则收紧到结尾 —— 「更新一下记忆」不能被它吃掉。
# 同样只认主人：这个要真去连 GitHub，群里谁都能发就成了公共出口。
VER_RE = re.compile(r"^[/／]?(版本|更新|检查更新|version|update)\s*[:：]?\s*$", re.I)


def command_reply(ev: QB.QQEvent, who: dict) -> str | None:
    """认一下是不是指令。不是就返回 None。"""
    text = (ev.content or "").strip()

    if VER_RE.match(text):
        if not who.get("is_owner"):
            return "这个只有他能调。"
        import update as UP
        r = UP.check()
        log(f"查更新（{ev.scene} by {who['name']!r}）→ "
            + (f"有新版 {r['latest_clean']}" if r.get("newer")
               else ("已是最新" if r.get("ok") else f"没查成：{r.get('error')}")))
        # ★ 结论里不许出现 URL（QQ 会拒收整条），describe() 已经保证这点。
        return UP.describe(r)

    m = CMD_RE.match(text)
    if not m:
        return None
    if not who.get("is_owner"):
        return "这个只有他能调。"

    import thinking as T
    names = {"auto": "自动"}
    for k, v in T.load().get("presets", {}).items():
        names[k] = v.get("name", k)
    opts = "、".join(names.get(k, k) for k in LEVEL_ORDER)

    arg = (m.group(2) or "").strip().lower()
    if not arg:
        cur = T.current_level()
        return f"现在是「{names.get(cur, cur)}」\n可选：{opts}"

    lv = LEVEL_ALIAS.get(arg)
    if not lv:
        # ★ 认不出来就交回模型，别自作主张回"没这个档位"。
        #   实测过："档位是什么意思"这种正常提问会被它吃掉，
        #   然后答非所问。宁可漏一个打错字的提示。
        return None

    old = T.current_level()
    T.set_level(lv)
    log(f"档位 {old} → {lv}（{ev.scene} by {who['name']!r}）")
    # ★ 群里那条消息本身仍走 daily（见文件头），得说清楚，
    #   不然他调完发现"没反应"，会以为是坏的。
    tail = "\n群里还是走日常档（怕超时），这条对单聊和桌宠生效。" if ev.scene == "group" else ""
    return f"好，切到「{names.get(lv, lv)}」。{tail}"


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════

class Bridge:
    """
    处理一条 QQ 消息。线程安全，可以被多个事件并发调用。

    每条消息一个线程 —— 因为 brain 要跑好几秒，
    在 Qt 事件循环里同步做会把心跳卡死（心跳不发，网关会踢你）。
    """

    def __init__(self, reply_enabled: bool = True):
        self.reply_enabled = reply_enabled
        self._busy: set[str] = set()
        self._pending: dict[str, list] = {}     # 会话 key → 排队等着的消息
        self._lock = threading.Lock()

    def handle(self, ev: QB.QQEvent) -> None:
        if ev.kind not in ("group_at", "c2c"):
            log(f"忽略事件类型 {ev.kind}")
            return
        # ★ 只看正文会把图片消息整条丢掉。实测：群里发图的推送是
        #   content=" "（一个空格）+ attachments=[{url, content_type, ...}]。
        #   所以判断「有没有内容」必须把附件也算上。
        #
        #   ★ 但这里原来是**静默 return** —— 日志里只留一行"收到"，
        #     后面什么都没有，排查时根本看不出是被这条挡的。记一笔。
        if not (ev.content or "").strip() and not ev.attachments:
            log("空内容事件（没有正文也没有附件），跳过")
            return
        t = threading.Thread(target=self._run, args=(ev,), daemon=True)
        t.start()

    # ── 真正干活 ──
    def _run(self, ev: QB.QQEvent) -> None:
        """
        同一个会话串行，免得两条消息同时改记忆。

        ★ 原来是"正忙就丢掉"（`log("上一条还在处理，这条跳过")` + return）。
          问题在于她一次能想 4~90 秒，这个窗口里同群发的消息**全都没了**，
          而且用户那边看不出任何异常 —— 只是"她没理我"。

          现在改成排队：忙的时候把消息压进 _pending，处理完一条自动取下一条。
          原来是怕并发改记忆才串行的，但 memory.py 早就有写锁了，
          真正需要的只是"顺序"，不是"丢弃"。

        ★ t0 每条都要重算：REPLY_BUDGET_S 是从 t0 起算的，
          排队排了半分钟再用同一条的 t0，会直接判超时。
        """
        key = f"{ev.scene}:{ev.group_openid or ev.user_openid}"
        with self._lock:
            if key in self._busy:
                q = self._pending.setdefault(key, [])
                if len(q) >= MAX_PENDING:
                    # 积压太多说明前一条卡住了。丢最老的，保最新的 ——
                    # 最新的那句才是用户现在在等的。
                    q.pop(0)
                    log(f"队列积压超过 {MAX_PENDING} 条，丢掉最早的一条")
                q.append(ev)
                log(f"上一条还在处理，这条排队（队列 {len(q)} 条）")
                return
            self._busy.add(key)

        try:
            cur = ev
            while cur is not None:
                try:
                    self._process(cur, time.time())
                except Exception as e:
                    import traceback
                    log(f"处理出错：{type(e).__name__}: {e}")
                    log(traceback.format_exc()[-800:])
                with self._lock:
                    q = self._pending.get(key) or []
                    cur = q.pop(0) if q else None
                    if cur is None:
                        self._pending.pop(key, None)
                        self._busy.discard(key)
        except BaseException:
            # 不管怎么出去的，闸门必须放开 —— 否则这个会话永远卡在"忙"。
            with self._lock:
                self._pending.pop(key, None)
                self._busy.discard(key)
            raise

    def _process(self, ev: QB.QQEvent, t0: float) -> None:
        who = identify(ev)
        # ★ 带 <image url="..."/> 这类富媒体标签的必须整条记下来。
        #   平时截 40 字是为了日志好读，但图片消息的 URL 正好在
        #   40 字往后 —— 截了就永远查不出格式，只能靠猜。
        _raw = ev.content or ""
        _show = _raw if (len(_raw) <= 400 or "<" in _raw) else _raw[:40] + "…"
        log(f"{'主人' if who['is_owner'] else '群友'} {who['name']!r}：{_show}")

        # ★ 图先读成文字，再入历史。
        #   放在入历史之前是有意的：这样她下次翻聊天记录，看到的是
        #   "对方发了张图，图里是 xxx"，而不是一个空格 —— 否则过一会儿
        #   再提起这张图，她完全不记得有这回事。
        if ev.attachments:
            imported = "\n\n".join(
                x for x in (read_images(ev), read_documents(ev)) if x)
            if imported:
                ev.content = ((ev.content or "").strip() + "\n\n" + imported).strip()

        # ★ 先入历史再回话。
        #   认领、低信息量这些也记 —— "再来再来"前面那句可能正是
        #   "你几点上线"，不记的话上下文还是断的。
        hist_append(conv_key(ev), "user", who["name"], ev.content)

        # 认领优先，别拿去喂模型
        c = claim_reply(ev, who)
        if c is not None:
            self._send(ev, c)
            hist_append(conv_key(ev), "assistant", "", c)
            return

        # 指令口。也排在喂模型前面 —— 指令不是聊天内容。
        c = command_reply(ev, who)
        if c is not None:
            self._send(ev, c)
            hist_append(conv_key(ev), "assistant", "", c)
            return

        # 带图的消息正文可能是空的，别拿"低信息量"把它误杀
        if not ev.attachments and ev.content.strip().lower() in IGNORE_EXACT:
            log("低信息量，不回")
            return

        if not self.reply_enabled:
            log("（只收不发模式，不回复）")
            return

        # ── 想 ──
        import brain as B
        if not B.api_key():
            log("没配 API key，回不了")
            return

        system, meta = build_system(ev)
        budget = (GROUP_MAX_TOKENS if ev.scene == "group" else C2C_MAX_TOKENS)
        left = REPLY_BUDGET_S - (time.time() - t0)
        if left <= 20:
            log(f"只剩 {left:.0f} 秒，来不及想了")
            return

        prompt = build_prompt(ev, who)
        # 对明显的实时问题先查一次，避免兼容接口漏掉工具调用。
        prefetched, searched = prefetch_search(ev.content)
        if searched:
            prompt += ("\n\n## 刚刚查到的联网结果\n"
                       "下面是程序刚刚检索到的资料。只根据这些资料回答；"
                       "资料没有明确比分就直说没有查到，不要编造。\n"
                       + prefetched)
        try:
            # ★ 非主人不给 see_image。那个工具会把整个文件 base64 之后
            #   发到 vision.py 配的那个服务（可能是外部的），群里任何人
            #   都不该有这个口子 —— 一句"看看 D:\某文件.png"就够把东西送出去。
            #   主人的记忆里有这条规矩，但记忆是说服，这里是拦。
            blocked = set() if who["is_owner"] else {"see_image"}
            # 已经预取过就禁用第二次 web_search，避免浪费时间并让模型
            # 又回到“后端不可用”的拒答模板。
            if searched:
                blocked.add("web_search")
            reply, _reasoning, info = B.ask_with_system(
                prompt, system,
                level=meta["level"], max_tokens=budget,
                block_tools=blocked)
        except Exception as e:
            log(f"brain 出错：{type(e).__name__}: {e}")
            return

        reply = (reply or "").strip()
        if not reply:
            log(f"她没说出话来。诊断：{info}")
            return

        took = time.time() - t0
        if took > REPLY_BUDGET_S:
            log(f"想出答案花了 {took:.0f} 秒，超过被动回复窗口，不发了")
            return

        # ★ 回话内容也写日志。之前只记了字数，出了问题根本看不到
        #   她到底说了什么 —— 排查"逻辑不通"的时候两眼一抹黑。
        log(f"想了 {took:.1f} 秒，回 {len(reply)} 字：{reply[:120]}")
        self._send(ev, reply)
        hist_append(conv_key(ev), "assistant", "", reply)

        # ── 记 ──
        self._remember(ev, who, reply)
        self._log_mood(ev)

    def _log_mood(self, ev: QB.QQEvent) -> None:
        """每轮回完落一行当时的状态。规矩写在 SOUL.md 里。"""
        try:
            import mood as MD
            MD.write_log(ev.content, source=f"qq:{ev.scene}")
        except Exception as e:
            log(f"mood 日志出错：{type(e).__name__}: {e}")

    def _send(self, ev: QB.QQEvent, text: str) -> None:
        clean, notes = QT.sanitize(text)
        if not clean:
            log("清洗后没内容了，不发")
            return
        if notes:
            log(f"出站清洗：{'、'.join(notes)}")
        r = QB.reply(ev, clean)
        if r.get("_error") or r.get("_skipped"):
            log(f"发送失败：{r}")
        else:
            log("已回复")

    def _remember(self, ev: QB.QQEvent, who: dict, reply: str) -> None:
        """落盘。主人的话进记忆库，群友的话只进人物卡的临时笔记。"""
        try:
            if who["is_owner"]:
                M.add(f"用户在QQ上说：{ev.content}", 2, ["QQ"], "", "normal",
                      source="qq", speaker="owner",
                      speaker_name=who["name"])
                if reply:
                    M.add(f"我回了：{reply}", 1, ["QQ"], "", "normal",
                          source="qq", speaker="owner")
            else:
                # ★ 群友的话不进口述记忆库 —— 那是关于主人的地方。
                #   只在他自己的人物卡上留一条几小时就淡掉的笔记。
                oid = who["id"]
                if oid:
                    P.note(oid, ev.content[:NOTE_MAX_CHARS], kind="said")
                    P.note(oid, f"（我回了：{reply[:60]}）", kind="replied")
                    P.touch(oid, ev.username, ev.member_role, ev.group_openid)
        except Exception as e:
            log(f"写记忆出错：{e}")


# ═══════════════════════════════════════════════════════════════
#  自检（灌假事件，不联网）
# ═══════════════════════════════════════════════════════════════

def _fake(scene="group", content="你好", name="小明", oid="AAA1",
          group="GRP1", role="member", secs_ago=0) -> QB.QQEvent:
    from datetime import timedelta
    return QB.QQEvent(
        kind="group_at" if scene == "group" else "c2c",
        msg_id="MSG1", content=content, ts=M.now() - timedelta(seconds=secs_ago),
        scene=scene, group_openid=group if scene == "group" else "",
        member_openid=oid if scene == "group" else "",
        user_openid=oid if scene == "c2c" else "",
        username=name, member_role=role)


def selftest() -> int:
    fails = 0

    def check(label, cond, extra=""):
        nonlocal fails
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + extra) if extra else ''}")
        if not cond:
            fails += 1

    QB._quiet_logging()       # 自检不往生产日志里写假事件
    print("QQ 桥接自检（灌假事件，不联网）\n")

    bp, bq = P.snapshot(), P.qq_snapshot()
    # 记忆库整份原样快照，收尾时原样写回（见 finally 里的说明）
    _jf = M._p("journal")
    _journal_before = _jf.read_bytes() if _jf.exists() else None
    try:
        P.save({**P._blank_people()})
        P.save_qq(P._blank_qq())

        # ── 身份 ──
        ev = _fake(name="小明", oid="AAA1")
        w = identify(ev)
        check("认出群友", w["kind"] == "guest" and w["name"] == "小明")
        check("建了人物卡", P.get("AAA1") is not None)
        check("★ 群友的注入块声明不是主人", "不是主人的话" in w["block"])

        P.add_owner("OWNER1", "主人", "c2c")
        w2 = identify(_fake(name="主人", oid="OWNER1", role="admin"))
        check("认证过的人被认成主人", w2["is_owner"])
        check("★ 主人换一个群还是主人",
              identify(_fake(oid="OWNER1", group="OTHER"))["is_owner"])
        check("主人的注入块不一样", "现在说话的是主人" in w2["block"])

        # ── 组装 ──
        ev3 = _fake(content="今天几号")
        pr = build_prompt(ev3, identify(ev3))
        check("身份排在最前面", pr.index("现在说话的是") < pr.index("对你说"))
        check("带上了消息内容", "今天几号" in pr)
        check("群聊提醒了要短", "群里刷得快" in pr)
        ev4 = _fake(scene="c2c", oid="AAA1")
        check("私聊有不同说法",
              "只有他看得到" in build_prompt(ev4, identify(ev4)))
        check("身份块缺失也不崩", build_prompt(ev4, {}).strip() != "")
        check("提醒了三条硬限制",
              all(k in build_system(ev3)[0]
                  for k in ("不发链接", str(REPLY_CHARS_HINT), "Markdown")))

        # ── 档位与预算 ──
        check("群里固定 daily", build_system(_fake())[1]["level"] == "daily")
        check("群里不给 deep/max", GROUP_LEVEL not in ("deep", "max"))
        check("240 秒闸小于 QQ 的 5 分钟", REPLY_BUDGET_S < 300)

        # ★ 这条是永久守卫。踩过一次：提示词说"500 字以内"，
        #   而 sanitize 按 1000 字节截断 —— 500 个中文字是 1500 字节，
        #   她照着规则写反而被砍掉三分之二。两个数字必须对得上。
        check(f"★ 给她的字数提示（{REPLY_CHARS_HINT}）装得进字节上限"
              f"（{QT.QQ_SAFE_BYTES}）",
              QT.byte_len("字" * REPLY_CHARS_HINT) <= QT.QQ_SAFE_BYTES,
              f"{QT.byte_len('字' * REPLY_CHARS_HINT)} 字节")

        # ★ max_tokens 是思维链和正文共享的。太小的话思维链吃光预算，
        #   正文在想到一半被砍断 —— 症状是"逻辑不通"，或者干脆一个字都没有。
        #   实测撞过两次：700 的额度被 1091 字的思维链吃光，整条消息哑掉。
        #   这条只保证下限，具体多少见 GROUP_MAX_TOKENS 上面那段。
        need = 1200 + REPLY_CHARS_HINT * 4      # 思维链留 1200，正文按中文约 4 字节/token 粗算
        check("★ max_tokens 留得下思维链 + 正文",
              GROUP_MAX_TOKENS >= need,
              f"{GROUP_MAX_TOKENS}（至少要 {need}）")

        # ── 对话历史 ──
        # 这段是本次的核心修复。原来的症状：群里刚数完"一、二、三"，
        # 下一条"再来再来"因为没有前文，她凭空编了个上下文接着数"六、七"，
        # 内容还是从人格文档里抓的 —— 看起来就是"逻辑不通"。
        hpath = HIST_FILE
        old_hist = hpath.read_text(encoding="utf-8") if hpath.exists() else None
        try:
            if hpath.exists():
                hpath.unlink()

            g1 = _fake(content="我们来数数，一、二、三", oid="AAA1", name="小明")
            k = conv_key(g1)
            check("群和单聊的 key 分开",
                  k.startswith("group:") and conv_key(_fake(scene="c2c")).startswith("c2c:"))
            check("没说过话时历史是空的", hist_for(k) == [])
            check("没历史时不注入", hist_block(g1) == "")

            hist_append(k, "user", "小明", "我们来数数，一、二、三")
            hist_append(k, "assistant", "", "一、二、三。")
            check("写过就有历史", len(hist_for(k)) == 2)
            blk = hist_block(g1)
            check("历史进得了注入块", "一、二、三" in blk)
            check("历史标了谁说", "小明：" in blk and "你：" in blk)
            check("★ 提醒她别自己编上下文", "不要自己编一个上下文" in blk)

            # 关键场景：短消息 + 有历史
            ev5 = _fake(content="再来再来", oid="AAA1", name="小明")
            pr5 = build_prompt(ev5, identify(ev5))
            check("★ 断句能接上（历史排在当前消息之前）",
                  pr5.index("一、二、三") < pr5.index("再来再来"))

            # 隔离：别的会话读不到
            other = _fake(content="你好", oid="ZZZ", group="OTHER")
            check("别的群看不到这个群的历史", hist_block(other) == "")

            # 容量
            for i in range(40):
                hist_append(k, "user", "小明", f"第{i}句")
            check(f"历史有上限（{HIST_MAX} 条）", len(hist_for(k)) == HIST_MAX,
                  f"{len(hist_for(k))} 条")

            # TTL：拨旧了就该丢
            import json as _j
            d = _j.loads(hpath.read_text(encoding="utf-8"))
            for it in d[k]:
                it["ts"] = (M.now() - __import__("datetime").timedelta(
                    minutes=HIST_TTL_MIN + 5)).isoformat(timespec="seconds")
            hpath.write_text(_j.dumps(d, ensure_ascii=False), encoding="utf-8")
            check(f"★ 超过 {HIST_TTL_MIN} 分钟自动作废（别对着新问题答旧话）",
                  hist_for(k) == [])
        finally:
            if old_hist is None:
                try:
                    hpath.unlink()
                except OSError:
                    pass
            else:
                hpath.write_text(old_hist, encoding="utf-8")

        # ── 超时放弃 ──
        b = Bridge(reply_enabled=False)
        old = _fake(secs_ago=REPLY_BUDGET_S + 100)
        sent: list = []
        b._send = lambda e, t: sent.append(t)          # type: ignore
        b._process(old, time.time() - REPLY_BUDGET_S - 100)
        check("超时的消息不再回复", not sent)

        # ── 认领 ──
        c = P.issue_claim()
        check("单聊发错口令被拒",
              claim_reply(_fake(scene="c2c", content="认领 XXXXXXXX",
                                oid="WHO1"), {}) is not None)
        r = claim_reply(_fake(scene="c2c", content=f"认领 {c['code']}",
                              oid="WHO1", name="主人"), {})
        check("单聊发对口令认证成功", r is not None and "记下了" in r, f"{r}")
        check("认证后进 owners", "WHO1" in P.owner_openids())
        check("认证后群里自动是主人",
              P.is_owner("ANYGROUP", "WHO1"))
        check("口令用掉即废", P.claim_state() is None)

        check("普通消息不触发认领",
              claim_reply(_fake(content="今天天气不错"), {}) is None)

        # ── 低信息量 ──
        for junk in ("", ".", "？", "test"):
            check(f"低信息量不触发认领：{junk!r}",
                  claim_reply(_fake(content=junk), {}) is None)

        # ── 群友的话不进主记忆库 ──
        n_before = len(M.load_journal())
        b2 = Bridge(reply_enabled=True)
        b2._send = lambda e, t: None                   # type: ignore
        b2._remember(_fake(name="小明", oid="GUEST9", content="我生日是5月1号"),
                     identify(_fake(name="小明", oid="GUEST9")), "哦")
        check("★ 群友的话没进主记忆库",
              len(M.load_journal()) == n_before, f"{n_before} → {len(M.load_journal())}")
        gn = P.notes_for("GUEST9")
        check("群友的话进了他自己的临时笔记", len(gn) >= 1, f"{[n['text'] for n in gn]}")

        # 主人的话要进
        b2._remember(_fake(name="主人", oid="OWNER1", content="我明天要交报告"),
                     identify(_fake(name="主人", oid="OWNER1")), "记着了")
        check("主人的话进了主记忆库", len(M.load_journal()) > n_before)
    finally:
        P.save(bp)
        P.save_qq(bq)
        # ★ 用「原样快照 + 原样还原」，不要用条件过滤。
        #
        #   踩过的坑（真丢过数据）：原来这里写的是
        #       kept = [e for e in M.load_journal() if e.get("source") != "qq"]
        #       M.save_journal(kept)
        #   两个问题叠在一起：
        #     1. 真实 QQ 对话的记忆 source 也是 "qq"，一起被删了。
        #     2. M.load_journal() 本身还会过滤掉用户「撤回并忘记」的条目，
        #        用它的结果去 save_journal，等于把那些条目从磁盘上永久抹掉。
        #   测试要清理的是**它自己写进去的东西**，不是"看起来像测试的东西"。
        _raw = M._p("journal")
        if _journal_before is None:
            try:
                _raw.unlink()
            except OSError:
                pass
        else:
            _raw.write_bytes(_journal_before)

        # ★ 永久守卫：确认**原来有的记忆一条都没少**。
        #   这条比"测试通过了"重要 —— 原来就是测试全绿、记忆却少了 4 条。
        #
        #   注意判据是"旧条目没消失"，不是"逐字节相同"：
        #   QQ 桥可能正在线上跑，这期间它会正常追加新记忆。
        #   追加是对的，丢条目才是 bug。拿字节相等去判会误报。
        def _rows(b):
            if not b:
                return []
            return [json.loads(l) for l in b.decode("utf-8").splitlines() if l.strip()]

        _old = _rows(_journal_before)
        _new_ids = {e["id"] for e in _rows(_raw.read_bytes() if _raw.exists() else None)}
        _lost = [e for e in _old if e["id"] not in _new_ids]
        check("★ 自检没有删掉任何原有记忆",
              not _lost,
              f"原有 {len(_old)} 条，现在 {len(_new_ids)} 条"
              + (f"，丢了 {[e['id'] for e in _lost]}" if _lost else ""))

    print(f"\n{'全部通过' if fails == 0 else str(fails) + ' 项失败'}")
    return 1 if fails else 0


# ═══════════════════════════════════════════════════════════════
#  跑
# ═══════════════════════════════════════════════════════════════

def run(reply_enabled: bool = True) -> int:
    from PySide6.QtCore import QCoreApplication
    import brain as B

    app = QCoreApplication(sys.argv)

    appid, _ = QB.secrets()
    if not appid:
        print("没配 QQ 凭据。先跑：python tools/backup_cherry_qq.py")
        return 1
    if reply_enabled and not B.api_key():
        print("没配 DeepSeek key，她张不开嘴。先：python src/brain.py setkey sk-xxxx")
        return 1

    before = P.prune()
    if before["removed"]:
        log(f"清掉 {before['removed']} 条过期笔记")

    bridge = Bridge(reply_enabled=reply_enabled)
    gw = QB.QQGateway(
        on_event=bridge.handle,
        on_state=lambda s, d: log(f"网关 {s} {d}"),
        # ★ 致命错误**不退出**。退出了就没人再拉起来 —— 开机自启只在登录时跑，
        #   也就是 QQ 会一直死到下次重启，而且看不出来为什么。
        #   停在这儿，状态写进 qq_status.json，桌宠右键菜单能看见原因。
        on_fatal=lambda m: log(f"致命：{m}（已停下，不重试；看桌宠右键菜单）"),
    )
    gw.start()
    log(f"跑起来了（{'会回复' if reply_enabled else '只收不发'}）。Ctrl+C 停。")
    return app.exec()


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "selftest":
        sys.exit(selftest())
    elif args and args[0] == "run":
        sys.exit(run(reply_enabled="--debug" not in args))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
