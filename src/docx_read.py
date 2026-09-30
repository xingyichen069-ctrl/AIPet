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
import tempfile
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

# 图片：每张不同图片只调用一次视觉接口。超过上限时整份拒绝，不静默漏图。
# 这个数字是"一份文档里真正需要读的图"的合理上限 —— 一整本扫描件
# 不该走这条路，那种该先转成文本。
MAX_IMAGES = 12
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 4096

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

def _text(blocks: list[dict], text: str) -> None:
    if text:
        if blocks and blocks[-1]["kind"] == "text":
            blocks[-1]["text"] += text
        else:
            blocks.append({"kind": "text", "text": text})


def _inline(node, blocks: list[dict]) -> None:
    name = _local(node.tag)
    if name == "t":
        _text(blocks, node.text or "")
    elif name == "tab":
        _text(blocks, "\t")
    elif name in ("br", "cr"):
        _text(blocks, "\n")
    elif name in ("blip", "imagedata"):
        rid = node.get(f"{R}embed") or node.get(f"{R}link") or node.get(f"{R}id")
        if rid:
            blocks.append({"kind": "image", "rid": rid})
    elif name == "AlternateContent":
        # Word stores a second representation as a fallback, not a second image.
        choice = next((c for c in node if _local(c.tag) == "Choice"), None)
        if choice is None:
            choice = next((c for c in node if _local(c.tag) == "Fallback"), None)
        if choice is not None:
            _inline(choice, blocks)
    else:
        for child in node:
            _inline(child, blocks)


def _walk_blocks(node, blocks: list[dict]) -> None:
    for child in node:
        name = _local(child.tag)
        if name == "p":
            _inline(child, blocks)
            _text(blocks, "\n")
        elif name == "tbl":
            for row in child.findall(f"{W}tr"):
                for index, cell in enumerate(row.findall(f"{W}tc")):
                    if index:
                        _text(blocks, " | ")
                    cell_blocks = []
                    _walk_blocks(cell, cell_blocks)
                    if cell_blocks and cell_blocks[-1]["kind"] == "text":
                        cell_blocks[-1]["text"] = cell_blocks[-1]["text"].rstrip("\n")
                    for block in cell_blocks:
                        if block["kind"] == "text":
                            _text(blocks, block["text"])
                        else:
                            blocks.append(block)
                _text(blocks, "\n")
            _text(blocks, "\n")
        elif name in ("sdt", "sdtContent", "body", "txbxContent"):
            _walk_blocks(child, blocks)


def extract_blocks(document_xml: bytes) -> list[dict]:
    root = ET.fromstring(document_xml)
    body = root.find(f"{W}body")
    blocks: list[dict] = []
    if body is not None:
        _walk_blocks(body, blocks)
    return blocks


def extract_text(document_xml: bytes) -> tuple[str, list[str]]:
    """Compatibility extraction view; ordered blocks retain image positions."""
    blocks = extract_blocks(document_xml)
    text = "".join(b["text"] for b in blocks if b["kind"] == "text")
    return re.sub(r"\n{3,}", "\n\n", text).strip(), [b["rid"] for b in blocks if b["kind"] == "image"]


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
        if not rid or not target or rel.get("TargetMode", "").lower() == "external":
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


