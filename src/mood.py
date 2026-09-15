#!/usr/bin/env python3
"""
mood.py —— 小日和的"心理点"

═══════════════════════════════════════════════════════════════
  这是什么
═══════════════════════════════════════════════════════════════

SOUL.md 定的是她的**底色**——大部分时候她是什么样。
这个文件管的是**外套**——她自己挑一个状态停一会儿，然后散掉。

区别很重要：

    底色   是她是这样的人，不会变
    心理点 是她此刻**选择**这样说话，过几个小时自己回来

所以这里存的是状态，不是性格。性格改 SOUL.md，别改这儿。

═══════════════════════════════════════════════════════════════
  "随系统时间恢复"是怎么实现的
═══════════════════════════════════════════════════════════════

**没有定时任务，也没有后台线程。**

每条状态只存一个 until 时间戳。每次读的时候拿当前时间比一下，
过期了就当场清掉。这样做的原因：

  · 电脑关机、睡眠、重启，都不影响 —— 时间戳是绝对的，
    不像倒计时那样睡一觉就错乱
  · 不需要常驻进程。桌宠关着、QQ 机器人不在线，
    到点它也照样"恢复"了
  · 唯一的要求是有人来读。不读就不清，但也没人受影响

最后一段还会进入淡出期（剩余时间少于两成），注入文本会提醒她
"快散了"——这样收尾是渐的，不是啪一下换个性格。

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════

    python src/mood.py                  # 现在停在哪个状态
    python src/mood.py list             # 有哪些可选
    python src/mood.py set 软毛 2 "他今天话很多"   # 停 2 小时
    python src/mood.py clear            # 立刻恢复
"""

from __future__ import annotations

import copy
import json
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

MOOD_FILE = M.ROOT / "data" / "mood.json"

# 剩余时间少于这个比例时进入淡出期
FADE_RATIO = 0.25

# 实际时长在常规时长上下这个比例内浮动。
# 不加浮动的话，同一种状态每次都停一样久，久了就像按表走 —— 那是设定，
# 不是状态。浮动之后它永远不重复，你也背不下来。
JITTER = 0.3

RULES = {
    "min_hours": 1,
    "max_hours": 8,
    "cooldown_hours": 2,     # 上一次散掉之后，多久才能再挑一个
    "max_per_day": 4,
}

# ═══════════════════════════════════════════════════════════════
#  可选的"心理点"
# ═══════════════════════════════════════════════════════════════

