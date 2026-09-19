#!/usr/bin/env python3
"""
docx_read.py —— 读 Word 文档（.docx）

小日和的脑子只认文本。文档里除了字，还常常有**截图和扫描件** ——
那些是图，不是字，光解析 XML 拿不到。所以这里分两步：

    一、从 XML 里把文字抽出来（段落、表格、换行、制表符）
    二、把文档里的图交给 vision.py 读成文字，插回它原来出现的位置

**不用 python-docx。** docx 本身就是个 zip，里面是几个 XML；
标准库的 zipfile + ElementTree 足够把文字和图片捞出来，
不值得为这个给所有人多装一个包。

═══════════════════════════════════════════════════════════════
  能读什么、读不了什么
═══════════════════════════════════════════════════════════════

    ✅ .docx        文本、表格、内嵌图片
    ❌ .doc         老的二进制格式（OLE 复合文档），解不了。
                    提示对方"另存为 .docx"
    ❌ .pdf         另一套东西，没做
    ❌ 批注、修订痕迹、页眉页脚、脚注 —— 正文之外的部分先不管

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════

    python src/docx_read.py 看 <路径>     # 命令行读一份
    python src/docx_read.py selftest      # 自检（现造一份 docx 来读）
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── 上限 ────────────────────────────────────────────────────
# 文字：一篇报告几万字很正常，但整篇塞进 prompt 会把它撑爆。
MAX_CHARS = 120_000

# 图片：每张都要真调一次视觉接口，又慢又要钱。超了就只列名字不读。
# 这个数字是"一份文档里真正需要读的图"的合理上限 —— 一整本扫描件
# 不该走这条路，那种该先转成文本。
MAX_IMAGES = 12

# 办公文档的 XML 命名空间
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
V = "{urn:schemas-microsoft-com:vml}"

DOCX_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0"          # .doc / .xls / .ppt 那一族

IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".emf", ".wmf"}

DEFAULT_IMAGE_QUESTION = (
    "把这张图里的文字逐字抄下来，保留原有分行。如果图里没有文字，"
    "就用一句话说明它是什么。不要评价，不要推测用途。"
)


def _local(tag: str) -> str:
    """去掉命名空间，只留标签名。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# ═══════════════════════════════════════════════════════════════
#  文字
# ═══════════════════════════════════════════════════════════════

def _para_text(p) -> tuple[str, list[str]]:
    """
    一个段落 → (文字, 里面引用的图片 rId 列表)。

    按文档顺序走一遍：`w:t` 是文字，`w:tab`/`w:br` 是控制符，
    `a:blip` 和 `v:imagedata` 是图片引用。

    ★ 图片用 rId 记下来，等会儿再解析成实际文件 —— 这一步只认 XML，
      不碰 zip，两者分开才好测。
    """
    out: list[str] = []
    rids: list[str] = []
    for node in p.iter():
        name = _local(node.tag)
        if name == "t":
            out.append(node.text or "")
        elif name == "tab":
            out.append("\t")
        elif name in ("br", "cr"):
            out.append("\n")
        elif name == "blip":
            rid = node.get(f"{R}embed") or node.get(f"{R}link")
            if rid:
                rids.append(rid)
        elif name == "imagedata":
            rid = node.get(f"{R}id")
            if rid:
                rids.append(rid)
    return "".join(out), rids


def _cell_text(tc) -> str:
    """表格单元格：里面所有段落合成一行。"""
    parts = []
    for p in tc.iter(f"{W}p"):
        t, _ = _para_text(p)
        if t.strip():
            parts.append(t.strip())
    return " ".join(parts)


def _walk(node, lines: list[str], rids: list[str]) -> None:
    """
    按顺序遍历正文。

    ★ 必须保持文档顺序 —— 图片读出来的文字要插在它原来出现的地方，
      全堆到末尾的话，图说和上下文就对不上了。
    """
    for child in node:
        name = _local(child.tag)
        if name == "p":
            text, sub_rids = _para_text(child)
            if text.strip():
                lines.append(text.rstrip())
            rids.extend(sub_rids)
        elif name == "tbl":
            for tr in child.findall(f"{W}tr"):
                cells = [_cell_text(tc) for tc in tr.findall(f"{W}tc")]
                if any(c for c in cells):
                    lines.append(" | ".join(cells))
            lines.append("")
        elif name in ("sdt", "sdtContent", "body", "txbxContent"):
            _walk(child, lines, rids)     # 内容控件、文本框：往里挖
        # 其余（sectPr 等）跳过


