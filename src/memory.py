#!/usr/bin/env python3
"""
memory.py —— AIPet 的记忆引擎

设计要点（相比"把对话历史全塞进 prompt"的朴素做法）：

  1. 事实与事件分离
     PROFILE.md = 当前状态快照（可覆盖、去重）
     journal.jsonl = 历史时间线（只追加）—— 两者生命周期不同，混一起必然膨胀

  2. 每条记忆带重要性 / 情绪 / 衰减类别
     不是所有记忆都该等权。生日和"今天吃了什么"不该一个待遇。

  3. 检索评分而非全量注入
     score = 关键词匹配 × 重要度 × 时间衰减 × 标签加成
     无需向量库，纯 stdlib，零依赖

  4. Token 预算硬约束
     记忆注入有上限，按分数截断。这是防 prompt 膨胀的关键闸门。

  5. 分层压缩
     日 → 周 → 月。老记忆降采样，原始条目进 archive/ 而非删除。

  6. 隐私过滤
     命中敏感词模式的整句拒绝入库。

用法：
    python src/memory.py add "用户说下周三要交方案" -i 4 -t 工作,承诺 -e 压力
    python src/memory.py search "方案 进度"
    python src/memory.py context "方案 进度"     # 打印组装好的 prompt 片段
    python src/memory.py compress
    python src/memory.py stats
    python src/memory.py demo                    # 灌示例数据看效果
"""

from __future__ import annotations

