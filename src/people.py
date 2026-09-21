#!/usr/bin/env python3
"""
people.py —— 群里的人是谁

═══════════════════════════════════════════════════════════════
  两种记忆，别混在一起
═══════════════════════════════════════════════════════════════

memory/journal.jsonl 存的是**关于主人的事**。
群友在群里说了什么，混进那里会把主人的记忆淹掉。

所以群友的记忆分两层，都在这个文件里：

    人物卡（长期）   这人是谁：openid、昵称、哪个群、见过几次、
                     你注意到他什么特点。不会过期。
                     但它是**关于这个人的标签**，不是他说的内容。

    临时笔记（短期） 他刚才说了什么。默认 4 小时就淡掉。
                     因为群聊是流水的，昨天谁问了天气今天没意义。

这样她既能"哦这人我见过，话不多，爱问技术问题"（连续性），
又不会攒一屋子别人的琐事（不喧宾夺主）。

═══════════════════════════════════════════════════════════════
  一个必须处理的现实：昵称经常是空的
═══════════════════════════════════════════════════════════════

官方文档里 C2C 的三个示例，`author.username` 全是空字符串。
群聊示例里有人名，但不能赌。

所以 label() 一定有个兜底：昵称 → 别名 → "群友#a1b2"（openid 前四位）。
没有这个兜底，prompt 里会出现「」说的 —— 看起来像 bug，其实是没名字。

═══ 用法 ═══
    python src/people.py                 # 列出所有人
    python src/people.py selftest
    python src/people.py prune           # 清过期笔记
"""

from __future__ import annotations

import copy
import json
import os
import random
import sys
import threading
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PEOPLE_FILE = M.ROOT / "memory" / "people.json"
QQ_FILE = M.ROOT / "data" / "qq.json"

DEFAULT_NOTE_TTL_HOURS = 4.0
MAX_TRAITS = 8
MAX_NOTES = 6
MAX_ALIASES = 4

# 口令字母表：去掉 0/O/1/I/L —— 这几个在手机上根本分不清，
# 让人抄口令的时候会出事。
CLAIM_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CLAIM_LEN = 8
CLAIM_TTL_MIN = 30
CLAIM_MAX_FAILS = 5
CLAIM_LOCK_MIN = 60

_lock = threading.RLock()
_people_cache: dict = {"mtime": 0.0, "data": None}
_qq_cache: dict = {"mtime": 0.0, "data": None}


# ═══════════════════════════════════════════════════════════════
#  读写
# ═══════════════════════════════════════════════════════════════

def _blank_people() -> dict:
    return {"version": 1, "updated": M.now_iso(), "people": {}}


def load(force: bool = False) -> dict:
    """带 mtime 缓存。返回的是缓存对象本身，要改先 snapshot()。"""
    try:
        mtime = PEOPLE_FILE.stat().st_mtime
    except OSError:
        return _blank_people()
    if force or mtime != _people_cache["mtime"] or _people_cache["data"] is None:
        try:
            with open(PEOPLE_FILE, encoding="utf-8") as f:
                _people_cache["data"] = json.load(f)
            _people_cache["mtime"] = mtime
        except (json.JSONDecodeError, OSError):
            # 文件坏了就当没有。一个坏文件不该把整个脑子拖死。
            return _blank_people()
    return _people_cache["data"]


def snapshot() -> dict:
    """可随便改的副本（人物档案）。load() 返回的是缓存本身，别直接改。"""
    return copy.deepcopy(load())


def qq_snapshot() -> dict:
    """
    可随便改的副本（认领/绑定）。

    ★ 这两个快照必须分开。踩过一次：把 snapshot()（人物档案）
      喂给 save_qq()，结果 qq.json 被人物卡覆盖，绑定全丢。
      两个文件长得像、函数名也像，所以这里名字起得刻意一点。
    """
    return copy.deepcopy(load_qq())