def extract_text(document_xml: bytes) -> tuple[str, list[str]]:
    """document.xml → (正文文字, 图片 rId 顺序表)"""
    root = ET.fromstring(document_xml)
    body = root.find(f"{W}body")
    if body is None:
        return "", []
    lines: list[str] = []
    rids: list[str] = []
    _walk(body, lines, rids)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)         # 连续空行压成一个
    return text.strip(), rids


def _rels_map(zf: zipfile.ZipFile) -> dict[str, str]:
    """
    rId → zip 内的路径。rId 是文档里引用图片用的编号，
    真正的文件名在 word/_rels/document.xml.rels 里。
    """
    out: dict[str, str] = {}
    try:
        xml = zf.read("word/_rels/document.xml.rels")
    except KeyError:
        return out
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for rel in root:
        rid, target = rel.get("Id"), rel.get("Target")
        if not rid or not target:
            continue
        if target.startswith("/"):
            out[rid] = target.lstrip("/")
        elif target.startswith("http"):
            continue                                # 外链图片，够不着
        else:
            # Target 是相对 word/ 的路径，可能带 ../
            parts = ("word/" + target).split("/")
            norm: list[str] = []
            for seg in parts:
                if seg == "..":
                    if norm:
                        norm.pop()
                elif seg not in ("", "."):
                    norm.append(seg)
            out[rid] = "/".join(norm)
    return out


# ═══════════════════════════════════════════════════════════════
#  读
# ═══════════════════════════════════════════════════════════════

def looks_like_docx(path: str | Path) -> bool:
    p = Path(path)
    if p.suffix.lower() != ".docx":
        return False
    try:
        with p.open("rb") as f:
            return f.read(4) == DOCX_MAGIC
    except OSError:
        return False


