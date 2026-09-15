#!/usr/bin/env python3
"""
render.py —— 把记忆库渲染成一个自包含的 HTML 面板

生成 view/memory.html，双击就能用浏览器打开。
无外部依赖、无 CDN、不联网——所有 CSS/JS 都内联。

用法：
    python src/render.py
    python src/render.py --open      # 生成后自动打开浏览器
"""

from __future__ import annotations

import html
import json
import math
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def heat(e: dict, ref: datetime) -> float:
    """当前记忆热度 0~1：衰减后的残余强度。"""
    return M._recency_factor(e, ref)


def decay_label(e: dict) -> tuple[str, str]:
    d = e.get("decay", "normal")
    return {
        "permanent": ("永久", "#c084fc"),
        "slow": ("慢衰减", "#38bdf8"),
        "normal": ("常规", "#94a3b8"),
    }.get(d, ("常规", "#94a3b8"))


def esc(s) -> str:
    return html.escape(str(s))


def render() -> Path:
    entries = M.load_journal()
    st = M.load_state()
    ref = M.now()

    # ---------- 统计 ----------
    total_tokens = sum(M.estimate_tokens(e["text"]) for e in entries)
    by_decay: dict[str, int] = {}
    tag_count: dict[str, int] = {}
    for e in entries:
        by_decay[e.get("decay", "normal")] = by_decay.get(e.get("decay", "normal"), 0) + 1
        for t in e.get("tags") or []:
            tag_count[t] = tag_count.get(t, 0) + 1

    # ---------- 排序：新的在前 ----------
    entries_sorted = sorted(entries, key=lambda x: x["ts"], reverse=True)

    # ---------- 时间线 ----------
    rows = []
    for e in entries_sorted:
        h = heat(e, ref)
        bar = int(round(h * 100))
        dname, dcolor = decay_label(e)
        imp = e.get("importance", 3)
        stars = "★" * imp + "☆" * (5 - imp)
        ts = M.parse_ts(e["ts"])
        age_days = (ref - ts).days
        age = "今天" if age_days == 0 else f"{age_days} 天前"

        tags = "".join(
            f'<span class="tag" data-tag="{esc(t)}">#{esc(t)}</span>'
            for t in (e.get("tags") or [])
        )
        emo = (f'<span class="emo">{esc(e["emotion"])}</span>'
               if e.get("emotion") else "")

        # 会随时间变化的事实，把「当前状态」现算出来显示
        prog = M.compute_progress(e, ref)
        prog_html = (f'<div class="prog">⏳ 当前：{esc(prog)}</div>'
                     if prog else "")

        rows.append(f"""
        <div class="row" data-tags="{esc(' '.join(e.get('tags') or []))}">
          <div class="heat" title="记忆热度 {bar}%">
            <div class="heat-bar" style="height:{bar}%;background:{dcolor}"></div>
            <span class="heat-num">{bar}</span>
          </div>
          <div class="body">
            <div class="text">{esc(e['text'])}</div>
            {prog_html}
            <div class="meta">
              <span class="stars" title="重要度 {imp}/5">{stars}</span>
              <span class="dot" style="background:{dcolor}"></span>
              <span class="decay">{dname}</span>
              <span class="sep">·</span>
              <span>{ts:%m月%d日}</span>
              <span class="sep">·</span>
              <span class="age">{age}</span>
              {emo}
              {tags}
            </div>
          </div>
        </div>""")

    # ---------- 标签云 ----------
    cloud = "".join(
        f'<button class="chip" data-filter="{esc(t)}">{esc(t)}'
        f'<span class="n">{n}</span></button>'
        for t, n in sorted(tag_count.items(), key=lambda x: -x[1])
    ) or '<span class="muted">还没有标签</span>'

    # ---------- 承诺 ----------
    promises = st.get("pending_promises") or []
    promise_html = "".join(f"<li>{esc(p)}</li>" for p in promises) or \
        '<li class="muted">没有未兑现的承诺</li>'

    # ---------- 摘要 / 归档 ----------
    sm = sorted(M._p("summaries").glob("*.md")) if M._p("summaries").exists() else []
    arc = sorted(M._p("archive").glob("*.jsonl")) if M._p("archive").exists() else []
    sm_html = "".join(f"<li>{esc(p.name)}</li>" for p in sm) or \
        '<li class="muted">还没有压缩摘要</li>'
    arc_html = "".join(f"<li>{esc(p.name)}</li>" for p in arc) or \
        '<li class="muted">还没有归档</li>'

    # ---------- 关系状态 ----------
    last = st.get("last_interaction")
    if last:
        gap = (ref - M.parse_ts(last)).total_seconds() / 3600
        gap_txt = f"{gap:.1f} 小时前" if gap < 48 else f"{gap / 24:.1f} 天前"
    else:
        gap_txt = "—"

    decay_bars = "".join(
        f'<div class="dbar"><span>{ {"permanent":"永久","slow":"慢衰减","normal":"常规"}.get(k,k) }</span>'
        f'<div class="track"><div class="fill" style="width:'
        f'{n / max(1, len(entries)) * 100:.0f}%;background:{decay_label({"decay": k})[1]}"></div></div>'
        f'<b>{n}</b></div>'
        for k, n in sorted(by_decay.items())
    )

    # ---------- 心理点（她自己挑的状态，到点自动散）----------
    try:
        import mood as _mood
        _cur = _mood.active()
    except ImportError:
        _cur = None
    if _cur:
        mood_k = f"停在「{_cur['key']}」"
        mood_v = f"{_cur['left_min']} 分"
        mood_sub = "淡出中" if _cur["fading"] else "后散"
    else:
        mood_k, mood_v, mood_sub = "心理点", "—", ""

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIPet · 记忆面板</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  :root{{
    --bg:#0d1014; --card:#161a20; --card2:#1c2229; --line:#252c35;
    --fg:#e6eaf0; --dim:#8b97a8; --accent:#5eead4; --warn:#fbbf24;
  }}
  body{{background:var(--bg);color:var(--fg);
    font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
    padding:28px;max-width:1080px;margin:0 auto}}
  h1{{font-size:20px;font-weight:600;letter-spacing:.5px}}
  h1 small{{color:var(--dim);font-weight:400;font-size:12px;margin-left:10px}}
  h2{{font-size:13px;font-weight:600;color:var(--dim);text-transform:uppercase;
    letter-spacing:1.2px;margin:26px 0 12px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
  .stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
  .stat .k{{font-size:11px;color:var(--dim);letter-spacing:.5px}}
  .stat .v{{font-size:24px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}}
  .stat .v small{{font-size:12px;color:var(--dim);font-weight:400;margin-left:3px}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}}
  ul{{list-style:none}}
  li{{padding:3px 0}}
  .muted{{color:var(--dim)}}
  .dbar{{display:flex;align-items:center;gap:10px;padding:4px 0;font-size:12px}}
  .dbar span{{width:52px;color:var(--dim)}}
  .dbar b{{width:26px;text-align:right;font-variant-numeric:tabular-nums}}
  .track{{flex:1;height:6px;background:var(--card2);border-radius:3px;overflow:hidden}}
  .fill{{height:100%;border-radius:3px}}
  .chips{{display:flex;flex-wrap:wrap;gap:7px}}
  .chip{{background:var(--card2);border:1px solid var(--line);color:var(--fg);
    border-radius:20px;padding:5px 12px;font-size:12px;cursor:pointer;
    font-family:inherit;transition:.15s}}
  .chip:hover{{border-color:var(--accent);color:var(--accent)}}
  .chip.on{{background:var(--accent);color:#0d1014;border-color:var(--accent)}}
  .chip .n{{color:var(--dim);margin-left:6px;font-size:11px}}
  .chip.on .n{{color:#0d1014;opacity:.6}}
  .row{{display:flex;gap:14px;padding:12px 4px;border-bottom:1px solid var(--line)}}
  .row:last-child{{border-bottom:none}}
  .row.hide{{display:none}}
  .heat{{position:relative;width:26px;flex:0 0 26px;height:44px;
    background:var(--card2);border-radius:5px;overflow:hidden;
    display:flex;align-items:flex-end}}
  .heat-bar{{width:100%;border-radius:5px;transition:.3s}}
  .heat-num{{position:absolute;inset:0;display:flex;align-items:center;
    justify-content:center;font-size:10px;color:var(--dim);
    font-variant-numeric:tabular-nums;mix-blend-mode:difference}}
  .body{{flex:1;min-width:0}}
  .text{{margin-bottom:4px}}
  .meta{{display:flex;flex-wrap:wrap;align-items:center;gap:7px;
    font-size:11.5px;color:var(--dim)}}
  .stars{{color:var(--warn);letter-spacing:1px}}
  .dot{{width:6px;height:6px;border-radius:50%;display:inline-block}}
  .tag{{color:#7dd3fc;cursor:pointer}}
  .tag:hover{{text-decoration:underline}}
  .emo{{background:var(--card2);border-radius:4px;padding:1px 7px}}
  .prog{{margin-top:5px;font-size:12px;color:#7dd3fc;
        background:rgba(56,189,248,.09);border-left:2px solid #38bdf8;
        border-radius:0 5px 5px 0;padding:4px 9px;display:inline-block}}
  .sep{{opacity:.4}}
  .bar{{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}}
  .bar input{{background:var(--card);border:1px solid var(--line);color:var(--fg);
    border-radius:8px;padding:8px 12px;font-size:13px;font-family:inherit;
    outline:none;flex:1;min-width:180px}}
  .bar input:focus{{border-color:var(--accent)}}
  .bar button{{background:var(--card2);border:1px solid var(--line);color:var(--dim);
    border-radius:8px;padding:8px 14px;font-size:12px;cursor:pointer;font-family:inherit}}
  .bar button:hover{{color:var(--fg);border-color:var(--accent)}}
  footer{{margin-top:30px;padding-top:16px;border-top:1px solid var(--line);
    color:var(--dim);font-size:11.5px}}
  .two{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
  @media(max-width:640px){{.two{{grid-template-columns:1fr}}body{{padding:16px}}}}
</style>
</head>
<body>

<h1>AIPet 记忆面板 <small>生成于 {ref:%Y-%m-%d %H:%M}</small></h1>

<div class="grid" style="margin-top:18px">
  <div class="stat"><div class="k">记忆条目</div><div class="v">{len(entries)}</div></div>
  <div class="stat"><div class="k">占用 TOKEN</div><div class="v">{total_tokens}</div></div>
  <div class="stat"><div class="k">亲密度</div><div class="v">{st.get('closeness', 0)}</div></div>
  <div class="stat"><div class="k">累计互动</div><div class="v">{st.get('interaction_count', 0)}<small>次</small></div></div>
  <div class="stat"><div class="k">当前情绪</div><div class="v" style="font-size:18px">{esc(st.get('mood', '—'))}</div></div>
  <div class="stat"><div class="k">距上次互动</div><div class="v" style="font-size:18px">{gap_txt}</div></div>
  <div class="stat"><div class="k">{esc(mood_k)}</div><div class="v" style="font-size:18px">{mood_v}<small>{mood_sub}</small></div></div>
</div>

<h2>记忆构成</h2>
<div class="two">
  <div class="card">{decay_bars}</div>
  <div class="card">
    <div class="k" style="font-size:11px;color:var(--dim);letter-spacing:.5px;margin-bottom:6px">未兑现的承诺</div>
    <ul>{promise_html}</ul>
  </div>
</div>

<h2>标签</h2>
<div class="chips" id="cloud">{cloud}</div>

<h2>时间线</h2>
<div class="bar">
  <input id="q" placeholder="过滤记忆…（按内容或标签）">
  <button id="clear">清空</button>
  <button id="sort">按热度排序</button>
</div>
<div class="card" id="list">{''.join(rows) or '<div class="muted">还没有记忆。运行 python src/memory.py demo 灌入演示数据。</div>'}</div>

<h2>压缩与归档</h2>
<div class="two">
  <div class="card">
    <div class="k" style="font-size:11px;color:var(--dim);margin-bottom:6px">周/月摘要</div>
    <ul>{sm_html}</ul>
  </div>
  <div class="card">
    <div class="k" style="font-size:11px;color:var(--dim);margin-bottom:6px">原始归档</div>
    <ul>{arc_html}</ul>
  </div>
</div>

<footer>
  本面板由 <code>src/render.py</code> 生成，数据来自
  <code>memory/journal.jsonl</code> 与 <code>memory/state.json</code>。<br>
  左边色条是「记忆热度」——时间衰减后的残余强度。永久类记忆永远是 100，日常记忆会慢慢变暗，
  低于阈值就不再进入对话上下文，但不会被删除。
</footer>

<script>
const rows = [...document.querySelectorAll('.row')];
const q = document.getElementById('q');
const chips = document.getElementById('cloud');

function apply(){{
  const term = q.value.trim().toLowerCase();
  const active = [...chips.querySelectorAll('.chip.on')].map(c => c.dataset.filter);
  rows.forEach(r => {{
    const txt = r.querySelector('.text').textContent.toLowerCase();
    const tags = (r.dataset.tags || '').split(' ');
    const okText = !term || txt.includes(term) || tags.some(t => t.toLowerCase().includes(term));
    const okTag = !active.length || active.every(a => tags.includes(a));
    r.classList.toggle('hide', !(okText && okTag));
  }});
}}

q.addEventListener('input', apply);
chips.addEventListener('click', e => {{
  const c = e.target.closest('.chip');
  if (c) {{ c.classList.toggle('on'); apply(); }}
}});
document.querySelectorAll('.tag').forEach(t => t.addEventListener('click', () => {{
  const c = [...chips.querySelectorAll('.chip')].find(x => x.dataset.filter === t.dataset.tag);
  if (c) {{ c.classList.add('on'); apply(); }}
}}));
document.getElementById('clear').addEventListener('click', () => {{
  q.value = '';
  chips.querySelectorAll('.chip.on').forEach(c => c.classList.remove('on'));
  apply();
}});
document.getElementById('sort').addEventListener('click', e => {{
  const list = document.getElementById('list');
  const asc = e.target.dataset.asc === '1';
  e.target.dataset.asc = asc ? '0' : '1';
  e.target.textContent = asc ? '按热度排序' : '按时间排序';
  rows.sort((a,b) => {{
    const ha = +a.querySelector('.heat-num').textContent;
    const hb = +b.querySelector('.heat-num').textContent;
    return asc ? ha - hb : hb - ha;
  }});
  rows.forEach(r => list.appendChild(r));
}});
</script>
</body>
</html>"""

    out = M._p("view")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out


def main() -> None:
    p = render()
    print(f"已生成：{p}")
    if "--open" in sys.argv:
        webbrowser.open(p.as_uri())


if __name__ == "__main__":
    main()
