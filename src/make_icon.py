#!/usr/bin/env python3
"""
make_icon.py —— 从角色图生成 Windows 图标 (.ico)

Windows 图标不是一个尺寸，是**一包多个尺寸**：
任务栏用 32、桌面用 48、文件管理器小图标用 16、Alt+Tab 用 256。
只塞一张图进去，系统缩放出来的会糊。

这个脚本生成 8 个尺寸打包成一个 .ico。

有个坑值得说：ICO 里的尺寸字段是个 **BYTE**，最大只能表示 255，
所以 256 要写成 0（这是 ICO 格式的约定，不是 bug）。
不处理这个的话 256 那一档会写坏。

用法：
    python src/make_icon.py                    # 从 assets/character.png 生成
    python src/make_icon.py 某个图.png          # 指定源图
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
SIZES = [16, 24, 32, 48, 64, 128, 256]


def _render_pngs(src: Path) -> list[tuple[int, bytes]]:
    """把源图缩成各个尺寸，各自编码为 PNG 字节。"""
    from PySide6.QtCore import QBuffer, QByteArray, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter

    # Qt 的绘图/编码需要有个 application 实例。没有它会直接段错误，
    # 而不是抛异常 —— 这种崩法很难查，所以显式建一个。
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    _ = app

    img = QImage(str(src))
    if img.isNull():
        raise RuntimeError(f"读不了这张图：{src}")

    out = []
    for size in SIZES:
        scaled = QImage(size, size, QImage.Format_ARGB32)
        scaled.fill(Qt.transparent)
        p = QPainter(scaled)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        # 保持宽高比，居中放置 —— 源图不是正方形时不会被拉扁
        s = img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawImage((size - s.width()) // 2, (size - s.height()) // 2, s)
        p.end()

        # QBuffer 保存的是 QByteArray 的**指针**。写成 QBuffer(QByteArray())
        # 的话那个临时对象立刻被回收，指针就悬空了 —— 会段错误。
        # 必须显式持有引用。
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.WriteOnly)
        scaled.save(buf, "PNG")
        buf.close()
        out.append((size, bytes(ba)))
    return out


def build(src: Path, dest: Path) -> Path:
    pngs = _render_pngs(src)

    # ICONDIR: 保留(2) + 类型(2, 1=图标) + 数量(2)
    header = struct.pack("<HHH", 0, 1, len(pngs))

    # 数据区从所有目录项之后开始
    offset = len(header) + 16 * len(pngs)
    entries, blobs = [], []
    for size, data in pngs:
        # 宽高是 BYTE，256 写 0（ICO 格式约定）
        b = 0 if size >= 256 else size
        entries.append(struct.pack(
            "<BBBBHHII",
            b, b,        # 宽、高
            0,           # 调色板数（PNG 模式填 0）
            0,           # 保留
            1,           # 色彩平面
            32,          # 位深
            len(data),   # 该图数据长度
            offset,      # 该图数据偏移
        ))
        blobs.append(data)
        offset += len(data)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(header + b"".join(entries) + b"".join(blobs))
    return dest


def _render_model_png(model: Path, out_png: Path,
                      w: int = 600, h: int = 840) -> bool:
    """
    渲染一帧 Live2D 模型，存成带透明通道的 PNG。

    宽高比按 Hiyori 的画布（1 : 1.403）来，这样模型不会被拉变形。
    """
    try:
        sys.path.insert(0, str(ROOT / "src"))
        # 必须先把 GL 格式设成兼容模式，否则 live2d 什么都不画
        from live2d_widget import make_transparent_gl, HAS_LIVE2D
        if not HAS_LIVE2D:
            print("live2d-py 不可用")
            return False

        make_transparent_gl()
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer
        from live2d_widget import Live2DWidget

        app = QApplication.instance() or QApplication(sys.argv[:1])
        gl = Live2DWidget(model, None, zoom=1.0)
        gl.resize(w, h)
        gl.show()

        done = {"ok": False}

        def shoot():
            img = gl.grabFramebuffer()
            if not img.isNull():
                img.save(str(out_png), "PNG")
                done["ok"] = True
            gl.stop()
            app.quit()

        QTimer.singleShot(2200, shoot)
        app.exec()
        return done["ok"]
    except Exception as e:                           # noqa: BLE001
        print(f"渲染 Live2D 失败：{e}")
        return False


def head_crop(src: Path, dest: Path, head_ratio: float = 0.30,
              pad: float = 0.08):
    """
    从全身像里裁出「大头」，做成正方形。

    定位方法（不猜画布坐标，换模型也能用）：

      1. 求整张图不透明像素的包围盒 —— 去掉模型在画布里的留白
      2. 取包围盒**最上面 head_ratio 那一段**，认为那就是头部
      3. **用这一段自己的横向包围盒算中心** —— 这一步很关键：
         用整图的中心会被辫子宽度和身体带偏，脸跑到画面边上去
      4. 以头部中心为基准裁正方形，四边留 pad 的余量

    head_ratio 越小框得越紧：0.30 是头肩特写，0.46 会带到腰。
    """
    from PIL import Image

    im = Image.open(src).convert("RGBA")
    bbox = im.getbbox()
    if not bbox:
        raise RuntimeError("整张图都是透明的，模型没渲染出来")

    x0, y0, x1, y1 = bbox
    bh = y1 - y0
    head_h = max(8, int(bh * head_ratio))

    # ★ 只在上部这一段里重新求横向范围，避开辫子和身体的干扰
    head_band = im.crop((x0, y0, x1, min(y1, y0 + head_h)))
    hb = head_band.getbbox() or (0, 0, head_band.width, head_band.height)
    head_cx = x0 + (hb[0] + hb[2]) // 2

    # 正方形边长 = 头高 + 两侧余量
    side = int(head_h * (1 + pad * 2))
    left = head_cx - side // 2
    top = y0 - int(head_h * pad)

    # 贴边时平移而不是裁掉
    left = max(0, min(left, im.width - side)) if im.width > side else 0
    top = max(0, min(top, im.height - side)) if im.height > side else 0
    side = min(side, im.width - left, im.height - top)

    crop = im.crop((left, top, left + side, top + side))
    crop = crop.resize((512, 512), Image.LANCZOS)
    crop.save(dest, "PNG")
    return im.size, (left, top, side)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    use_l2d = "--live2d" in sys.argv
    full_body = "--full" in sys.argv          # 不加 --full 就默认裁大头
    ratio = 0.30
    for a in sys.argv:
        if a.startswith("--head="):
            ratio = float(a.split("=", 1)[1])

    tmp_full = ROOT / "assets" / "_full.png"
    tmp_head = ROOT / "assets" / "_head.png"

    if use_l2d:
        print("正在渲染 Live2D 模型…")
        sys.path.insert(0, str(ROOT / "src"))
        try:
            import memory as _M
            model = _M.ROOT / _M.CFG.get("live2d", {}).get("model", "")
        except Exception:                            # noqa: BLE001
            model = None

        if not model or not model.exists():
            print(f"模型不存在：{model}")
            use_l2d = False
        elif not _render_model_png(model, tmp_full):
            print("渲染失败，改用静态图")
            use_l2d = False

    if use_l2d:
        if full_body:
            src = tmp_full
        else:
            print(f"裁切头部（取上部 {ratio:.0%}）…")
            try:
                size, box = head_crop(tmp_full, tmp_head, ratio)
                print(f"  原图 {size[0]}x{size[1]} → 裁切框 {box[2]}px @ ({box[0]},{box[1]})")
                src = tmp_head
            except Exception as e:                   # noqa: BLE001
                print(f"  裁切失败（{e}），用全身图")
                src = tmp_full

    if not use_l2d:
        src = Path(args[0]) if args else ROOT / "assets" / "character.png"
        if not src.exists():
            print(f"找不到源图：{src}")
            print("先运行 python src/pet.py 生成占位形象，或指定一张自己的 PNG。")
            sys.exit(1)

    dest = ROOT / "assets" / "character.ico"
    build(src, dest)

    kb = dest.stat().st_size / 1024
    print(f"已生成：{dest}")
    print(f"  源图：{src.name}")
    print(f"  尺寸：{' / '.join(str(s) for s in SIZES)}")
    print(f"  体积：{kb:.1f} KB")

    # 临时文件用完就删
    for f in (tmp_full, tmp_head):
        try:
            f.unlink()
        except OSError:
            pass

    print()
    print("用法：右键桌面快捷方式 → 属性 → 更改图标 → 浏览到这个文件")
    print("     （任务栏图标要重启资源管理器才刷新，Windows 的缓存机制）")
    print()
    print("选项：")
    print("  --live2d          从 Live2D 模型渲染（默认裁大头）")
    print("  --head=0.40       调整裁切比例，越小框得越紧")
    print("  --full            不裁切，用全身")


if __name__ == "__main__":
    main()