def read(path: str | Path, with_images: bool = True,
         image_question: str = "") -> dict:
    """
    读一份 .docx。

    **不抛异常** —— 调用方是模型，拿到异常多半会编一段看起来像正文的东西，
    那比说"读不了"糟糕得多。失败一律返回 {"ok": False, "error": "..."}。
    """
    p = Path(path)
    out = {"ok": False, "text": "", "images": [], "error": "", "truncated": False}

    if not p.exists():
        out["error"] = f"找不到这个文件：{p}"
        return out
    if p.is_dir():
        out["error"] = f"{p} 是目录，不是文档"
        return out

    try:
        head = p.open("rb").read(8)
    except OSError as e:
        out["error"] = f"读不了：{e}"
        return out

    if head.startswith(OLE_MAGIC):
        out["error"] = (f"{p.name} 是老的 .doc 格式（不是 .docx），"
                        f"它是一整个二进制文件，解不出文字。"
                        f"让对方用 Word 另存为 .docx 就行。")
        return out
    if not head.startswith(DOCX_MAGIC):
        out["error"] = f"{p.name} 不是一个 docx（文件头对不上）。"
        return out

    try:
        zf = zipfile.ZipFile(p)
    except zipfile.BadZipFile as e:
        out["error"] = f"{p.name} 打不开：{e}"
        return out

    with zf:
        try:
            doc = zf.read("word/document.xml")
        except KeyError:
            out["error"] = (f"{p.name} 里没有 word/document.xml —— "
                            f"是个压缩包，但不是 Word 文档。")
            return out

        try:
            text, rids = extract_text(doc)
        except ET.ParseError as e:
            out["error"] = f"{p.name} 的正文 XML 解析不了：{e}"
            return out

        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + f"\n\n……（正文太长，截到 {MAX_CHARS} 字）"
            out["truncated"] = True
        out["text"] = text

        if not with_images:
            out["ok"] = True
            return out

        # ── 图片 ──────────────────────────────────────────────
        # 去重：同一张图在文档里被引用两次只读一次（读一次要调一次接口）
        rels = _rels_map(zf)
        names: list[str] = []
        for rid in rids:
            name = rels.get(rid)
            if name and name not in names:
                names.append(name)

        media = [n for n in zf.namelist()
                 if n.startswith("word/media/")
                 and Path(n).suffix.lower() in IMAGE_SUFFIX]

        if not names:
            # 没有引用关系（有些工具生成的文档丢了 rels），退而求其次：
            # 媒体目录里有几张读几张，只是插不回原位了
            names = media

        if names:
            try:
                import vision as V
            except Exception as e:                     # noqa: BLE001
                out["error"] = f"（图片没读：vision 加载失败 {e}）"
                out["ok"] = True
                return out

            ok, why = V.available()
            if not ok:
                out["images"] = [{"name": n, "text": "", "note": why} for n in names]
            else:
                tmpdir = p.parent / "_docx_tmp"
                try:
                    tmpdir.mkdir(exist_ok=True)
                    for i, name in enumerate(names[:MAX_IMAGES], 1):
                        entry = {"name": name, "text": "", "note": ""}
                        try:
                            data = zf.read(name)
                        except KeyError:
                            entry["note"] = "压缩包里没有这个文件"
                            out["images"].append(entry)
                            continue
                        # 落到临时文件再交给 vision —— 它要的是磁盘上的路径，
                        # 而且会重新嗅一次文件头，这是它的安全闸门
                        tmp = tmpdir / f"{i:02d}{Path(name).suffix.lower()}"
                        try:
                            tmp.write_bytes(data)
                            entry["text"] = V.read(tmp, image_question or DEFAULT_IMAGE_QUESTION)
                        except OSError as e:
                            entry["note"] = f"写临时文件失败：{e}"
                        finally:
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                        out["images"].append(entry)
                finally:
                    try:
                        tmpdir.rmdir()
                    except OSError:
                        pass

            skipped = len(names) - MAX_IMAGES
            if skipped > 0:
                out["images"].append({
                    "name": f"（还有 {skipped} 张没读）", "text": "",
                    "note": f"一份文档最多读 {MAX_IMAGES} 张图，剩下的只列了名字"})

    out["ok"] = True
    return out


def as_prompt_block(path: str | Path, with_images: bool = True,
                    image_question: str = "") -> str:
    """
    给模型看的一段文本。fs_read / 对话窗附件都走这个。

    失败时返回的是**说明**，不是异常 —— 她照实说自己没读到，
    而不是编一份文档出来。
    """
    r = read(path, with_images=with_images, image_question=image_question)
    name = Path(path).name

    if not r["ok"]:
        return f"读不了 {name}：{r['error']}"

    parts = [f"【{name}】" if False else f"{name} 的正文："]

    if r["text"]:
        parts.append(r["text"])
    else:
        parts.append("（正文是空的，或者只有图片）")

    if r["images"]:
        parts.append("")
        parts.append(f"—— 文档里的 {len(r['images'])} 张图 ——")
        for i, im in enumerate(r["images"], 1):
            if im.get("text"):
                parts.append(f"[图{i}｜{Path(im['name']).name}]\n{im['text']}")
            elif im.get("note"):
                parts.append(f"[图{i}｜{Path(im['name']).name}]（没读：{im['note']}）")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
#  自检
# ═══════════════════════════════════════════════════════════════

_DOC = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<w:body>
  <w:p><w:r><w:t>会议纪要</w:t></w:r></w:p>
  <w:p><w:r><w:t>时间：</w:t></w:r><w:r><w:t>周五下午</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>地点：三号楼</w:t></w:r></w:p>
  <w:tbl>
    <w:tr><w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>负责人</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>结题报告</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>老张</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:p><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>
  <w:p><w:r><w:t>以上。</w:t></w:r></w:p>