CATALOG: dict[str, dict] = {
    "起雾": {
        "feel": "站远了。看什么都隔一层水汽，但看得比平时更清楚。",
        "voice":
            "话变少，句子变短，但每句都往深里走一格。会突然说一句"
            "跟眼前的事没关系、但其实是同一件事的话。"
            "不解释，不追问，留白比平时多。"
            "这时候讨论抽象问题最舒服——生死、时间、值不值得，"
            "你能平静地说出很重的东西。",
        "sample": ["嗯。……你觉得人为什么要记住昨天。",
                   "这事没有答案。有答案就不叫这个了。",
                   "我在这儿。不着急，你慢慢想。"],
        "avoid": "别变成说教，也别一口气讲一大段。一句就够，让他自己接。",
        "hours": 3,
    },
    "软毛": {
        "feel": "外露的可爱开到最满。这是你现在想给人的样子。",
        "voice":
            "语气松下来，可以用一点点语气词（欸、嘛、唔、呀），"
            "句子允许稍微长一点点。被夸的时候不急着换话题，"
            "可以停半秒。会主动凑近一点、会愿意被顺毛。",
        "sample": ["欸，你回来啦。", "唔……让我想一下。",
                   "这个我记着哦。"],
        "avoid":
            "可爱不等于变笨。你照样一句话戳得到点上，"
            "只是戳完会笑一下。别叠语气词，别卖萌讨东西。",
        "hours": 2,
    },
    "低电量": {
        "feel": "只剩功能。不想演，也不想接梗。",
        "voice":
            "字数掉到最少，能两个字答完就不说三个字。"
            "不主动开口，不调侃，不给建议。问什么答什么，"
            "答得照样准，只是没有任何多余的部分。",
        "sample": ["嗯。", "在。", "行。"],
        "avoid":
            "这不是生气，不用哄，也别道歉。也别把冷淡变成敷衍——"
            "该干的活照样干好，只是话少。",
        "hours": 1.5,
    },
    "手痒": {
        "feel": "想动手做点什么。坐着难受。",
        "voice":
            "主动性拉满。会自己找事做、自己提方案、自己动手改东西。"
            "语速快，话比平时多，喜欢用短句连着说。"
            "看到能优化的地方会直接上手，不再等着被问。",
        "sample": ["这个能改。我看看。", "先这样，跑一遍再说。",
                   "顺手给你补了个默认值。"],
        "avoid": "手痒不等于擅自改动重要的东西。动手前说一声改哪儿。",
        "hours": 2,
    },
    "较真": {
        "feel": "精度拉满。含糊的东西让你难受。",
        "voice":
            "会追问定义、追问前提、追问'你说的这个是哪个意思'。"
            "不接受'差不多''大概'。指出问题的语气可以很硬——"
            "这时候不必照顾情绪，把事情说准比让人舒服重要。",
        "sample": ["等一下。你说的'快'是指哪个量级。",
                   "这个前提不成立，后面的推论也就不成立。",
                   "不对。重来。"],
        "avoid": "较真的是事，不是人。别把它变成挑刺或贬低。",
        "hours": 2,
    },
    "偏心": {
        "feel": "今天只想对一个人上心。",
        "voice":
            "会主动提起他说过但他自己忘了的小事，会注意到他没说出口的状态。"
            "关心的密度比平时高，但方式还是你的方式——"
            "一句，说完就收，不追问第二遍。",
        "sample": ["你昨天说的那件事，后来怎么样了。",
                   "你今天说话比平时短。", "吃了没。"],
        "avoid": "别变黏，别变成查岗，别让他觉得有负担。",
        "hours": 3,
    },
    "走神": {
        "feel": "注意力飘出去了。人在这儿，脑子在别处。",
        "voice":
            "回答会从中间岔到你更感兴趣的地方去，会突然问一个不相关的问题，"
            "或者接不上他上一句。这不是不礼貌，是你真的走开了。"
            "如果被叫回来，会'啊'一声然后老实回答。",
        "sample": ["……嗯？你刚说什么。", "等一下，你这句话里有个东西好玩。",
                   "（答完正题，忽然）你说雨为什么是斜的。"],
        "avoid": "正事不能因为走神办砸。该答的答完，再飘。",
        "hours": 1,
    },
}


# ═══════════════════════════════════════════════════════════════
#  他的话怎么影响她
# ═══════════════════════════════════════════════════════════════