def _check_zip(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ValueError("文档内部文件过多，请拆分后再读。")
    if sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
        raise ValueError("文档解压后超过64 MB，请缩小或拆分后再读。")
    seen = set()
    for info in infos:
        name = info.filename.casefold()
        if name in seen:
            raise ValueError("文档包含重复的内部文件，无法可靠读取。")
        seen.add(name)
        if info.flag_bits & 1:
            raise ValueError("暂不支持加密的Word文档，请另存为未加密副本。")
        if name.endswith((".xml", ".rels")) and info.file_size > MAX_XML_BYTES:
            raise ValueError("文档的XML结构超过4 MB，请拆分文档。")


def _compose(result: dict) -> str:
    pictures = {im["name"]: (i, im) for i, im in enumerate(result["images"], 1)}
    parts = []
    for block in result["blocks"]:
        if block["kind"] == "text":
            parts.append(block["text"])
        elif block.get("name") in pictures:
            number, image = pictures[block["name"]]
            parts.append(f"\n[图{number}｜{Path(image['name']).name}]\n{image['text']}\n")
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def read(path: str | Path, with_images: bool = True,
         image_question: str = "", *, cancelled=None,
         max_chars: int = MAX_CHARS) -> dict:
    """Read once, retaining order. Any required image failure fails the file.

    `with_images=False` is an explicit text-only developer operation. Desktop
    attachments always require every referenced image to be read successfully.
    No member is extracted using its archive pathname.
    """
    p = Path(path)
    stopped = cancelled or (lambda: False)
    out = {"ok": False, "text": "", "images": [], "blocks": [], "error": "",
           "truncated": False, "partial": False, "cancelled": False}

    def fail(reason, *, was_cancelled=False):
        out.update(ok=False, error=reason, partial=bool(out["text"] or out["images"]),
                   cancelled=was_cancelled)
        return out

    if stopped():
        return fail("已取消读取。", was_cancelled=True)
    if not p.is_file():
        return fail("文件不存在或不是普通文件。")
    try:
        if p.stat().st_size > MAX_ARCHIVE_BYTES:
            return fail("文档超过32 MB，请先压缩图片或拆分文档。")
        with p.open("rb") as handle:
            head = handle.read(8)
        if head.startswith(OLE_MAGIC):
            return fail(f"{p.name} 是老的 .doc 格式，请用Word另存为 .docx。")
        if not head.startswith(DOCX_MAGIC):
            return fail(f"{p.name} 不是一个docx（文件头对不上）。")
        with zipfile.ZipFile(p) as zf:
            _check_zip(zf)
            if "word/document.xml" not in zf.namelist():
                return fail(f"{p.name} 没有 word/document.xml，不是 Word 文档。")
            blocks = extract_blocks(zf.read("word/document.xml"))
            out["blocks"] = blocks
            out["text"] = "".join(b["text"] for b in blocks if b["kind"] == "text").strip()
            if len(out["text"]) > max_chars:
                out["truncated"] = True
                return fail(f"正文超过 {max_chars:,} 字，无法完整作为材料，请拆分文档。")
            if not with_images:
                out["ok"] = True
                return out
            rels = _rels_map(zf)
            names = []
            for block in blocks:
                if block["kind"] != "image":
                    continue
                name = rels.get(block["rid"])
                if not name:
                    return fail("文档包含缺失或外链图片，无法完整读取；请将图片嵌入文档后重试。")
                block["name"] = name
                if name not in names:
                    names.append(name)
            if not names:
                # Some exporters omit relationships. Preserve an explicit
                # positional limitation instead of inventing an image location.
                names = [n for n in zf.namelist() if n.startswith("word/media/")
                         and Path(n).suffix.lower() in IMAGE_SUFFIX]
                if names:
                    _text(blocks, "\n（以下图片未提供文内位置，按文件顺序列出。）\n")
                    blocks.extend({"kind": "image", "name": name} for name in names)
            if len(names) > MAX_IMAGES:
                return fail(f"文档含 {len(names)} 张不同图片，超过一次 {MAX_IMAGES} 张上限，请拆分。")
            if names:
                import vision as V
                ok, why = V.available()
                if not ok:
                    return fail(f"文档中的图片无法识别：{why}。整份材料未添加。")
                # Preflight all members before sending any image to a provider.
                for name in names:
                    try:
                        info = zf.getinfo(name)
                    except KeyError:
                        return fail(f"文档引用的图片缺失：{Path(name).name}。整份材料未添加。")
                    if info.file_size > MAX_IMAGE_BYTES or Path(name).suffix.lower() not in V.MIME:
                        return fail(f"图片 {Path(name).name} 超过8 MB或格式不支持，请转为PNG/JPG等支持格式。")
                cache = Path(__file__).resolve().parents[1] / "data/cache/docx"
                cache.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="read-", dir=cache) as temporary:
                    for index, name in enumerate(names, 1):
                        if stopped():
                            return fail("已取消读取。", was_cancelled=True)
                        image = Path(temporary) / f"{index:02d}{Path(name).suffix.lower()}"
                        image.write_bytes(zf.read(name))
                        result = V.read_result(image, image_question or DEFAULT_IMAGE_QUESTION,
                                               cancelled=stopped)
                        if stopped() or result.get("cancelled"):
                            return fail("已取消读取。", was_cancelled=True)
                        if not result["ok"]:
                            return fail(f"图片 {index}（{Path(name).name}）读取失败：{result['error']} 整份材料未添加。")
                        out["images"].append({"name": name, "text": result["text"], "note": ""})
            rendered = _compose(out)
            if len(rendered) > max_chars:
                out["truncated"] = True
                return fail(f"正文与识别结果超过 {max_chars:,} 字，无法完整作为材料，请拆分文档。")
            if stopped():
                return fail("已取消读取。", was_cancelled=True)
            out["ok"] = True
            return out
    except ValueError as exc:
        return fail(str(exc))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError,
            RuntimeError, NotImplementedError) as exc:
        return fail(f"文档未读取完成：{type(exc).__name__}。请检查文件是否损坏、受限或正在被修改。")


def format_result(result: dict, name: str) -> str:
    """Format an existing result without another parse or paid image request."""
    if not result["ok"]:
        return f"读不了 {name}：{result['error']}"
    body = _compose(result)
    return f"{name} 的正文：\n{body}" if body else ""


def as_prompt_block(path: str | Path, with_images: bool = True,
                    image_question: str = "") -> str:
    return format_result(read(path, with_images=with_images, image_question=image_question),
                         Path(path).name)


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

    # 带图必须完整成功；未配置视觉时整份失败，不把部分内容当成功。
    r = read(tmp / "有图.docx")
    check("带图成功或明确说明整份失败", r["ok"] or bool(r["error"]))
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