import heapq
import json
import math
import shutil
import uuid
import contextvars
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Windows 控制台中文输出
if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
CJK = r"一-鿿"
ACTIVE_MESSAGE = contextvars.ContextVar("aipet_message", default=None)
_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RE = re.compile(rf"[{CJK}]+")
_JOURNAL_CACHE: dict[str, object] = {"sig": None, "entries": None}
_TOKEN_CACHE: dict[str, set[str]] = {}
_TS_CACHE: dict[str, datetime] = {}


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    """
    读 data/config.json。

    ★ 文件不存在就先拿 data/config.example.json 播种一份出来。
      以前这里是直接 open()，而仓库里既没有 config.json、也没有任何地方
      会创建它 —— 结果是 clone 下来第一件事就是 FileNotFoundError，
      README 里写的「双击就能跑」根本不成立。模板本身就带 _说明，
      播种出来的那份是能直接看懂的。
    """
    path = ROOT / "data" / "config.json"
    if not path.exists():
        example = ROOT / "data" / "config.example.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if example.exists():
            shutil.copyfile(example, path)
        else:                      # 连模板都没有：给一份最小可跑的
            path.write_text(json.dumps({
                "paths": {
                    "journal": "memory/journal.jsonl", "state": "memory/state.json",
                    "profile": "persona/PROFILE.md", "summaries": "memory/summaries",
                    "archive": "memory/archive", "view": "view/memory.html",
                },
                "tools": {"fs_root": "", "search_backend": "ddgs", "proxy": "auto"},
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()
P = CFG["paths"]


def reload_config() -> dict:
    """Reload the editable application config after an in-app settings save."""
    global CFG, P
    CFG = load_config()
    P = CFG["paths"]
    return CFG


def _p(key: str) -> Path:
    return ROOT / P[key]


# ---------------------------------------------------------------- 工具

def now() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_ts(ts: str) -> datetime:
    cached = _TS_CACHE.get(ts)
    if cached is None:
        cached = datetime.fromisoformat(ts)
        _TS_CACHE[ts] = cached
    return cached


def tokenize(text: str) -> set[str]:
    """中文按字 + 二元组，英文按词。够用，且不用装分词库。"""
    text = text.lower()
    toks: set[str] = set(_TOKEN_RE.findall(text))
    for m in _CJK_RE.finditer(text):
        s = m.group()
        toks.update(s)                                   # 单字
        toks.update(s[i:i + 2] for i in range(len(s) - 1))  # 相邻二字
    return toks


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中文约 1 token/字，其余约 3.5 字符/token。"""
    cjk = len(re.findall(rf"[{CJK}]", text))
    return int(cjk + (len(text) - cjk) / 3.5) + 1


_OWNER_WORDS = ("owner", "self", "me", "主人")


def speaker_kind(entry: dict) -> str:
    """
    说话人分三档：owner / person / guest。

    背景：bot 可能同时在好几个 QQ 群里。群里谁说一句"我生日是 5 月 1 号"，
    如果被记成"用户的生日是 5 月 1 号"，那不是权重问题，**是错的** ——
    它会跟主人的真生日打架。

      owner  ——主人说的。桌面宠物的所有对话都算
      person ——认得出来的群友，speaker 存成 "qq:<member_openid>"
      guest  ——认不出来的，或者旧数据

    ★ 必须写成 `entry.get("speaker") or "owner"`，不能写 `entry["speaker"]`。
      实测 journal.jsonl 里有两种历史形态：13 条是 "owner"，
      2 条**完全没有这个字段**（最早那两条）。而且显式的 null 也要能兜住，
      所以是 `or` 不是 `get(k, default)`。
    """
    sp = str(entry.get("speaker") or "owner")
    if sp in _OWNER_WORDS:
        return "owner"
    if sp.startswith("qq:") or sp.startswith("group:"):
        return "person"
    return "guest"


def speaker_weight(entry: dict) -> float:
    """
    说话人权重。

    外人的记忆不是不能记（聊天上下文有用），但：
      · 检索权重打折
      · 重要度封顶
      · 衰减极快（guest 一周左右淡出，认得的群友慢一些）
      · 注入时加前缀，防止模型张冠李戴
    """
    cfg = CFG.get("speaker", {})
    kind = speaker_kind(entry)
    if kind == "owner":
        return cfg.get("owner_weight", 1.0)
    if kind == "person":
        return cfg.get("known_guest_weight", 0.6)
    return cfg.get("guest_weight", 0.45)


def is_sensitive(text: str) -> str | None:
    """命中隐私红线则返回命中的词。"""
    low = text.lower()
    for pat in CFG["privacy"]["blocked_patterns"]:
        if pat.lower() in low:
            return pat
    return None


# ---------------------------------------------------------------- 存储

def load_journal() -> list[dict]:
    f = _p("journal")
    if not f.exists():
        return []
    control = ROOT / "data" / "companion.sqlite3"
    sig_parts = []
    for candidate in (f, control):
        try:
            st = candidate.stat()
            sig_parts.append((str(candidate), st.st_mtime_ns, st.st_size))
        except OSError:
            sig_parts.append((str(candidate), None, None))
    sig = tuple(sig_parts)
    if sig == _JOURNAL_CACHE["sig"] and _JOURNAL_CACHE["entries"] is not None:
        return _JOURNAL_CACHE["entries"]  # type: ignore[return-value]
    out = []
    with open(f, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if control.exists():
        import sqlite3
        import hashlib
        # ★ 必须显式 close()。`with sqlite3.connect(...) as db:` 管的是**事务**
        #   （提交/回滚），不是连接 —— 出了 with 块句柄还开着。
        #   后果：每调一次 load_journal() 漏一个句柄，一直占着这个文件，
        #   Windows 上删不掉、改名失败，测试的临时目录清理会报 WinError 32。
        db = sqlite3.connect(control, timeout=15)
        try:
            blocked = set(db.execute("SELECT id,digest FROM excluded_memory"))
        except sqlite3.OperationalError:
            blocked = set()
        finally:
            db.close()
        out = [e for e in out if (e['id'], hashlib.sha256(e['text'].encode()).hexdigest()) not in blocked]
    _JOURNAL_CACHE["sig"] = sig
    _JOURNAL_CACHE["entries"] = out
    return out


def append_journal(entry: dict) -> None:
    f = _p("journal")
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _JOURNAL_CACHE["sig"] = None
    _JOURNAL_CACHE["entries"] = None


def save_journal(entries: list[dict]) -> None:
    f = _p("journal")
    with open(f, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    _JOURNAL_CACHE["sig"] = None
    _JOURNAL_CACHE["entries"] = None


def load_state() -> dict:
    f = _p("state")
    default = {
        "closeness": 0,
        "mood": "平静",
        "last_interaction": None,
        "interaction_count": 0,
        "streak_days": 0,
        "pending_promises": [],
        "updated": now_iso(),
    }
    if not f.exists():
        return default
    try:
        with open(f, encoding="utf-8") as fh:
            default.update(json.load(fh))
    except (json.JSONDecodeError, OSError):
        pass
    return default


def save_state(state: dict) -> None:
    state["updated"] = now_iso()
    f = _p("state")
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 写入

def compute_progress(entry: dict, ref: datetime | None = None) -> str:
    """
    把「会随时间变化的事」算成当前状态。

    为什么需要它：直接记「用户大二」的话，过了暑假这条就过期了，
    而且没人会记得去改。改成记**起点 + 规则**，每次注入时现算：

        "since": "2025-09-01"      入学时间
        "labels": ["大一","大二","大三","大四"]
        "span_months": 12          每 12 个月升一级

    → 2026-09 算出「大二」，2027-09 自动变成「大三」，不用任何人维护。

    适用于任何有周期规律的事：年级、工龄、项目阶段、在一起多久。
    """
    pr = entry.get("progress")
    if not pr:
        return ""

    try:
        since = parse_ts(pr["since"])
    except (KeyError, ValueError, TypeError):
        return ""

    ref = ref or now()
    labels = pr.get("labels") or []
    span = max(1, int(pr.get("span_months", 12)))

    months = (ref.year - since.year) * 12 + (ref.month - since.month)
    months = max(0, months)
    idx = months // span

    if labels:
        if idx >= len(labels):
            label = labels[-1]
            done = True
        else:
            label = labels[idx]
            done = False
    else:
        label, done = f"第 {idx + 1} 期", False

    yrs, mos = divmod(months, 12)
    if yrs and mos:
        elapsed = f"{yrs} 年 {mos} 个月"
    elif yrs:
        elapsed = f"{yrs} 年"
    else:
        elapsed = f"{mos} 个月"

    unit = pr.get("unit", "")
    if done:
        return f"{label}（已满 {unit}，距起点 {elapsed}）"
    return f"{label}（{since:%Y年%m月}起，已经 {elapsed}）"


def add(text: str, importance: int = 3, tags: list[str] | None = None,
        emotion: str = "", decay: str = "normal", source: str = "chat",
        progress: dict | None = None, speaker: str = "owner",
        speaker_name: str = "", speaker_role: str = "") -> dict | None:
    """
    写入一条记忆。返回写入的条目，被隐私过滤则返回 None。

    speaker:
        "owner"        主人（默认）
        "guest"        认不出来的群友
        "qq:<openid>"  认得出来的群友，见 people.py

    speaker_name / speaker_role 是可选的补充信息（昵称、群内角色），
    只在 speaker 是具体的人时才有意义。它们有默认值，现有调用方一行不用改。
    """
    if ACTIVE_MESSAGE.get() == "__ephemeral__":
        return None
    text = text.strip()
    if not text:
        return None

    hit = is_sensitive(text)
    if hit:
        print(f"[跳过] 命中隐私红线「{hit}」，不记录")
        return None

    if decay not in ("permanent", "slow", "normal"):
        decay = "normal"

    kind = speaker_kind({"speaker": speaker})
    scfg = CFG.get("speaker", {})

    if kind == "guest":
        # 陌生人三重限制，不管调用方传了什么
        importance = min(int(importance), scfg.get("guest_max_importance", 2))
        # permanent 对外人无效 —— 群友的生日不该被永久记住
        if decay == "permanent":
            decay = "normal"
    elif kind == "person":
        # 认得的群友松一档：能记慢衰减，但仍然不永久。
        # 理由没变 —— 这些不是关于主人的事实，不该跟他自己的争位置。
        importance = min(int(importance), scfg.get("known_guest_max_importance", 3))
        if decay == "permanent":
            decay = "slow"

    entries = load_journal()
    entry = {
        "id": f"{now():%Y%m%d}-{uuid.uuid4().hex[:12]}",
        "ts": now_iso(),
        "text": text,
        "importance": max(1, min(5, int(importance))),
        "tags": tags or [],
        "emotion": emotion,
        "decay": decay,
        "source": source,
        # 归一化：owner 的几种写法都收成 "owner"；person 原样存 openid（身份不能丢）。
        "speaker": {"owner": "owner", "person": str(speaker)}.get(kind, "guest"),
    }
    if ACTIVE_MESSAGE.get():
        entry["message_id"] = ACTIVE_MESSAGE.get()
    if progress:
        entry["progress"] = progress
    if speaker_name:
        entry["speaker_name"] = speaker_name
    if speaker_role:
        entry["speaker_role"] = speaker_role
    append_journal(entry)

    # 同步更新关系状态
    st = load_state()
    st["interaction_count"] = st.get("interaction_count", 0) + 1
    st["last_interaction"] = entry["ts"]
    if emotion:
        st["mood"] = emotion
    save_state(st)

    return entry


# ---------------------------------------------------------------- 检索（核心）

def _keyword_score(entry_toks: set[str], query_toks: set[str]) -> float:
    if not query_toks or not entry_toks:
        return 0.0
    overlap = entry_toks & query_toks
    if not overlap:
        return 0.0
    cov_q = len(overlap) / len(query_toks)      # 覆盖了多少查询
    cov_e = len(overlap) / len(entry_toks)      # 命中的精确度
    return 0.7 * cov_q + 0.3 * cov_e


def _entry_tokens(entry: dict) -> set[str]:
    """复用记忆条目的分词结果，避免高量检索时反复扫描正文。"""
    text = entry.get("text", "")
    key = f"{entry.get('id', '')}\0{text}"
    toks = _TOKEN_CACHE.get(key)
    if toks is None:
        toks = tokenize(text)
        if len(_TOKEN_CACHE) >= 4096:
            _TOKEN_CACHE.clear()
        _TOKEN_CACHE[key] = toks
    return toks


def _recency_factor(entry: dict, ref: datetime) -> float:
    # 外人的记忆走独立衰减曲线 —— 群里的闲聊一周左右就该淡出，
    # 不该跟主人的记忆用同一条时间线。
    # 认得的群友慢一档（半衰期约 11.5 天）：他反复出现，值得记住久一点。
    scfg = CFG.get("speaker", {})
    kind = speaker_kind(entry)
    if kind == "guest":
        lam = scfg.get("guest_decay", 0.15)
    elif kind == "person":
        lam = scfg.get("known_guest_decay", 0.06)
    else:
        lam = CFG["scoring"]["decay_lambda"].get(entry.get("decay", "normal"), 0.02)
    if lam <= 0:
        return 1.0
    age_days = max(0.0, (ref - parse_ts(entry["ts"])).total_seconds() / 86400)
    return math.exp(-lam * age_days)


def score_entry(entry: dict, query_toks: set[str], ref: datetime,
                query_tags: set[str] | None = None,
                entry_toks: set[str] | None = None) -> float:
    kw = _keyword_score(entry_toks or _entry_tokens(entry), query_toks)
    if kw <= 0:
        return 0.0

    floor = CFG["scoring"]["importance_floor"]
    imp = floor + (entry.get("importance", 3) - 1) / 4 * (1.0 - floor)

    s = kw * imp * _recency_factor(entry, ref) * speaker_weight(entry)

    if query_tags and set(entry.get("tags") or []) & query_tags:
        s *= 1 + CFG["scoring"]["tag_bonus"]

    return s


def retrieve(query: str, query_tags: list[str] | None = None,
             budget_tokens: int | None = None, top_k: int | None = None,
             retrieval: dict | None = None) -> list[dict]:
    """按分数检索记忆，受 token 预算和条数上限约束。"""
    # 本轮参数由调用者显式传入。不要把一次请求的档位写回全局 CFG，
    # 否则桌面、QQ 和后台任务并发时会互相污染检索边界。
    r = retrieval or CFG["retrieval"]
    budget = budget_tokens if budget_tokens is not None else r["token_budget"]
    k = top_k if top_k is not None else r["max_entries"]

    entries = load_journal()
    if not entries:
        return []

    ref = now()
    qtoks = tokenize(query)
    qtags = set(query_tags or [])

    # 保留固定大小候选，避免每次对完整记忆库做 O(n log n) 排序。
    scored = []
    for e in entries:
        s = score_entry(e, qtoks, ref, qtags)
        if s >= r["min_score"]:
            scored.append((s, e))
    candidate_k = min(len(scored), max(k * 4, 64))
    scored = heapq.nlargest(candidate_k, scored, key=lambda x: x[0])

    picked: list[dict] = []
    used = 0
    seen_ids = set()

    for s, e in scored:
        if len(picked) >= k:
            break
        cost = estimate_tokens(e["text"])
        if used + cost > budget and picked:
            break
        picked.append({**e, "_score": round(s, 4)})
        seen_ids.add(e["id"])
        used += cost

    # 带 progress 规则的条目（年级、工龄这类）**永远注入**。
    # 它们描述的是"用户现在处于什么阶段"，每次对话都用得上，
    # 不该因为关键词没匹配上就被漏掉。
    for e in entries:
        if e.get("progress") and e["id"] not in seen_ids:
            picked.append({**e, "_score": 0.0, "_progress": True})
            seen_ids.add(e["id"])

    # 近期兜底：保证短期连贯性，不受分数影响
    floor = r["recency_floor"]
    if floor > 0:
        for e in sorted(entries, key=lambda x: x["ts"], reverse=True)[:floor]:
            if e["id"] not in seen_ids:
                picked.append({**e, "_score": 0.0, "_floor": True})
                seen_ids.add(e["id"])

    return picked


# ---------------------------------------------------------------- prompt 组装

def persona_text(include: tuple[str, ...] = ("SOUL.md", "BOUNDARIES.md")) -> str:
    """
    读人格文件，**剥掉给用户看的编辑说明**。

    这是共用入口 —— 桌宠（brain.py）和 MCP（mcp_server.py）都走它。
    之前剥离逻辑只写在 brain.py 里，结果 MCP 那条路把
    "这是你最该动手改的文件"原样喂给了 QQ 上的小日和，
    害她以为自己是份待编辑的文档。

    同一个文件，你看到的是说明书，她看到的是自己。

    注意 HTML 注释里不能再出现注释结束标记，否则会提前闭合 ——
    SOUL.md 的说明里已经写了这条。
    """
    parts = []
    for f in include:
        p = ROOT / "persona" / f
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        if t:
            parts.append(t)
    return "\n\n---\n\n".join(parts)


def _profile_facts() -> str:
    """只抽取 PROFILE.md 的事实分区，丢掉说明性文字——那些对模型没用，白烧 token。"""
    prof = _p("profile")
    if not prof.exists():
        return "（暂无）"
    text = prof.read_text(encoding="utf-8")

    start = text.find("## 身份")
    if start == -1:
        return "（暂无）"
    end = text.find("## 维护说明")
    if end == -1:
        end = len(text)

    body = text[start:end].rstrip()
    # 去掉尾部的 --- 分隔线和空行
    body = re.sub(r"[\s\-]*$", "", body)
    # 去掉 HTML 注释
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    # 去掉还没填内容的占位条目
    lines = [ln for ln in body.splitlines()
             if not re.match(r"^-\s*（.*）\s*$", ln.strip())]
    body = "\n".join(lines).strip()

    # 丢掉没有任何内容的分区标题
    kept = []
    for block in re.split(r"\n(?=## )", body):
        ls = block.splitlines()
        if len(ls) > 1 and any(x.strip() for x in ls[1:]):
            kept.append(block.rstrip())

    return "\n\n".join(kept).strip() or "（暂无）"


def build_context(query: str, query_tags: list[str] | None = None,
                  retrieval: dict | None = None) -> str:
    """组装要注入 prompt 的记忆片段。"""
    st = load_state()
    mem = retrieve(query, query_tags, retrieval=retrieval)

    parts = ["## 当前关系状态"]
    parts.append(
        f"- 亲密度 {st['closeness']} · 情绪 {st['mood']} · "
        f"累计互动 {st['interaction_count']} 次"
    )
    if st.get("pending_promises"):
        parts.append("- 未兑现的承诺：")
        parts.extend(f"  - {p}" for p in st["pending_promises"])
    if st.get("last_interaction"):
        last = parse_ts(st["last_interaction"])
        gap = (now() - last).total_seconds() / 3600
        parts.append(f"- 距上次互动：{gap:.1f} 小时")

    # ★ 心理点放在回忆之前。它是"此刻用什么语气"，
    #   比"记得什么"更靠前——同样的记忆，换个状态说出来是不一样的。
    try:
        import mood as _mood
        for seg in (_mood.block(), _mood.suggest_block(query)):
            if seg:
                parts.append("")
                parts.append(seg)
    except ImportError:
        pass

    parts.append("\n## 相关回忆")
    if mem:
        mark_guest = CFG.get("speaker", {}).get("mark_in_context", True)
        for e in mem:
            when = parse_ts(e["ts"]).strftime("%m月%d日")
            mark = "★" * e.get("importance", 3)
            tag = f" #{' #'.join(e['tags'])}" if e.get("tags") else ""
            # 有 progress 规则的条目，把当前状态现算出来附在后面
            prog = compute_progress(e)
            extra = f"　→ 当前：{prog}" if prog else ""
            # ★ 外人说的必须标出来，否则模型会当成关于主人的事实。
            # 这不是权重问题 —— 群里有人报自己的生日，被记成"用户的生日"
            # 就是彻头彻尾的错。
            who = ""
            # 认得出来的人就点名，认不出的说"有人"。
            if mark_guest:
                k = speaker_kind(e)
                if k == "person":
                    nm = e.get("speaker_name") or "某个群友"
                    who = f"［群里「{nm}」说的，不是主人］ "
                elif k == "guest":
                    who = "［群里有人说的，不是主人］ "
            parts.append(f"- [{when}] {who}{e['text']}{extra} {mark}{tag}")
    else:
        parts.append("- （没有相关记忆）")

    parts.append("\n## 关于用户的事实")
    parts.append(_profile_facts())

    return "\n".join(parts)


# ---------------------------------------------------------------- 压缩 / 遗忘

def compress() -> dict:
    """分层压缩：老条目降采样成摘要，原始条目移入 archive/。"""
    c = CFG["compression"]
    entries = load_journal()
    if not entries:
        return {"weekly": 0, "monthly": 0, "archived": 0}

    ref = now()
    d2w = c["daily_to_weekly_after_days"]
    w2m = c["weekly_to_monthly_after_days"]

    keep, to_compress = [], []
    for e in entries:
        age = (ref - parse_ts(e["ts"])).days
        if e.get("decay") == "permanent" or age < d2w:
            keep.append(e)
        else:
            to_compress.append(e)

    if not to_compress:
        return {"weekly": 0, "monthly": 0, "archived": 0}

    # 按 ISO 周分组
    weeks: dict[str, list[dict]] = {}
    for e in to_compress:
        iso = parse_ts(e["ts"]).isocalendar()
        weeks.setdefault(f"{iso[0]}-W{iso[1]:02d}", []).append(e)

    sm_dir = _p("summaries")
    sm_dir.mkdir(parents=True, exist_ok=True)
    weekly_made = monthly_made = 0

    for wk, items in sorted(weeks.items()):
        # 抽取式摘要：重要度降序 + 时间升序，取头几条
        items.sort(key=lambda x: (-x.get("importance", 3), x["ts"]))
        picked = items[:5]
        lines = [f"# {wk} 周摘要",
                 f"（{len(items)} 条记忆压缩自 {picked[0]['ts'][:10]} 起）\n"]
        for e in picked:
            lines.append(f"- [{e['ts'][:10]}] {e['text']} "
                         f"{'★' * e.get('importance', 3)}")
        (sm_dir / f"{wk}.md").write_text("\n".join(lines), encoding="utf-8")

        # 该周是否已老到该升月摘要
        if (ref - parse_ts(items[0]["ts"])).days >= w2m:
            monthly_made += 1
        weekly_made += 1

    if c["archive_originals"]:
        arc = _p("archive")
        arc.mkdir(parents=True, exist_ok=True)
        f = arc / f"journal-{ref:%Y%m%d-%H%M%S}.jsonl"
        with open(f, "w", encoding="utf-8") as fh:
            for e in to_compress:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    save_journal(keep)
    return {"weekly": weekly_made, "monthly": monthly_made,
            "archived": len(to_compress), "remaining": len(keep)}


PROFILE_TEMPLATE = """\
# PROFILE —— 关于你的事实

> **这个文件由程序自动维护，但你也可以随时手改。**
> 手改的内容不会被覆盖——程序只做"合并"，不做"重写"。
>
> 它和 `memory/journal.jsonl` 的区别（这是整个记忆系统的核心设计）：
>
> | | PROFILE.md | journal.jsonl |
> |---|---|---|
> | 内容 | **当前状态快照**（事实） | **历史时间线**（事件） |
> | 写入 | 覆盖 / 合并去重 | 只追加，永不修改 |
> | 例子 | "用户的专业是计算机" | "9月14日，用户说周三要交方案" |
> | 膨胀 | 有上限，会去重 | 会膨胀，靠压缩控制 |
>
> 混在一起的后果：同一个事实被记 20 遍，prompt 越来越长，模型越来越糊涂。

---

## 身份

<!-- 程序会在这里追加条目。格式：- 事实内容  `来源:日期` -->

## 作息与节律

## 在做的事

## 喜好

## 忌讳

## 重要日期

<!-- 这个区域的条目 decay 类别为 permanent，永不被压缩或遗忘 -->

## 长期目标

---

## 维护说明

程序每隔一段时间会：
1. 从 journal 里抽取新事实，**与上面已有条目比对**
2. 重复的 → 更新（改日期、补充细节），不新增
3. 全新的 → 追加到对应分区
4. "重要日期"区的条目 → 标记为 permanent，永不衰减

所以这个文件**不会无限膨胀**。如果你手动加了内容，程序也会一并纳入去重逻辑。
"""


def reset(clear_archive: bool = True) -> dict:
    """
    清空记忆库，但保留人格（persona/ 下的 SOUL 和 BOUNDARIES 不动）。

    用途：灌过演示数据之后要开始正式使用；或者你想让它忘掉过去、重新认识你。
    """
    n_entries = len(load_journal())
    removed = []

    save_journal([])
    removed.append(f"journal.jsonl（{n_entries} 条）")

    _p("profile").write_text(PROFILE_TEMPLATE, encoding="utf-8")
    removed.append("PROFILE.md（已重置为空白模板）")

    save_state({
        "closeness": 0, "mood": "平静", "last_interaction": None,
        "interaction_count": 0, "streak_days": 0,
        "pending_promises": [], "last_consolidated": None,
    })
    removed.append("state.json（关系状态归零）")

    if clear_archive:
        for sub in ("summaries", "archive"):
            d = _p(sub)
            if d.exists():
                files = list(d.glob("*"))
                for f in files:
                    if f.is_file():
                        f.unlink()
                if files:
                    removed.append(f"{sub}/（{len(files)} 个文件）")

    return {"removed": removed, "entries_cleared": n_entries}


def stats() -> dict:
    entries = load_journal()
    st = load_state()
    by_decay: dict[str, int] = {}
    by_imp: dict[int, int] = {}
    for e in entries:
        by_decay[e.get("decay", "normal")] = by_decay.get(e.get("decay", "normal"), 0) + 1
        by_imp[e.get("importance", 3)] = by_imp.get(e.get("importance", 3), 0) + 1
    tokens = sum(estimate_tokens(e["text"]) for e in entries)
    return {
        "条目数": len(entries),
        "总 token": tokens,
        "按衰减类别": by_decay,
        "按重要度": dict(sorted(by_imp.items())),
        "摘要文件": len(list(_p("summaries").glob("*.md"))) if _p("summaries").exists() else 0,
        "归档文件": len(list(_p("archive").glob("*.jsonl"))) if _p("archive").exists() else 0,
        "关系状态": st,
    }


# ---------------------------------------------------------------- 演示数据

DEMO = [
    ("用户说下周三要交一份课程设计报告，还完全没开始写", 5, ["工作", "承诺"], "压力", "normal", 0),
    ("用户偏好用 C++ 写算法题，不太喜欢 Python 的性能", 3, ["技术", "偏好"], "", "slow", 2),
    ("用户提到最近睡眠很差，经常凌晨两点才睡", 4, ["作息", "状态"], "疲惫", "slow", 3),
    ("用户晚上吃了麻辣烫，说那家店换了老板味道变差了", 1, ["日常"], "", "normal", 5),
    ("用户说这个学期想把绩点提到 3.5 以上", 5, ["目标"], "", "slow", 7),
    ("用户的生日是 3 月 15 日", 5, ["重要日期"], "", "permanent", 10),
    ("用户抱怨导师每次开会都不说重点，一开就是两小时", 3, ["工作", "情绪"], "烦躁", "normal", 12),
    ("用户说想养一只猫但是宿舍不让", 2, ["愿望"], "", "slow", 15),
    ("用户提到自己 osu! 打到了 4 段，挺得意的", 2, ["爱好"], "开心", "normal", 20),
    ("用户说讨厌别人在他专注的时候打扰他", 4, ["忌讳", "偏好"], "", "slow", 25),
    ("用户在准备考研，目标院校还没定", 5, ["目标", "工作"], "迷茫", "slow", 40),
    ("用户提到高中的时候学过一点吉他，后来荒废了", 2, ["爱好", "过去"], "", "slow", 55),
]


def seed_demo() -> None:
    print("灌入演示数据…")
    ref = now()
    n = 0
    for text, imp, tags, emo, decay, days_ago in DEMO:
        if is_sensitive(text):
            continue
        entries = load_journal()
        ts = (ref - timedelta(days=days_ago)).isoformat(timespec="seconds")
        append_journal({
            "id": f"{parse_ts(ts):%Y%m%d}-{len(entries) + 1:04d}",
            "ts": ts, "text": text, "importance": imp, "tags": tags,
            "emotion": emo, "decay": decay, "source": "demo",
        })
        n += 1
    st = load_state()
    st.update({"closeness": 42, "mood": "平静", "interaction_count": n,
               "pending_promises": ["下周三交课程设计报告"]})
    save_state(st)
    print(f"完成，写入 {n} 条。")


# ---------------------------------------------------------------- CLI

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]

    if cmd == "add":
        text = rest[0] if rest else ""
        imp, tags, emo, decay = 3, [], "", "normal"
        prog: dict = {}
        i = 1
        while i < len(rest):
            a = rest[i]
            if a in ("-i", "--importance") and i + 1 < len(rest):
                imp = int(rest[i + 1]); i += 2
            elif a in ("-t", "--tags") and i + 1 < len(rest):
                tags = [t.strip() for t in rest[i + 1].split(",") if t.strip()]; i += 2
            elif a in ("-e", "--emotion") and i + 1 < len(rest):
                emo = rest[i + 1]; i += 2
            elif a in ("-d", "--decay") and i + 1 < len(rest):
                decay = rest[i + 1]; i += 2
            # ── 会随时间变化的事实 ──
            elif a == "--since" and i + 1 < len(rest):
                prog["since"] = rest[i + 1]; i += 2
            elif a == "--labels" and i + 1 < len(rest):
                prog["labels"] = [x.strip() for x in rest[i + 1].split(",") if x.strip()]
                i += 2
            elif a == "--span" and i + 1 < len(rest):
                prog["span_months"] = int(rest[i + 1]); i += 2
            elif a == "--unit" and i + 1 < len(rest):
                prog["unit"] = rest[i + 1]; i += 2
            else:
                i += 1
        e = add(text, imp, tags, emo, decay, progress=prog or None)
        if e:
            print(f"已记录 {e['id']}：{e['text']}")
            if prog:
                print(f"  随时间更新 → 当前：{compute_progress(e)}")

    elif cmd == "search":
        q = rest[0] if rest else ""
        hits = retrieve(q)
        if not hits:
            print("没有命中。")
        for e in hits:
            flag = "(近期兜底)" if e.get("_floor") else f"score={e['_score']}"
            print(f"  [{e['ts'][:10]}] {e['text']}  {flag}")

    elif cmd == "context":
        q = rest[0] if rest else ""
        out = build_context(q)
        print(out)
        print(f"\n--- 本次注入 {estimate_tokens(out)} tokens ---")

    elif cmd == "compress":
        print(json.dumps(compress(), ensure_ascii=False, indent=2))

    elif cmd == "show":
        # 人看的格式：把库里所有条目按时间倒序列出来，带说话人和衰减类别
        entries = sorted(load_journal(), key=lambda x: x["ts"], reverse=True)
        if not entries:
            print("记忆库是空的。")
            return
        st = load_state()
        print(f"共 {len(entries)} 条 · 亲密度 {st.get('closeness', 0)} · "
              f"累计互动 {st.get('interaction_count', 0)} 次")
        try:
            import mood as _mood
            cur = _mood.active()
            if cur:
                print(f"现在停在「{cur['key']}」"
                      f"（还剩 {cur['left_min']} 分钟，到点自己散）")
        except ImportError:
            pass
        print()
        for e in entries:
            sp = e.get("speaker", "owner")
            who = "  " if sp == "owner" else "群"
            prog = compute_progress(e)
            tail = f"  → {prog}" if prog else ""
            print(f"  {who} [{e['ts'][:16]}] ({e.get('decay','normal'):<9}) "
                  f"{'★' * e.get('importance', 3):<5} {e['text']}{tail}")
        print(f"\n  左列：空 = 主人说的 ·「群」= 群里其他人说的")

    elif cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))

    elif cmd == "reset":
        if "--yes" not in rest:
            print("这会清空全部记忆（人格文件不动）。")
            print("确认请加 --yes：python src/memory.py reset --yes")
            print("建议先备份：python src/backup.py create 清库前")
            return
        r = reset()
        print("已清空：")
        for x in r["removed"]:
            print(f"  · {x}")
        print("\n人格（SOUL / BOUNDARIES）未改动。它现在是张白纸。")

    elif cmd == "demo":
        seed_demo()

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