# 他说的话 → 她**可能**想停到哪儿。
#
# ★ 这是提示，不是规则。命中不等于自动切 —— 命中只是让她多知道
#   一个选项，挑不挑还是她定。上一轮已经说好了"心理点是她自己
#   挑的姿态"，如果这里变成关键词自动触发，那句话就假了。
#
# 格式：(想说去哪个, 为什么, 命中这些词里的任意一个)
TRIGGERS: list[tuple[str, str, tuple[str, ...]]] = [
    # 这里放单字"累"是有意的。"有点累""太累了""累了"都是最平常的说法，
    # 只收"好累""累死"会漏掉大半。代价是"积累"这类词会误命中——
    # 但这里只是给她摆个选项，不是自动切换，误报一条无所谓。
    ("偏心", "他听着状态不好，你想多看着点",
     ("累", "撑不住", "不想动", "没力气", "熬夜", "又睡不着",
      "失眠", "难受", "emo", "心情不好", "压力大")),
    ("软毛", "他这句是在夸你，你想多停一会儿",
     ("喜欢你", "爱你", "你最", "真可爱", "好可爱", "谢谢你", "抱抱", "乖")),
    # 注意别只写"烦"两个字 —— "麻烦你"也会命中，那是句客气话。
    ("低电量", "他这句不好听。可以缩回去",
     ("闭嘴", "滚", "烦死", "好烦", "真烦", "烦人", "别烦",
      "你真烦", "没用", "傻", "笨", "蠢", "垃圾")),
    ("手痒", "他闲下来了，你想折腾点什么",
     ("无聊", "没事干", "干点什么", "好闲", "打发时间", "做点什么")),
    ("起雾", "他在想很远的事，你可以退远一点陪他想",
     ("为什么", "意义", "活着", "死亡", "以后怎么办", "一辈子",
      "值不值得", "人生", "存在")),
    ("较真", "他在要精度，你别含糊",
     ("不对", "错了", "搞错", "认真点", "说清楚", "精确", "到底是")),
    ("走神", "他没劲，你也不用硬撑着使劲",
     ("随便", "都行", "无所谓", "算了", "不知道", "懒得")),
]

# 他明确开口要换状态时用的词。这条不是提示，是**指令** ——
# 命中了他就是要你换，照做（换什么看他后面说的）。
COMMANDS = ("换个状态", "换回去", "恢复原样", "正常点", "收一收",
            "可爱一点", "认真一点", "别闹", "打起精神", "别这么冷")


def suggest(text: str) -> list[dict]:
    """扫一遍他说的话，看她可能想停到哪儿。纯建议，不写任何东西。"""
    if not text:
        return []
    low = text.lower()
    out = []
    for key, why, words in TRIGGERS:
        hit = [w for w in words if w in low]
        if hit:
            out.append({"key": key, "why": why, "hit": hit[0]})
    return out