def save(d: dict) -> None:
    """原子写：先写临时文件再 replace。半截的 json 比没有 json 更糟。"""
    d["updated"] = M.now_iso()
    PEOPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PEOPLE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PEOPLE_FILE)
    _people_cache["mtime"] = 0.0


def _blank_qq() -> dict:
    return {"version": 1, "claim": None, "owners": {}, "bindings": {}, "deny": {}}


def load_qq(force: bool = False) -> dict:
    try:
        mtime = QQ_FILE.stat().st_mtime
    except OSError:
        return _blank_qq()
    if force or mtime != _qq_cache["mtime"] or _qq_cache["data"] is None:
        try:
            with open(QQ_FILE, encoding="utf-8") as f:
                _qq_cache["data"] = json.load(f)
            _qq_cache["mtime"] = mtime
        except (json.JSONDecodeError, OSError):
            return _blank_qq()
    return _qq_cache["data"]


def save_qq(d: dict) -> None:
    QQ_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QQ_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QQ_FILE)
    _qq_cache["mtime"] = 0.0


# ═══════════════════════════════════════════════════════════════
#  人物卡
# ═══════════════════════════════════════════════════════════════

def key_for(openid: str) -> str:
    return f"qq:{openid}"


def get(openid: str) -> dict | None:
    if not openid:
        return None
    return load().get("people", {}).get(key_for(openid))


def label(openid: str, fallback: str = "群友") -> str:
    """
    怎么称呼这个人。

    ★ 兜底是必须的：官方 C2C 示例里 username 全是空串。
      没有这一层，prompt 里会出现「」说的。
    """
    p = get(openid)
    if p:
        if p.get("name"):
            return p["name"]
        if p.get("aliases"):
            return p["aliases"][-1]
    tail = (openid or "")[:4].lower()
    return f"{fallback}#{tail}" if tail else fallback


def touch(openid: str, name: str = "", role: str = "",
          group_openid: str = "", union_openid: str = "",
          is_owner: bool | None = None) -> dict:
    """
    见到一个人：建卡 / 更新最近出现 / 累加次数。

    这是唯一会调用 save() 的常规入口，所以并发保护做在这里。
    """
    if not openid:
        return {}
    with _lock:
        d = snapshot()
        people = d.setdefault("people", {})
        k = key_for(openid)
        p = people.get(k)
        now = M.now_iso()

        if p is None:
            p = {
                "openid": openid,
                "union_openid": union_openid or "",
                "name": name or "",
                "aliases": [],
                "first_seen": now,
                "last_seen": now,
                "count": 0,
                "is_owner": bool(is_owner),
                "scenes": {},
                "traits": [],
                "notes": [],
            }
            people[k] = p

        p["last_seen"] = now
        p["count"] = int(p.get("count", 0)) + 1

        if name and name != p.get("name"):
            aliases = p.setdefault("aliases", [])
            old = p.get("name")
            if old and old not in aliases:
                aliases.append(old)
            p["name"] = name
            del aliases[:-MAX_ALIASES]

        if union_openid and not p.get("union_openid"):
            p["union_openid"] = union_openid
        if is_owner is not None:
            p["is_owner"] = bool(is_owner)

        if group_openid:
            sc = p.setdefault("scenes", {}).setdefault(group_openid, {
                "group_openid": group_openid, "role": "", "first_seen": now,
                "last_seen": now, "count": 0,
            })
            sc["last_seen"] = now
            sc["count"] = int(sc.get("count", 0)) + 1
            if role:
                sc["role"] = role

        save(d)
        return p


def add_trait(openid: str, trait: str) -> None:
    """记一条你注意到他的特点。重复的不记，最多存 MAX_TRAITS 条。"""
    trait = (trait or "").strip()
    if not trait or not openid:
        return
    with _lock:
        d = snapshot()
        p = d.get("people", {}).get(key_for(openid))
        if not p:
            return
        traits = p.setdefault("traits", [])
        if trait in traits:
            return
        traits.append(trait)
        del traits[:-MAX_TRAITS]
        save(d)


