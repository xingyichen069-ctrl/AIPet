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

所以群里固定走 daily，而且 max_tokens 在**调用之前**就压到 300 ——
不能指望"生成完了再截断"，那样时间已经花掉了。

═══ 用法 ═══
    python src/qq_bridge.py selftest      # 灌假事件，不联网
    python src/qq_bridge.py run           # 真正跑（连网+回话）
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402
import people as P  # noqa: E402
import qq_bot as QB  # noqa: E402
import qq_text as QT  # noqa: E402

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
#   踩过一次：设成 300，思维链吃掉 158~288，正文只剩几十 token，
#   有时在想到一半就被砍断，出来就是"逻辑不通"。
#   实测思维链稳定在 150~300 tok，正文按 300 中文字算约 300 tok，
#   留够余量给 700。
GROUP_MAX_TOKENS = 700

# 单聊没有 5 分钟的紧迫感，但输出长度限制是一样的
C2C_LEVEL = None            # None = 跟随 thinking.json
C2C_MAX_TOKENS = 700

# ★ 给模型看的长度上限，必须和 qq_text.QQ_SAFE_BYTES 对得上。
#   以前这里写 500 字、那边按 1000 字节截断 —— 500 个中文字是 1500 字节，
#   她老老实实照 500 字写，反而会被砍掉三分之二。
#   现在统一成 300 字（约 900 字节，留一点余量）。
REPLY_CHARS_HINT = 300

# 群里最多记多长的临时笔记
NOTE_MAX_CHARS = 120

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
    items = _hist_load().get(key) or []
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
        "该给的理由给完，然后停。"
    )

    system = "\n\n---\n\n".join(p for p in [persona, ctx, platform] if p)
    return system, {"level": level}


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
        self._lock = threading.Lock()

    def handle(self, ev: QB.QQEvent) -> None:
        if ev.kind not in ("group_at", "c2c"):
            log(f"忽略事件类型 {ev.kind}")
            return
        if not (ev.content or "").strip():
            return
        t = threading.Thread(target=self._run, args=(ev,), daemon=True)
        t.start()

    # ── 真正干活 ──
    def _run(self, ev: QB.QQEvent) -> None:
        t0 = time.time()
        # 同一个会话串行，免得两条消息同时改记忆
        key = f"{ev.scene}:{ev.group_openid or ev.user_openid}"
        with self._lock:
            if key in self._busy:
                log("上一条还在处理，这条跳过")
                return
            self._busy.add(key)
        try:
            self._process(ev, t0)
        except Exception as e:
            import traceback
            log(f"处理出错：{type(e).__name__}: {e}")
            log(traceback.format_exc()[-800:])
        finally:
            with self._lock:
                self._busy.discard(key)

    def _process(self, ev: QB.QQEvent, t0: float) -> None:
        who = identify(ev)
        log(f"{'主人' if who['is_owner'] else '群友'} {who['name']!r}：{ev.content[:40]}")

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

        if ev.content.strip().lower() in IGNORE_EXACT:
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
        try:
            reply, _reasoning, info = B.ask_with_system(
                prompt, system,
                level=meta["level"], max_tokens=budget)
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
        #   正文在想到一半被砍断 —— 症状是"逻辑不通"。
        check("★ max_tokens 留得下思维链 + 正文",
              GROUP_MAX_TOKENS >= 600,
              f"{GROUP_MAX_TOKENS} = 思维链约 300 + 正文约 {REPLY_CHARS_HINT}")

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
        kept = [e for e in M.load_journal() if e.get("source") != "qq"]
        M.save_journal(kept)

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