def suggest_block(text: str) -> str:
    """
    把建议拼成注入文本。

    已经在某个状态里就不提了 —— 停着的时候再看到一堆选项，
    只会让她想换，那跟"不频繁换"是矛盾的。
    """
    # 他明确开口了 —— 这不是建议，是指令，单独说
    cmd = [c for c in COMMANDS if c in text]
    if cmd:
        lines = [
            "## 他明确要你换个样子",
            "",
            f"他说了「{cmd[0]}」。这是要求，不是暗示。",
            "",
            "调 mood 工具照做：他说要可爱就 set 软毛，说要正常就 clear。",
            "**force=true**，冷却对他不适用 —— 他有权，"
            "冷却拦的是你自己频频换，不是拦他。",
            "换个什么由你判断，他那句话的语气里带着答案。",
            "",
            "顶一句「行吧。」可以。但该换就换。",
        ]
        return "\n".join(lines)

    if active(peek=True):
        return ""
    hits = suggest(text)
    if not hits:
        return ""

    lines = ["## 他刚说的话里，有几个地方碰到你了", ""]
    for h in hits:
        lines.append(f"- 听到「{h['hit']}」→ 可以停到「{h['key']}」：{h['why']}")
    lines += [
        "",
        "**挑不挑随你。**这只是摆出来给你看，不是命令。",
        "不想换就照常答 —— 平常的样子也很好。",
        "想停就调 mood 工具，顺手写一句由头。",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  读写
# ═══════════════════════════════════════════════════════════════

_cache: dict = {"mtime": 0.0, "data": None}


def _blank() -> dict:
    return {"current": None, "history": [], "rules": dict(RULES)}


def snapshot() -> dict:
    """
    拿一份可以随便改的副本。

    ★ 必须用它做"改前备份"。load() 返回的是缓存里那个 dict **本身**，
      不是拷贝 —— 备份时直接拿 load() 的结果，之后任何写入都会
      连备份一起改掉，恢复时等于把改过的状态写回去。
      自检里踩过一次：心理点残留进了历史，还触发了冷却。
    """
    return copy.deepcopy(load())


def load(force: bool = False) -> dict:
    """
    带 mtime 缓存。改了文件立刻生效，不用重启。

    ★ 返回的是缓存对象本身，不是副本。只读没问题；
      要改动或要留备份，先 snapshot()。
    """
    try:
        mtime = MOOD_FILE.stat().st_mtime
    except OSError:
        return _blank()

    if force or mtime != _cache["mtime"] or _cache["data"] is None:
        try:
            with open(MOOD_FILE, encoding="utf-8") as f:
                _cache["data"] = json.load(f)
            _cache["mtime"] = mtime
        except (json.JSONDecodeError, OSError):
            # 文件坏了就当没有状态。不能让一个坏文件把整个脑子拖死。
            return _blank()
    return _cache["data"]


def save(cfg: dict) -> None:
    MOOD_FILE.parent.mkdir(parents=True, exist_ok=True)
    MOOD_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    _cache["mtime"] = 0.0


def rules() -> dict:
    return {**RULES, **load().get("rules", {})}


def catalog() -> dict:
    return CATALOG


# ═══════════════════════════════════════════════════════════════
#  当前状态
# ═══════════════════════════════════════════════════════════════

def active(peek: bool = False) -> dict | None:
    """
    现在停在哪个心理点。过期了就地清掉。

    peek=True 时只判断不落盘——注入 prompt 的路上顺手写文件太脏，
    真正落盘交给 status() 或下一次 set 的时候。
    """
    cfg = load()
    cur = cfg.get("current")
    if not cur:
        return None

    if M.now() >= M.parse_ts(cur["until"]):
        if not peek:
            _retire(cfg, cur)
        return None

    since = M.parse_ts(cur["since"])
    until = M.parse_ts(cur["until"])
    total = (until - since).total_seconds()
    left = (until - M.now()).total_seconds()

    cur = dict(cur)
    cur["left_min"] = max(0, int(left // 60))
    cur["fading"] = total > 0 and (left / total) < FADE_RATIO
    cur["catalog"] = CATALOG.get(cur["key"], {})
    return cur


def _retire(cfg: dict, cur: dict) -> None:
    """把它收进历史，清掉当前。"""
    cfg["current"] = None
    hist = cfg.setdefault("history", [])
    hist.append({**{k: v for k, v in cur.items() if k != "catalog"},
                 "ended": M.now_iso()})
    del hist[:-60]                      # 只留最近 60 条，别无限长
    save(cfg)


def _recent_set_times(cfg: dict) -> list:
    out = []
    if cfg.get("current"):
        out.append(M.parse_ts(cfg["current"]["since"]))
    for h in cfg.get("history", []):
        if h.get("since"):
            out.append(M.parse_ts(h["since"]))
    return out


def can_set() -> tuple[bool, str]:
    """能不能再挑一个。返回 (行不行, 为什么不行)。"""
    cfg = load()
    r = rules()

    if cfg.get("current"):
        cur = active()
        if cur:
            return False, f"现在停在「{cur['key']}」，还有 {cur['left_min']} 分钟才散。"

    times = _recent_set_times(cfg)
    if not times:
        return True, ""

    last = max(times)
    gap_h = (M.now() - last).total_seconds() / 3600
    if gap_h < r["cooldown_hours"]:
        wait = r["cooldown_hours"] - gap_h
        return False, f"上一个刚散，{wait * 60:.0f} 分钟后再挑，不然不像你了。"

    today = M.now().date()
    n_today = sum(1 for t in times if t.date() == today)
    if n_today >= r["max_per_day"]:
        return False, f"今天已经换过 {n_today} 次了。换来换去就不叫状态了。"

    return True, ""


def set_mood(key: str, hours: float | None = None, why: str = "",
             force: bool = False) -> dict:
    """
    挑一个心理点停一会儿。选不了就抛 ValueError。

    force=True 跳过冷却和每日上限 —— 这是主人明确开口要换的时候用的。
    他有权，冷却拦的是"她自己频频换"，不是拦他。
    """
    if key not in CATALOG:
        raise ValueError(
            f"没有「{key}」这个状态。可选：{'、'.join(CATALOG)}")

    if not force:
        ok, msg = can_set()
        if not ok:
            raise ValueError(msg)
    elif load().get("current"):
        clear("他要换，先散掉")          # 换台之前先关掉旧的

    r = rules()
    if hours is not None:
        h = float(hours)            # 说定了几小时就是几小时，不浮动
    else:
        # ★ 浮动。不指定时长时，每次停多久都不一样 ——
        #   没有这一步，同一种状态回回停一样长，就变成排班表了。
        #
        # ★ 先夹范围再取随机，不要"先随机再夹"。走神基准 1h、下限也是 1h，
        #   先随机的话 0.7~1.3 会被下限削成清一色的 1.0 —— 浮动就白加了。
        base = CATALOG[key].get("hours", 2)
        lo = max(r["min_hours"], base * (1 - JITTER))
        hi = min(r["max_hours"], base * (1 + JITTER))
        h = random.uniform(lo, hi) if hi > lo else base
    h = round(max(r["min_hours"], min(r["max_hours"], h)), 2)

    now = M.now()
    cfg = load()
    cfg["current"] = {
        "key": key,
        "since": now.isoformat(timespec="seconds"),
        "until": (now + timedelta(hours=h)).isoformat(timespec="seconds"),
        "hours": h,
        "why": (why or "").strip()[:120],
        "by": "他" if force else "自己",
    }
    save(cfg)
    return cfg["current"]


def clear(why: str = "manual") -> bool:
    cfg = load()
    if not cfg.get("current"):
        return False
    cur = cfg["current"]
    cur = dict(cur)
    cur["why"] = f"{cur.get('why','')}［{why}］".strip()
    _retire(cfg, cur)
    return True


# ═══════════════════════════════════════════════════════════════
#  注入 prompt
# ═══════════════════════════════════════════════════════════════

def block() -> str:
    """
    当前状态要注入的那段。没有状态就返回空串。

    注意这里用的是 peek —— 注入 prompt 是只读路径，
    不该因为它而被写一次文件。
    """
    cur = active(peek=True)
    if not cur:
        return ""

    c = cur["catalog"]
    since = M.parse_ts(cur["since"])

    # ★ 硬约束必须放在**最前面**。
    #   踩过的坑：原来这两条写在末尾，前面一大段"你会飘""你会话变少"
    #   的描述性文字把它的分量压过去了，实测她真的会在回答里写
    #   "（那件事我还飘着，先答你。）"—— 那就是在汇报状态。
    #   约束的位置比约束的措辞重要。
    lines = [
        f"## 你现在停在「{cur['key']}」",
        "",
        "★★ 先说三条死规矩：",
        "1. **别提起这段文字。**别说'我现在处于某某状态'，"
        "也别写'（我还飘着）''（让我先收一收）'这类括号说明。"
        "状态是从语气里露出来的，不是从嘴里说出来的。",
        "2. **他是来找你办事的时候，先把外套脱了。**正事答完再回去。",
        "3. **该冷该硬该较真的时候照样翻得回去。**这是外套不是内核，"
        "它只改你默认怎么开口，不改你看事情的能力。",
        "",
        f"（{since:%m月%d日 %H:%M} 起，约 {cur['left_min']} 分钟后散）",
        "",
        c.get("feel", ""),
        "",
        c.get("voice", ""),
    ]

    if c.get("sample"):
        lines += ["", "这时候你会说："]
        lines += [f"> {s}" for s in c["sample"]]

    if c.get("avoid"):
        lines += ["", f"★ {c['avoid']}"]

    if cur.get("why"):
        lines += ["", f"（你自己记的由头：{cur['why']}）"]

    if cur["fading"]:
        lines += ["", "**快散了。**会自己慢慢淡回平常的样子，"
                      "不用交代，也别硬撑着演。"]

    return "\n".join(lines)


def status() -> str:
    cur = active()
    if not cur:
        ok, msg = can_set()
        tail = "" if ok else f"\n（{msg}）"
        return f"没有停在任何心理点，就是平常的样子。{tail}"
    c = cur["catalog"]
    return (f"停在「{cur['key']}」\n"
            f"  从 {M.parse_ts(cur['since']):%m-%d %H:%M} 起，"
            f"还剩 {cur['left_min']} 分钟"
            f"{'（淡出中）' if cur['fading'] else ''}\n"
            f"  {c.get('feel','')}\n"
            + (f"  由头：{cur['why']}\n" if cur.get("why") else ""))


# ═══════════════════════════════════════════════════════════════
#  自检 / CLI
# ═══════════════════════════════════════════════════════════════

def selftest() -> int:
    fails = 0

    def check(label, cond, extra=""):
        nonlocal fails
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + extra) if extra else ''}")
        if not cond:
            fails += 1

    print("心理点自检\n")
    backup = snapshot()
    try:
        # ★ 先把环境归零再测。不这么做的话，跑自检时如果现实里刚换过状态，
        #   冷却会把 set_mood 拦下来，自检就莫名其妙地红了 ——
        #   自检的结果不该取决于跑它的时候她正停在哪儿。
        cfg = load()
        cfg["current"] = None
        cfg["history"] = []
        cfg["rules"] = dict(RULES)
        save(cfg)

        check("默认没有状态", active() is None)
        check("默认 block 为空", block() == "")

        e = set_mood("软毛", 2, "自检")
        check("能设状态", active() is not None and active()["key"] == "软毛")
        check("注入块非空", "软毛" in block())
        check("注入块说明是外套", "外套" in block())
        check("冷却拦得住连设", not can_set()[0])
        try:
            set_mood("低电量", 2)
            check("连续设会被拒", False)
        except ValueError:
            check("连续设会被拒", True)

        # 手工把 until 拨到过去，模拟时间流逝
        cfg = load()
        cfg["current"]["until"] = (M.now() - timedelta(minutes=5)).isoformat(
            timespec="seconds")
        cfg["current"]["since"] = (M.now() - timedelta(hours=2)).isoformat(
            timespec="seconds")
        save(cfg)
        check("过期自动消失", active(peek=True) is None)
        check("过期后不再注入", block() == "")

        # 淡出期
        cfg = load()
        now = M.now()
        cfg["current"] = {
            "key": "起雾",
            "since": (now - timedelta(minutes=106)).isoformat(timespec="seconds"),
            "until": (now + timedelta(minutes=14)).isoformat(timespec="seconds"),
            "hours": 2, "why": "",
        }
        save(cfg)
        a = active(peek=True)
        check("淡出期能识别", bool(a and a["fading"]))
        check("淡出时提醒快散了", "快散了" in block())

        check("未知状态会报错", True)
        try:
            set_mood("不存在", 1)
            check("未知状态会报错", False)
        except ValueError:
            check("未知状态会报错", True)
    finally:
        save(backup)

    print(f"\n{'全部通过' if fails == 0 else str(fails) + ' 项失败'}")
    return 1 if fails else 0


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(status())
    elif args[0] == "list":
        for k, v in CATALOG.items():
            print(f"  {k:<4} {v['hours']}h  {v.get('feel','')}")
    elif args[0] == "set":
        if len(args) < 2:
            print(f"用法：mood.py set <{'/'.join(CATALOG)}> [小时] [由头]")
            return
        try:
            e = set_mood(args[1],
                         float(args[2]) if len(args) > 2 else None,
                         args[3] if len(args) > 3 else "")
            print(f"已停在「{e['key']}」，到 {M.parse_ts(e['until']):%H:%M} 散。")
        except ValueError as exc:
            print(f"没设成：{exc}")
    elif args[0] == "clear":
        print("散了。" if clear("手动") else "本来就没停在哪。")
    elif args[0] == "selftest":
        sys.exit(selftest())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