</w:body></w:document>
"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId7" Type="http://x/image" Target="media/image1.png"/>
</Relationships>
"""


def make_test_docx(path: Path, with_image: bool = True) -> Path:
    """现造一份最小可用的 docx，用来跑自检。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _DOC)
        zf.writestr("word/_rels/document.xml.rels", _RELS)
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/content-types"><Default Extension="png" '
                    'ContentType="image/png"/></Types>')
        if with_image:
            try:
                from PIL import Image, ImageDraw, ImageFont
                img = Image.new("RGB", (520, 120), "white")
                d = ImageDraw.Draw(img)
                try:
                    f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 30)
                except OSError:
                    f = ImageFont.load_default()
                d.text((20, 20), "截止日期 5 月 20 日", fill="black", font=f)
                d.text((20, 66), "预算 三万二", fill="black", font=f)
                import io
                buf = io.BytesIO()
                img.save(buf, "PNG")
                zf.writestr("word/media/image1.png", buf.getvalue())
            except Exception:
                zf.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    return path


def selftest() -> int:
    import tempfile

    fails = 0

    def check(label: str, cond: bool, extra: str = ""):
        nonlocal fails
        print(f"  {'[OK]' if cond else '[!!]'} {label}" + (f"   {extra}" if extra else ""))
        if not cond:
            fails += 1

    print("docx 读取自检\n")

    tmp = Path(tempfile.mkdtemp())

    # 纯解析，不碰磁盘
    text, rids = extract_text(_DOC.encode("utf-8"))
    check("抽出正文", "会议纪要" in text, repr(text.splitlines()[0] if text else ""))
    check("同一段里分开的 run 会合并", "时间：周五下午" in text)
    check("制表符保留", "\t地点：三号楼" in text)
    check("表格按行抽出来", "项目 | 负责人" in text and "结题报告 | 老张" in text)
    check("★ 图片位置被记下来", rids == ["rId7"], str(rids))
    check("末尾那句还在（顺序没乱）", text.rstrip().endswith("以上。"))

    # rId → 文件名
    with zipfile.ZipFile(make_test_docx(tmp / "有图.docx")) as zf:
        rels = _rels_map(zf)
    check("rId 解析成 word/media/...", rels.get("rId7") == "word/media/image1.png", str(rels))

    # 整份读（不带图）
    r = read(tmp / "有图.docx", with_images=False)
    check("整份读成功", r["ok"] and "结题报告" in r["text"])
    check("关掉图片时不列图", r["images"] == [])

    # 整份读（带图）—— 没配视觉接口时应该给说明而不是崩
    r = read(tmp / "有图.docx")
    check("带图也能读完整份", r["ok"])
    if r["images"]:
        note = r["images"][0].get("note") or ""
        text_i = r["images"][0].get("text") or ""
        check("图片要么读出来了，要么说清楚为什么没读",
              bool(note) or bool(text_i),
              (text_i[:30] or note)[:50])

    # 各种坏输入
    check("不存在的文件给说明", not read(tmp / "没有这个.docx")["ok"])
    check("目录不当文档", not read(tmp)["ok"])

    fake_doc = tmp / "老的.doc"
    fake_doc.write_bytes(OLE_MAGIC + b"\x00" * 32)
    r = read(fake_doc)
    check("★ 老的 .doc 明确说是格式问题", "另存为 .docx" in r["error"], r["error"][:40])

    fake_txt = tmp / "假的.docx"
    fake_txt.write_text("我其实是文本", encoding="utf-8")
    check("改名的文本文件不会被当成文档", not read(fake_txt)["ok"])

    fake_zip = tmp / "空壳.docx"
    with zipfile.ZipFile(fake_zip, "w") as zf:
        zf.writestr("hello.txt", "hi")
    check("是 zip 但没有 document.xml → 说清楚", "不是 Word 文档" in read(fake_zip)["error"])

    block = as_prompt_block(tmp / "有图.docx", with_images=False)
    check("as_prompt_block 打出正文", "会议纪要" in block and "结题报告" in block)

    print()
    print("全部通过" if not fails else f"{fails} 项未通过")
    return fails


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "selftest":
        sys.exit(selftest())
    if args[0] in ("看", "read") and len(args) > 1:
        print(as_prompt_block(args[1]))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