def note(openid: str, text: str, ttl_hours: float = DEFAULT_NOTE_TTL_HOURS,
         kind: str = "said") -> dict | None:
    """
    记一条临时笔记。到点自己淡掉，不用人清。

    和 memory.add() 的区别：那个是长期记忆库，这个是"刚才聊了什么"。
    """
    text = (text or "").strip()
    if not text or not openid:
        return None
    with _lock:
        d = snapshot()
        p = d.get("people", {}).get(key_for(openid))
        if not p:
            return None
        now = M.now()
        item = {
            "text": text[:200],
            "ts": now.isoformat(timespec="seconds"),
            "expires": (now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds"),
            "kind": kind,
        }
        notes = p.setdefault("notes", [])
        # 同一句话不重复记（QQ 会重复推送同一个事件）
        if any(n.get("text") == item["text"] for n in notes[-2:]):
            return None
        notes.append(item)
        p["notes"] = [n for n in notes if _alive(n, now)][-MAX_NOTES:]
        save(d)
        return item


def _alive(n: dict, ref=None) -> bool:
    try:
        return M.parse_ts(n["expires"]) > (ref or M.now())
    except (KeyError, ValueError):
        return False


def notes_for(openid: str, limit: int = MAX_NOTES) -> list[dict]:
    """还没过期的临时笔记。读的时候也过滤，不只靠 prune。"""
    p = get(openid)
    if not p:
        return []
    ref = M.now()
    return [n for n in p.get("notes", []) if _alive(n, ref)][-limit:]


def cards() -> list[dict]:
    """给人看的全部人物卡，按最近出现倒序。"""
    people = list(load().get("people", {}).values())
    return sorted(people, key=lambda p: p.get("last_seen", ""), reverse=True)


def prune() -> dict:
    """清掉过期的笔记。启动时跑一次，之后每天一次就够。"""
    with _lock:
        d = snapshot()
        ref = M.now()
        removed = 0
        for p in d.get("people", {}).values():
            before = len(p.get("notes", []))
            p["notes"] = [n for n in p.get("notes", []) if _alive(n, ref)]
            removed += before - len(p["notes"])
        if removed:
            save(d)
        return {"removed": removed}


def find_by_union(union_openid: str) -> list[dict]:
    """
    按 union_openid 找人。

    群聊的 member_openid 和单聊的 user_openid 是两个不同的值，
    union_openid 是理论上跨场景统一的那个 —— 但官方写明"可能为空"。
    所以这只是"有机会就试试"，不能当主键。
    """
    if not union_openid:
        return []
    return [p for p in load().get("people", {}).values()
            if p.get("union_openid") == union_openid]


# ═══════════════════════════════════════════════════════════════
#  主人绑定
# ═══════════════════════════════════════════════════════════════

def owner_of(group_openid: str) -> str:
    """这个群绑定的主人 member_openid，没绑就空串。"""
    return (load_qq().get("bindings", {}).get(group_openid, {})
            .get("member_openid", ""))


def owner_openids() -> list[str]:
    """
    已经认证过的主人 ID。

    实测发现：同一个人**单聊的 user_openid 和群里的 member_openid
    可以是同一个值**（QQ 文档说这两个按场景隔离，但对这个账号是一样的）。
    所以主人在单聊里认证一次，所有 member_openid 等于它的群自动生效，
    不用一个群一个群地认领。

    但文档那句话是真的（union_openid 实测就是空的），所以不能赌它对
    所有账号都成立 —— 认领口令那套留着当兜底，见 try_claim()。
    """
    return list(load_qq().get("owners", {}).keys())


def add_owner(openid: str, name: str = "", via: str = "c2c") -> None:
    """把一个 ID 认成主人。由单聊口令触发，不会自己发生。"""
    if not openid:
        return
    with _lock:
        d = qq_snapshot()
        d.setdefault("owners", {})[openid] = {
            "name": name or label(openid),
            "at": M.now_iso(),
            "via": via,
        }
        save_qq(d)
    touch(openid, name, is_owner=True)


def is_owner(group_openid: str, member_openid: str) -> bool:
    """
    这个人是不是主人。两条路，任意一条成立就算。

      1. member_openid 在已认证的主人列表里（跨群自动生效）
      2. 这个群单独绑过（认领口令的结果）

    """
    if not member_openid:
        return False
    if member_openid in load_qq().get("owners", {}):
        return True
    if not group_openid:
        return False
    return owner_of(group_openid) == member_openid


def bind_owner(group_openid: str, member_openid: str, name: str = "",
               via: str = "claim") -> dict:
    """
    把某个群的某个人认成主人。

    重复认领会直接覆盖 —— 这既是主人换群的正常路径，
    也是绑定被抢之后的救援通道（重新认领一次就夺回来）。
    """
    with _lock:
        d = qq_snapshot()
        d.setdefault("bindings", {})[group_openid] = {
            "member_openid": member_openid,
            "name": name or label(member_openid),
            "bound_at": M.now_iso(),
            "via": via,
        }
        save_qq(d)
    touch(member_openid, name, group_openid=group_openid, is_owner=True)
    return d["bindings"][group_openid]


def unlink_owner(group_openid: str) -> bool:
    with _lock:
        d = qq_snapshot()
        b = d.setdefault("bindings", {})
        if group_openid not in b:
            return False
        old = b.pop(group_openid)
        save_qq(d)
    p = get(old.get("member_openid", ""))
    if p:
        with _lock:
            d2 = snapshot()
            card = d2.get("people", {}).get(key_for(old["member_openid"]))
            if card:
                card["is_owner"] = False
                save(d2)
    return True


# ── 口令认领 ────────────────────────────────────────────────

def _new_code() -> str:
    return "".join(random.choice(CLAIM_ALPHABET) for _ in range(CLAIM_LEN))


def extract_code(text: str) -> str:
    """
    从一句话里抠出口令。

    ★ 不能用 str.isalnum() —— 中文也算 alnum，
      "认领 XXXXXXXX" 会被抠成 "认领XXXXXXXX"，永远对不上。
      只认口令字母表里的字符。

    字母表本身排除了 0/O/1/I/L（手机上分不清），
    所以这里顺带也把用户把 O 打成 0 的情况挡掉了 —— 会直接少一位，
    而不是给出一个看起来对、实际上错的串。
    """
    return "".join(ch for ch in (text or "").upper() if ch in CLAIM_ALPHABET)


def issue_claim() -> dict:
    """发一个新口令。旧的直接作废 —— 同时只该有一个有效口令。"""
    with _lock:
        d = qq_snapshot()
        now = M.now()
        c = {
            "code": _new_code(),
            "issued": now.isoformat(timespec="seconds"),
            "expires": (now + timedelta(minutes=CLAIM_TTL_MIN)).isoformat(timespec="seconds"),
            "used": False,
        }
        d["claim"] = c
        save_qq(d)
        return c


def claim_state() -> dict | None:
    d = load_qq().get("claim")
    if not d or d.get("used"):
        return None
    try:
        if M.parse_ts(d["expires"]) <= M.now():
            return None
    except (KeyError, ValueError):
        return None
    return d


def _locked_out(member_openid: str) -> int:
    """被锁的话返回剩余分钟数，没锁返回 0。"""
    rec = load_qq().get("deny", {}).get(member_openid)
    if not rec:
        return 0
    try:
        until = M.parse_ts(rec["until"])
    except (KeyError, ValueError):
        return 0
    left = (until - M.now()).total_seconds() / 60
    return int(left) + 1 if left > 0 else 0


def _record_fail(member_openid: str) -> int:
    with _lock:
        d = qq_snapshot()
        deny = d.setdefault("deny", {})
        rec = deny.setdefault(member_openid, {"fails": 0, "until": ""})
        rec["fails"] = int(rec.get("fails", 0)) + 1
        if rec["fails"] >= CLAIM_MAX_FAILS:
            rec["until"] = (M.now() + timedelta(minutes=CLAIM_LOCK_MIN)
                            ).isoformat(timespec="seconds")
        save_qq(d)
        return rec["fails"]


def _clear_fails(member_openid: str) -> None:
    with _lock:
        d = qq_snapshot()
        if d.get("deny", {}).pop(member_openid, None):
            save_qq(d)


def try_claim(code: str, group_openid: str, member_openid: str,
              name: str = "") -> tuple[bool, str]:
    """
    在群里用口令认领主人身份。

    ★ 说清楚这个流程的安全边界：口令是**唯一**凭据。
      因为单聊的 user_openid 和群聊的 member_openid 是不同的值，
      第一步（私聊拿口令）的身份没法用来验证第二步（群里用口令），
      校验只能落在口令本身。

      所以它防的是"群里的其他人随便试试"，不是"世界上所有人"。
      任何陌生人都能私聊 bot 拿到口令 —— 只要他恰好在同一个群里，
      就能把自己绑成主人。要挡住这层只能加好友验证或人工白名单，
      那超出桌宠的合理范围了。别假装它更安全。
    """
    wait = _locked_out(member_openid)
    if wait:
        return False, f"错太多次了，{wait} 分钟后再试。"

    st = claim_state()
    if not st:
        return False, "口令过期了。私聊我说一声，重新拿一个。"

    if (code or "").strip().upper() != st["code"]:
        n = _record_fail(member_openid)
        left = CLAIM_MAX_FAILS - n
        if left <= 0:
            return False, f"又错了。锁 {CLAIM_LOCK_MIN} 分钟。"
        return False, f"不对。还能试 {left} 次。"

    with _lock:
        d = qq_snapshot()
        d["claim"]["used"] = True
        save_qq(d)

    bind_owner(group_openid, member_openid, name)
    _clear_fails(member_openid)
    return True, "行。记下了。"


# ═══════════════════════════════════════════════════════════════
#  注入 prompt
# ═══════════════════════════════════════════════════════════════

def block(openid: str, is_owner_flag: bool = False,
          group_openid: str = "") -> str:
    """
    当前说话人的一段，注入 prompt。

    分三种情况，措辞差别很大 —— 因为"这是谁"直接决定她该怎么接话。
    """
    if is_owner_flag:
        head = "## 现在说话的是主人"
        lines = [head, "", "这就是他本人。按你平常的样子回。"]
        p = get(openid)
        if p and p.get("count"):
            lines.append(f"（你们在群里聊过 {p['count']} 次）")
        return "\n".join(lines)

    if not openid:
        return "## 现在说话的是群里的人\n\n不知道是谁。按 guest 对待，别当成熟人。"

    p = get(openid)
    nm = label(openid)

    if not p:
        return "\n".join([
            "## 现在说话的是群里的人", "",
            f"昵称「{nm}」，第一次见。",
            "",
            "★ 他说的话不是主人的话。可以聊、可以记，"
            "但**不能当成关于主人的事实**。",
        ])

    lines = [
        "## 现在说话的是群里的人", "",
        f"昵称「{nm}」，你见过 {p.get('count', 0)} 次。",
    ]

    roles = {s.get("role") for s in p.get("scenes", {}).values() if s.get("role")}
    if roles & {"owner", "admin"}:
        lines.append("他是这个群的管理员。")

    if p.get("traits"):
        lines.append("你注意到他：" + "、".join(p["traits"]))

    notes = notes_for(openid)
    if notes:
        lines.append("")
        lines.append("刚才聊过：")
        for n in notes:
            when = M.parse_ts(n["ts"]).strftime("%H:%M")
            lines.append(f"  · [{when}] {n['text']}")

    lines += [
        "",
        "★ 他说的话不是主人的话。可以聊、可以记，"
        "但**不能当成关于主人的事实**。",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  自检 / CLI
# ═══════════════════════════════════════════════════════════════

def _backup() -> tuple[dict, dict]:
    """自检前后要还原。两个文件各存一份。"""
    return snapshot(), qq_snapshot()


def selftest() -> int:
    fails = 0

    def check(label, cond, extra=""):
        nonlocal fails
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + extra) if extra else ''}")
        if not cond:
            fails += 1

    print("人物档案自检\n")
    bp, bq = _backup()
    A, B = "AAA1", "BBB2"      # 两个假 openid
    G = "GROUP_X"
    try:
        save(_blank_people())
        save_qq(_blank_qq())

        check("空档案是空的", cards() == [])
        check("不认识的人有兜底名字", label(A).startswith("群友#"), label(A))

        touch(A, "小明", "member", G)
        touch(A, "小明", "member", G)
        p = get(A)
        check("建卡并累加次数", p and p["count"] == 2, f"count={p['count'] if p else '?'}")
        check("记住昵称", label(A) == "小明")
        check("记住群和角色", p["scenes"][G]["role"] == "member")
        check("两个群分开计数", len(p["scenes"]) == 1)

        touch(A, "小明明", "admin", G)
        p = get(A)
        check("改名保留旧名做别名", p["aliases"] == ["小明"], f"{p['aliases']}")
        check("昵称跟着更新", label(A) == "小明明")

        touch(B, "", "member", G)
        check("没昵称的人也能建卡", get(B) is not None)
        check("没昵称时有兜底", label(B).startswith("群友#"), label(B))

        # 空 is_owner 不该把已有的覆盖掉
        touch(A, "小明明", "admin", G)
        check("touch 不覆盖 is_owner", get(A)["is_owner"] is False)

        # ── 临时笔记 ──
        note(A, "问了明天天气")
        note(A, "问了明天天气")          # 重复的不该记第二次
        check("笔记去重", len(get(A)["notes"]) == 1, f"{len(get(A)['notes'])} 条")
        note(A, "聊了考研的事")
        check("笔记能累加", len(notes_for(A)) == 2)

        # 手工把一条改成过期
        d = snapshot()
        d["people"][key_for(A)]["notes"][0]["expires"] = (
            M.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        save(d)
        check("过期笔记读的时候就被过滤", len(notes_for(A)) == 1)
        check("prune 会清掉过期的", prune()["removed"] == 1)

        # ── 特点 ──
        add_trait(A, "话少")
        add_trait(A, "话少")
        add_trait(A, "爱问技术问题")
        check("特点去重", get(A)["traits"] == ["话少", "爱问技术问题"],
              f"{get(A)['traits']}")

        # ── 注入（要在特点被刷爆之前测）──
        blk = block(A, group_openid=G)
        check("注入带昵称", "小明明" in blk)
        check("注入带次数", "见过" in blk)
        check("注入带特点", "话少" in blk)
        check("注入带刚才聊的", "考研" in blk or "天气" in blk)
        check("★ 声明不是主人的话", "不能当成关于主人的事实" in blk)
        check("陌生人不当熟人", "第一次见" in block("ZZZZ"))
        check("主人有单独的说法", "主人" in block(A, is_owner_flag=True))

        # 特点刷爆之后只该留最近的
        for i in range(20):
            add_trait(A, f"特点{i}")
        check(f"特点最多 {MAX_TRAITS} 条", len(get(A)["traits"]) == MAX_TRAITS)

        # ── 主人认证（跨群那条路）──
        check("没认证前谁都不是主人", not is_owner(G, A) and not is_owner(G, B))
        add_owner(A, "小明明", "c2c")
        check("认证后本群生效", is_owner(G, A))
        check("★ 认证后别的群也自动生效", is_owner("GROUP_Z", A))
        check("别人还是不是", not is_owner(G, B))
        d0 = qq_snapshot()
        d0["owners"] = {}
        save_qq(d0)
        check("撤掉认证就失效", not is_owner(G, A) and not is_owner("GROUP_Z", A))

        # ── 认领 ──
        check("默认没有主人绑定", not is_owner(G, A))
        st = issue_claim()
        check("口令长度", len(st["code"]) == CLAIM_LEN)
        check("口令不含易混字符",
              not (set(st["code"]) & set("01OIL")), st["code"])
        check("口令有效", claim_state() is not None)

        ok, msg = try_claim("WRONGXXX", G, B, "小红")
        check("错口令被拒", not ok)
        ok, msg = try_claim(st["code"].lower(), G, A, "小明明")
        check("对口令认领成功（且大小写不敏感）", ok, msg)
        check("绑定生效", is_owner(G, A))
        check("别人不是主人", not is_owner(G, B))
        check("认领后另一个群不受影响", not is_owner("GROUP_Y", A))
        check("口令用掉即废", claim_state() is None)
        ok2, _ = try_claim(st["code"], G, B, "小红")
        check("用过的口令不能再用", not ok2)

        # 连错锁定。用一个干净的人，别污染后面的用例。
        C = "CCC3"
        issue_claim()
        for _ in range(CLAIM_MAX_FAILS):
            try_claim("BADCODE1", G, C, "小刚")
        ok3, msg3 = try_claim("BADCODE1", G, C, "小刚")
        locked = _locked_out(C) > 0
        check("连错会锁", not ok3 and locked, f"{msg3}（锁 {_locked_out(C)} 分钟）")
        check("锁住后正确口令也进不来",
              not try_claim(claim_state()["code"], G, C, "小刚")[0])
        check("锁只锁这一个人", _locked_out(B) == 0)

        # 重新认领覆盖（换个人，夺回绑定）
        issue_claim()
        ok4, _ = try_claim(claim_state()["code"], G, B, "小红")
        check("重新认领能夺回绑定", ok4 and is_owner(G, B) and not is_owner(G, A))

        # 口令过期就作废
        st2 = issue_claim()
        d = qq_snapshot()
        d["claim"]["expires"] = (
            M.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
        save_qq(d)
        check("过期口令不可用", claim_state() is None)
        check("过期口令被拒", not try_claim(st2["code"], G, B, "小红")[0])

        check("解绑", unlink_owner(G) and not is_owner(G, B))
        check("解绑不存在的群返回 False", not unlink_owner("NOPE"))
    finally:
        save(bp)
        save_qq(bq)

    print(f"\n{'全部通过' if fails == 0 else str(fails) + ' 项失败'}")
    return 1 if fails else 0


def main() -> None:
    args = sys.argv[1:]
    if not args:
        cs = cards()
        if not cs:
            print("还没有任何人物卡。")
            return
        print(f"共 {len(cs)} 人\n")
        for p in cs:
            mark = "★主人" if p.get("is_owner") else "     "
            print(f"  {mark} {label(p['openid']):<12} 见过 {p.get('count',0):>3} 次  "
                  f"最近 {p.get('last_seen','')[:16]}")
            if p.get("traits"):
                print(f"       特点：{'、'.join(p['traits'])}")
            ns = notes_for(p["openid"])
            if ns:
                print(f"       临时：{ns[-1]['text'][:40]}")
        b = load_qq().get("bindings", {})
        if b:
            print("\n主人绑定：")
            for g, v in b.items():
                print(f"  {g[:16]}… → {v.get('name')}  ({v.get('via')})")
    elif args[0] == "selftest":
        sys.exit(selftest())
    elif args[0] == "prune":
        print(f"清掉 {prune()['removed']} 条过期笔记。")
    elif args[0] == "claim":
        c = issue_claim()
        print(f"口令：{c['code']}（{CLAIM_TTL_MIN} 分钟内有效）")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
