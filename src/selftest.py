#!/usr/bin/env python3
"""
selftest.py —— 记忆系统自检

验证的是「记忆真的有用吗」，不是「代码能跑吗」。所以测的都是行为：

  1. 时间推进时，学业进度会不会自己更新
  2. 带 progress 的记忆会不会**永远**被注入（不靠关键词命中）
  3. 生日这类 permanent 记忆会不会被时间衰减掉
  4. 检索、压缩、备份这些机制还正常吗
  5. 记忆面板能不能反映出最新的库内容

用法：
    python src/selftest.py            # 全部跑
    python src/selftest.py --verbose  # 多打点细节
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M          # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PASS, FAIL = "  [OK]", "  [!!]"
_fails = 0
_verbose = "--verbose" in sys.argv


def check(label: str, cond: bool, detail: str = ""):
    global _fails
    print(f"{PASS if cond else FAIL} {label}" + (f"   {detail}" if detail else ""))
    if not cond:
        _fails += 1


def section(title: str):
    print(f"\n── {title} " + "─" * max(0, 52 - len(title)))


# ═══════════════════════════════════════════════════════════════
#  1. 时间推进 —— 学业进度自动更新
# ═══════════════════════════════════════════════════════════════

def test_progress():
    section("随时间更新（模拟时间推进）")

    entries = [e for e in M.load_journal() if e.get("progress")]
    if not entries:
        check("库里有带 progress 的记忆", False, "先写入一条带 --since 的记忆")
        return
    e = entries[0]

    # 入学 2025-09，四年制 → 各时间点该显示什么
    cases = [
        ("2025-10-01", "大一", "刚入学一个月"),
        ("2026-06-15", "大一", "大一学年末"),
        ("2026-09-14", "大二", "★ 今天"),
        ("2027-09-01", "大三", "升大三"),
        ("2028-09-01", "大四", "升大四"),
        ("2029-09-01", "大四", "超过四年，封顶在最后一档"),
    ]
    for ds, want, note in cases:
        ref = datetime.fromisoformat(ds).astimezone()
        got = M.compute_progress(e, ref)
        ok = want in got
        check(f"{ds} → {want}", ok, f"得到「{got}」  ({note})")
        if _verbose and ok:
            print(f"        {got}")


# ═══════════════════════════════════════════════════════════════
#  2. 永远注入 —— 不靠关键词命中
# ═══════════════════════════════════════════════════════════════

def test_always_injected():
    section("progress 记忆永远注入")

    # 用一个和学业、生日都毫无关系的问题
    query = "帮我看看这段 C++ 代码为什么死循环"
    hits = M.retrieve(query)
    ids = {e["id"] for e in hits}
    prog_ids = {e["id"] for e in M.load_journal() if e.get("progress")}

    missing = prog_ids - ids
    check("无关问题也会带上 progress 记忆", not missing,
          f"漏了 {len(missing)} 条" if missing else f"共注入 {len(hits)} 条")

    txt = M.build_context(query)
    check("上下文里有算好的当前状态", "大二" in txt,
          "关键词是 '大二'" if "大二" in txt else txt[-120:])


# ═══════════════════════════════════════════════════════════════
#  3. permanent —— 不被时间衰减
# ═══════════════════════════════════════════════════════════════

def test_permanent():
    section("permanent 记忆不被衰减")

    perms = [e for e in M.load_journal() if e.get("decay") == "permanent"]
    if not perms:
        check("库里有 permanent 记忆", False)
        return

    ref_now = M.now()
    ref_far = ref_now + timedelta(days=365 * 10)     # 十年后

    for e in perms:
        r1 = M._recency_factor(e, ref_now)
        r2 = M._recency_factor(e, ref_far)
        check(f"「{e['text'][:16]}…」十年后仍为满值", abs(r1 - 1.0) < 1e-9
              and abs(r2 - 1.0) < 1e-9, f"{r1:.3f} → {r2:.3f}")

    # 对照：普通记忆必须衰减
    normal = {"ts": M.now_iso(), "decay": "normal"}
    r_now = M._recency_factor(normal, ref_now)
    r_old = M._recency_factor(normal, ref_now + timedelta(days=180))
    check("普通记忆半年后确实衰减", r_old < r_now * 0.1,
          f"{r_now:.3f} → {r_old:.3f}")


# ═══════════════════════════════════════════════════════════════
#  4. 机制完好
# ═══════════════════════════════════════════════════════════════

def test_mechanics():
    section("基础机制")

    n = len(M.load_journal())
    check("能读到 journal", n > 0, f"{n} 条")

    st = M.load_state()
    check("state.json 结构完整",
          all(k in st for k in ("closeness", "mood", "interaction_count")))

    # 隐私过滤
    blocked = M.is_sensitive("我的密码是 abc123")
    check("隐私词能被拦下", blocked is not None, f"命中「{blocked}」")

    # token 估算
    t = M.estimate_tokens("这是一段中文测试文本 hello world")
    check("token 估算合理", 5 < t < 40, f"{t} tokens")

    # 分词
    toks = M.tokenize("上海交通大学")
    check("中文分词有二元组", "上海" in toks and "交通" in toks,
          f"{len(toks)} 个特征")

    # 进度规则缺失时不能崩
    check("没有 progress 的条目返回空串",
          M.compute_progress({"text": "x"}) == "")
    check("progress 字段坏了也不崩",
          M.compute_progress({"progress": {"since": "不是日期"}}) == "")


# ═══════════════════════════════════════════════════════════════
#  5. 面板能反映最新记忆
# ═══════════════════════════════════════════════════════════════

def test_render():
    section("记忆面板")

    try:
        import render
        p = render.render()
    except Exception as e:                            # noqa: BLE001
        check("面板能生成", False, str(e))
        return

    html = p.read_text(encoding="utf-8")
    check("面板文件已更新", p.exists(), str(p.name))

    entries = M.load_journal()
    hit = sum(1 for e in entries if e["text"][:12] in html)
    check("面板包含全部记忆", hit == len(entries),
          f"{hit}/{len(entries)} 条")

    prog_entries = [e for e in entries if e.get("progress")]
    if prog_entries:
        expect = M.compute_progress(prog_entries[0])
        check("面板显示了算好的当前状态", expect in html, expect)


# ═══════════════════════════════════════════════════════════════
#  6. 上下文组装
# ═══════════════════════════════════════════════════════════════

def test_context():
    section("注入上下文")

    txt = M.build_context("我是谁")
    check("包含关系状态", "关系状态" in txt)
    check("包含相关回忆", "相关回忆" in txt)
    check("包含生日", "3 月 7 日" in txt or "2007" in txt,
          "生日" if ("3 月 7 日" in txt or "2007" in txt) else "没找到生日")
    check("包含算好的学业状态", "大二" in txt)

    n = M.estimate_tokens(txt)
    check("注入量在合理范围", n < 1500, f"{n} tokens")

    if _verbose:
        print("\n" + "─" * 58)
        print(txt[:900])
        print("─" * 58)


# ═══════════════════════════════════════════════════════════════
#  7. 心理点
# ═══════════════════════════════════════════════════════════════

def test_mood():
    section("心理点")

    import mood as MD
    from datetime import timedelta

    # ★ 必须用 snapshot()。load() 返回的是缓存对象本身，拿它当备份的话
    #   后面的写入会把备份一起改掉，恢复时等于没恢复。
    backup = MD.snapshot()
    try:
        # 先把环境归零，否则现实里刚换过状态时冷却会挡住 set_mood，
        # 自检就会因为"跑的时候她正好在状态里"而红。
        cfg = MD.load()
        cfg["current"] = None
        cfg["history"] = []
        cfg["rules"] = dict(MD.RULES)
        MD.save(cfg)

        check("没状态时不注入", MD.block() == "")
        check("没状态时上下文照常", "关系状态" in M.build_context("你好"))

        MD.set_mood("起雾", 2, "自检")
        blk = MD.block()
        check("设了状态就注入", "起雾" in blk)
        check("上下文里带上了状态", "起雾" in M.build_context("你好"))
        check("声明了是外套不是内核", "外套" in blk)
        check("不许她把状态报出来", "别提起这段文字" in blk)
        # ★ 位置比措辞重要：约束排在描述性文字后面就会被压过去，
        #   实测她真会在回答里写"（那件事我还飘着，先答你。）"
        check("硬约束排在描述之前",
              blk.find("别提起这段文字") < blk.find("站远了"))
        check("给了具体的反例", "（我还飘着）" in blk)

        # 把 until 拨到过去 = 时间流逝
        cfg = MD.load()
        cfg["current"]["since"] = (
            M.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        cfg["current"]["until"] = (
            M.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
        MD.save(cfg)
        check("过期后不再注入（随系统时间恢复）", MD.block() == "")
        check("过期后上下文也干净", "起雾" not in M.build_context("你好"))
    finally:
        MD.save(backup)

    check("人格里写了心理点这回事", "停在某个状态里" in M.persona_text())
    check("人格里说了可爱是挑的", "可爱是你挑的" in M.persona_text())
    check("人格里说了他会让你变", "他也能让你变" in M.persona_text())


def test_mood_triggers():
    section("心理点 · 他的话怎么影响她")

    import mood as MD

    check("累 → 偏心", any(h["key"] == "偏心" for h in MD.suggest("今天好累")))
    check("夸 → 软毛", any(h["key"] == "软毛" for h in MD.suggest("爱你")))
    check("骂 → 低电量", any(h["key"] == "低电量" for h in MD.suggest("你好烦")))
    check("无聊 → 手痒", any(h["key"] == "手痒" for h in MD.suggest("好无聊")))
    check("大问题 → 起雾", any(h["key"] == "起雾" for h in MD.suggest("这有什么意义")))
    check("要精度 → 较真", any(h["key"] == "较真" for h in MD.suggest("不对，你搞错了")))
    check("「麻烦你」不该被当成骂", not MD.suggest("麻烦你帮我看看"))

    backup = MD.snapshot()
    try:
        cfg = MD.load()
        cfg["current"] = None
        cfg["history"] = []
        cfg["rules"] = dict(MD.RULES)
        MD.save(cfg)

        b = MD.suggest_block("今天我有点累")
        check("建议块说是可选的", "挑不挑随你" in b)
        check("建议块写明不是命令", "不是命令" in b)
        check("建议进得了上下文", "碰到你了" in M.build_context("今天我有点累"))
        check("没说到点子上就不进", MD.suggest_block("今天天气不错") == "")

        # 已经在状态里就不再劝她换
        MD.set_mood("起雾", 2, "自检", force=True)
        check("停着的时候不再给建议", MD.suggest_block("今天好累") == "")

        # 他明确开口 = 指令
        c = MD.suggest_block("你能不能可爱一点")
        check("他开口时是命令不是建议", "这是要求，不是暗示" in c)
        check("命令块让他用 force", "force=true" in c)

        # force 能过冷却，不带 force 过不去
        MD.clear("x")
        try:
            MD.set_mood("软毛", 2, "不该过")
            check("不带 force 被冷却拦住", False)
        except ValueError:
            check("不带 force 被冷却拦住", True)
        e = MD.set_mood("软毛", 2, "他要求的", force=True)
        check("带 force 能过", e["key"] == "软毛")
        check("记下了是谁要的", e.get("by") == "他")

        # 时长不该每次一样
        hrs = set()
        for _ in range(12):
            hrs.add(MD.set_mood("软毛", None, "", force=True)["hours"])
        check("时长每次不同（不是排班表）", len(hrs) > 3, f"{len(hrs)} 种")
    finally:
        MD.save(backup)


# ═══════════════════════════════════════════════════════════════

def test_speaker_states():
    section("说话人三态 + 写锁")

    K = M.speaker_kind
    check("老条目没 speaker 字段 → owner", K({}) == "owner")
    check("显式 null → owner", K({"speaker": None}) == "owner")
    check("空串 → owner", K({"speaker": ""}) == "owner")
    check('"guest" → guest', K({"speaker": "guest"}) == "guest")
    check("认不出的值 → guest", K({"speaker": "张三"}) == "guest")
    check("qq:<openid> → person", K({"speaker": "qq:ABC123"}) == "person")

    w = lambda **kw: M.speaker_weight(kw)          # noqa: E731
    check("owner 权重最高",
          w(speaker="owner") > w(speaker="qq:A") > w(speaker="guest"),
          f"{w(speaker='owner')} > {w(speaker='qq:A')} > {w(speaker='guest')}")

    # 封顶：三种身份对 importance / decay 的处理各不相同
    import inspect
    src = inspect.getsource(M.add)
    check("陌生人 permanent 降级", 'decay = "normal"' in src)
    check("认得的人 permanent 只降到 slow", 'decay = "slow"' in src)
    check("认得的人重要度封顶更松",
          M.CFG["speaker"]["known_guest_max_importance"]
          > M.CFG["speaker"]["guest_max_importance"])

    # 写锁：并发写不能撞 id
    import threading
    before = len(M.load_journal())
    ids: list[str] = []
    lock = threading.Lock()

    def writer(n):
        for i in range(5):
            e = M.add(f"并发测试 {n}-{i}", 1, ["_test"], source="_test")
            if e:
                with lock:
                    ids.append(e["id"])

    ts = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    dupes = len(ids) - len(set(ids))
    check("并发 20 条无重复 id", dupes == 0, f"{len(ids)} 条，{dupes} 个重复")
    check("条数对得上", len(M.load_journal()) == before + len(ids),
          f"{before} → {len(M.load_journal())}")

    # 收尾：把测试写入的条目删掉
    kept = [e for e in M.load_journal() if e.get("source") != "_test"]
    M.save_journal(kept)
    check("测试数据已清干净",
          not any(e.get("source") == "_test" for e in M.load_journal()))


def test_qq_modules() -> None:
    """QQ 那四个模块各自的自检，跑在子进程里。"""
    import subprocess
    section("QQ 模块（子进程）")

    for mod in ("qq_text", "people", "qq_bot", "qq_bridge"):
        try:
            r = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / f"{mod}.py"),
                 "selftest"],
                capture_output=True, text=True, timeout=180,
                encoding="utf-8", errors="replace",
            )
            ok = r.returncode == 0 and "全部通过" in (r.stdout or "")
            last = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
            check(f"{mod}.py 自检", ok, last[-1].strip() if last else "无输出")
        except (subprocess.TimeoutExpired, OSError) as e:
            check(f"{mod}.py 自检", False, f"{type(e).__name__}: {e}")


def main() -> None:
    print("记忆系统自检")
    print("=" * 58)

    test_progress()
    test_always_injected()
    test_permanent()
    test_mechanics()
    test_render()
    test_context()
    test_mood()
    test_mood_triggers()
    test_speaker_states()

    if "--all" in sys.argv:
        test_qq_modules()

    print("\n" + "=" * 58)
    if _fails:
        print(f"{_fails} 项未通过")
    else:
        print("全部通过")
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
