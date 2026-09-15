#!/usr/bin/env python3
"""
knowledge.py —— 本地资料检索（轻量知识库）

═══════════════════════════════════════════════════════════════
  先读这段：你多半不需要"知识库"
═══════════════════════════════════════════════════════════════

RAG / 向量库解决的是「模型不知道的私有知识」。但桌宠场景下：

  · "关于你的事"    → 已经在 PROFILE.md + journal.jsonl 里了，
                      而且那套检索（关键词 + 评分 + 时间衰减）已经够用
  · "世界上的事"    → 搜索接口就够，不需要预先建库
  · 真 RAG 的成本   → 文档解析 + 分块 + embedding + 向量库 + 重排
                      单人本机小规模，这是**过度工程**

所以：默认关闭。等下面两种情况真的出现了再打开——

  ① 你有一批固定的私有资料要反复查
     （500 页教材、你的笔记库、某份长规范文档）
  ② 你想让它"读过"某个世界观设定，用来角色扮演
     ← 这才是桌宠场景里真正有意思的用法

而且即使打开了，这里用的也是**关键词检索，不是向量检索**。
两千块以内的资料，关键词比 embedding 更快、更准、还不用装依赖。
上万块再考虑向量库。

═══════════════════════════════════════════════════════════════

用法：
    # 1. 把资料丢进 knowledge/ 文件夹（支持 .md .txt .markdown）
    #    其他格式（PDF/Word/PPT）先用 Cherry Studio 的文档转换转成 Markdown
    # 2. 建索引
    python src/knowledge.py build
    # 3. 检索
    python src/knowledge.py search "梯度下降的学习率怎么选"
    # 4. 在 config.json 里把 knowledge.enabled 改成 true
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

KCFG = M.CFG.get("knowledge", {})
DOCS_DIR = M.ROOT / KCFG.get("docs_dir", "knowledge")
INDEX_FILE = M.ROOT / "data" / "knowledge_index.json"
SUPPORTED = {".md", ".markdown", ".txt", ".text"}


# ---------------------------------------------------------------- 分块

def _split_long(text: str, size: int) -> list[str]:
    """段落切分，尽量不切断段落。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 2 <= size:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                out.append(buf)
            # 单个段落就超长 → 按句子硬切
            if len(p) > size:
                sents = re.split(r"(?<=[。！？.!?])\s*", p)
                cur = ""
                for s in sents:
                    if len(cur) + len(s) <= size:
                        cur += s
                    else:
                        if cur:
                            out.append(cur)
                        cur = s
                if cur:
                    out.append(cur)
                buf = ""
            else:
                buf = p
    if buf:
        out.append(buf)
    return out


def chunk_document(path: Path, size: int) -> list[dict]:
    """先按 Markdown 标题切，太长再按段落切。保留标题作为上下文。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = path.relative_to(M.ROOT).as_posix()

    # 按标题切段，记录每段的标题路径
    sections: list[tuple[str, str]] = []
    cur_head, cur_lines = path.stem, []
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if cur_lines:
                sections.append((cur_head, "\n".join(cur_lines).strip()))
            cur_head, cur_lines = m.group(2).strip(), []
        else:
            cur_lines.append(line)
    if cur_lines:
        sections.append((cur_head, "\n".join(cur_lines).strip()))

    chunks = []
    for head, body in sections:
        if not body:
            continue
        for piece in _split_long(body, size):
            chunks.append({
                "doc": rel,
                "stem": path.stem,
                "heading": head,
                "text": piece,
                "tokens": sorted(M.tokenize(head + " " + piece)),
            })
    return chunks


def build() -> dict:
    if not DOCS_DIR.exists():
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        return {"docs": 0, "chunks": 0,
                "note": f"已创建 {DOCS_DIR}，把资料放进去再跑一次"}

    size = KCFG.get("chunk_size", 600)
    files = [p for p in DOCS_DIR.rglob("*")
             if p.is_file() and p.suffix.lower() in SUPPORTED]

    chunks, skipped = [], []
    for f in files:
        try:
            chunks.extend(chunk_document(f, size))
        except Exception as e:
            skipped.append(f"{f.name}: {e}")

    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps({
        "built": M.now_iso(),
        "docs": len(files),
        "chunks": chunks,
    }, ensure_ascii=False), encoding="utf-8")

    result = {"docs": len(files), "chunks": len(chunks)}
    if skipped:
        result["skipped"] = skipped
    # 提示不支持的文件
    others = [p.name for p in DOCS_DIR.rglob("*")
              if p.is_file() and p.suffix.lower() not in SUPPORTED]
    if others:
        result["note"] = (f"{len(others)} 个文件格式不支持，"
                          f"先用文档转换转成 Markdown：{others[:5]}")
    return result


# ---------------------------------------------------------------- 检索

def load_index() -> list[dict]:
    if not INDEX_FILE.exists():
        return []
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8")).get("chunks", [])
    except (json.JSONDecodeError, OSError):
        return []


def retrieve(query: str, k: int | None = None) -> list[dict]:
    k = k or KCFG.get("max_chunks", 6)
    chunks = load_index()
    if not chunks:
        return []

    qtoks = M.tokenize(query)
    if not qtoks:
        return []

    scored = []
    for c in chunks:
        ctoks = set(c.get("tokens") or M.tokenize(c["text"]))
        overlap = qtoks & ctoks
        if not overlap:
            continue
        # 正文覆盖率 + 命中精确度 + 标题命中加成
        cov_q = len(overlap) / len(qtoks)
        cov_c = len(overlap) / max(1, len(ctoks))
        head_hit = 1.25 if M.tokenize(c.get("heading", "")) & qtoks else 1.0
        scored.append((round((0.75 * cov_q + 0.25 * cov_c) * head_hit, 4), c))

    scored.sort(key=lambda x: -x[0])
    return [{**c, "_score": s} for s, c in scored[:k]]


def as_prompt_block(query: str, k: int | None = None) -> str:
    if not KCFG.get("enabled"):
        return ""
    hits = retrieve(query, k)
    if not hits:
        return ""
    lines = [f"## 本地资料（{query}）"]
    for h in hits:
        lines.append(f"\n### {h['stem']} › {h['heading']}\n{h['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]

    if cmd == "build":
        print(json.dumps(build(), ensure_ascii=False, indent=2))

    elif cmd == "search":
        q = args[1] if len(args) > 1 else ""
        hits = retrieve(q)
        if not hits:
            print("没命中。索引建了吗？（python src/knowledge.py build）")
            return
        for h in hits:
            print(f"\n── [{h['_score']}] {h['stem']} › {h['heading']}")
            print(h["text"][:400] + ("…" if len(h["text"]) > 400 else ""))

    elif cmd == "stats":
        chunks = load_index()
        docs: dict[str, int] = {}
        for c in chunks:
            docs[c["doc"]] = docs.get(c["doc"], 0) + 1
        print(json.dumps({
            "已启用": KCFG.get("enabled", False),
            "资料目录": str(DOCS_DIR),
            "文档数": len(docs),
            "分块数": len(chunks),
            "索引大小": (f"{INDEX_FILE.stat().st_size / 1024:.1f} KB"
                      if INDEX_FILE.exists() else "未建立"),
            "各文档分块": docs,
        }, ensure_ascii=False, indent=2))

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
