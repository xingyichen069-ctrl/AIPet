#!/usr/bin/env python3
"""视频读取、字幕/OCR 和评论的轻量适配层。

基础能力只依赖系统的 ffprobe/ffmpeg/tesseract；语音转写、平台评论分别
在安装 faster-whisper/whisper 或 yt-dlp 时启用。所有增强项都是可选的，
因此桌宠在干净环境里仍能启动。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".ts"}
SUBTITLE_SUFFIXES = {".srt", ".vtt", ".ass", ".ssa", ".txt"}


def _run(args: list[str], timeout: int = 30) -> tuple[str, str, int]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout or "", p.stderr or "", p.returncode
    except (OSError, subprocess.SubprocessError) as e:
        return "", str(e), 127


def _probe(path: Path) -> dict:
    out, err, code = _run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                           "-of", "json", str(path)], timeout=20)
    if code != 0:
        return {"error": err.strip() or "ffprobe 不可用"}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": "ffprobe 输出无法解析"}


def _parse_subtitle(text: str, limit: int = 12000) -> str:
    text = re.sub(r"\r", "", text)
    text = re.sub(r"(?im)^\s*(?:WEBVTT|\d+|\d{2}:\d{2}:\d{2}[^\n]*)\s*$", "", text)
    text = re.sub(r"(?m)^\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->.*$", "", text)
    text = re.sub(r"(?m)^\s*\d{2}:\d{2}[,.]\d{3}\s+-->.*$", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)[:limit]


def _sidecar_subtitles(path: Path) -> str:
    for suffix in SUBTITLE_SUFFIXES:
        p = path.with_suffix(suffix)
        if p.exists() and p.is_file():
            try:
                return _parse_subtitle(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return ""


def _ocr_frames(path: Path, frames: int = 4) -> str:
    if not shutil.which("ffmpeg") or not shutil.which("tesseract"):
        return ""
    with tempfile.TemporaryDirectory(prefix="aipet-video-") as td:
        pattern = str(Path(td) / "frame-%02d.jpg")
        # fps 取少量代表帧，避免长视频把 CPU 和上下文都吃满。
        _, err, code = _run(["ffmpeg", "-v", "error", "-i", str(path),
                             "-vf", f"fps=1/15,scale=1280:-1", "-frames:v", str(max(1, frames)),
                             "-q:v", "4", pattern], timeout=90)
        if code != 0:
            return ""
        chunks = []
        for frame in sorted(Path(td).glob("frame-*.jpg")):
            out, _, rc = _run(["tesseract", str(frame), "stdout", "-l", "eng+chi_sim"], timeout=20)
            if rc != 0:
                out, _, rc = _run(["tesseract", str(frame), "stdout"], timeout=20)
            if out.strip():
                chunks.append(out.strip())
        return "\n".join(dict.fromkeys(chunks))[:12000]


def _transcribe(path: Path, max_seconds: int = 600) -> tuple[str, str]:
    """可选 faster-whisper/whisper；没有安装时返回可解释状态。"""
    try:
        from faster_whisper import WhisperModel  # type: ignore
        engine = "faster-whisper"
        model_name = os.environ.get("AIPET_WHISPER_MODEL", "base")
        model = WhisperModel(model_name, device="auto", compute_type="int8")
        segments, _ = model.transcribe(str(path), vad_filter=True)
        text = " ".join(s.text.strip() for s in segments if s.text.strip())
        return text[:20000], engine
    except ImportError:
        pass
    except Exception as e:
        return "", f"语音转写失败：{type(e).__name__}: {e}"
    try:
        import whisper  # type: ignore
        model = whisper.load_model(os.environ.get("AIPET_WHISPER_MODEL", "base"))
        result = model.transcribe(str(path), fp16=False)
        return (result.get("text") or "")[:20000], "openai-whisper"
    except ImportError:
        return "", "未安装语音转写依赖（可选 faster-whisper 或 openai-whisper）"
    except Exception as e:
        return "", f"语音转写失败：{type(e).__name__}: {e}"


def inspect(path: str | Path, ocr: bool = True, transcribe: bool = True,
            max_frames: int = 4) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {"ok": False, "error": f"没有这个文件：{p}"}
    if not p.is_file():
        return {"ok": False, "error": "路径不是文件"}
    if p.suffix.lower() not in VIDEO_SUFFIXES:
        return {"ok": False, "error": f"暂不支持的视频格式：{p.suffix or '无扩展名'}"}
    meta = _probe(p)
    streams = meta.get("streams") or []
    fmt = meta.get("format") or {}
    result = {
        "ok": "error" not in meta, "path": str(p),
        "format": fmt.get("format_name", ""),
        "duration": float(fmt.get("duration", 0) or 0),
        "size": int(fmt.get("size", p.stat().st_size) or 0),
        "streams": [{"type": s.get("codec_type"), "codec": s.get("codec_name"),
                     "language": (s.get("tags") or {}).get("language"),
                     "width": s.get("width"), "height": s.get("height")}
                    for s in streams],
        "text": {}, "warnings": [],
    }
    if "error" in meta:
        result["warnings"].append(meta["error"])
        return result
    subtitle = _sidecar_subtitles(p)
    if subtitle:
        result["text"]["subtitles"] = subtitle
    elif any(s.get("codec_type") == "subtitle" for s in streams):
        result["warnings"].append("检测到内嵌字幕；如需读取请先导出为同名 .srt/.vtt 文件")
    if ocr:
        text = _ocr_frames(p, max_frames)
        if text:
            result["text"]["ocr"] = text
        elif shutil.which("tesseract") is None:
            result["warnings"].append("未找到 tesseract，跳过画面文字识别")
    if transcribe and any(s.get("codec_type") == "audio" for s in streams):
        text, engine = _transcribe(p)
        if text:
            result["text"]["transcript"] = text
            result["text"]["transcript_engine"] = engine
        elif engine:
            result["warnings"].append(engine)
    return result


def _normal_comment(row: dict, source: str = "") -> dict:
    member = row.get("member") or {}
    user = row.get("user") or {}
    def integer(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    return {"text": str(row.get("text") or row.get("content") or row.get("message") or "").strip(),
            "author": str(row.get("author") or member.get("uname") or user.get("nickname") or ""),
            "like_count": integer(row.get("like_count", row.get("like", row.get("vote", 0)))),
            "reply_count": integer(row.get("reply_count", row.get("rcount", 0))),
            "published_at": row.get("published_at") or row.get("ctime") or row.get("time") or "",
            "source": source}


def _bilibili_comments(url: str, limit: int) -> tuple[list[dict], list[dict]]:
    m = re.search(r"(?:video/)?(BV[0-9A-Za-z]+)", url)
    if not m:
        return [], []
    base = "https://api.bilibili.com/x/web-interface/view?bvid=" + urllib.parse.quote(m.group(1))
    req = urllib.request.Request(base, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            aid = json.loads(r.read().decode("utf-8")).get("data", {}).get("aid")
        if not aid:
            return [], []
        def get(sort: int) -> list[dict]:
            u = f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort={sort}&ps={max(1, limit)}"
            rr = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(rr, timeout=15) as x:
                rows = json.loads(x.read().decode("utf-8")).get("data", {}).get("replies") or []
            return [_normal_comment({"text": z.get("content", {}).get("message"),
                                     "author": z.get("member", {}).get("uname"),
                                     "like": z.get("like"), "reply_count": z.get("rcount"),
                                     "ctime": z.get("ctime")}, "bilibili") for z in rows]
        return get(2), get(0)
    except Exception:
        return [], []


def comments(source: str | Path, hot_limit: int = 5, time_limit: int = 5) -> dict:
    """获取热度和时间两组评论；本地优先读取同名 .comments.json。"""
    value = str(source)
    if not value.strip():
        return {"ok": False, "hot": [], "time": [], "error": "缺少视频路径或 URL"}
    hot: list[dict] = []
    recent: list[dict] = []
    p = Path(value).expanduser()
    sidecar = (p.with_suffix(p.suffix + ".comments.json")
               if p.exists() and p.name else None)
    if sidecar and sidecar.exists():
        try:
            rows = json.loads(sidecar.read_text(encoding="utf-8"))
            rows = rows if isinstance(rows, list) else rows.get("comments", [])
            normalized = [_normal_comment(x, "sidecar") for x in rows if isinstance(x, dict)]
            hot = sorted(normalized, key=lambda x: (x["like_count"], x["reply_count"]), reverse=True)[:hot_limit]
            recent = sorted(normalized, key=lambda x: str(x["published_at"]), reverse=True)[:time_limit]
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    elif value.startswith(("http://", "https://")) and "bilibili.com" in value:
        hot, recent = _bilibili_comments(value, max(hot_limit, time_limit))
        hot, recent = hot[:hot_limit], recent[:time_limit]
    else:
        try:
            import yt_dlp  # type: ignore
            opts = {"quiet": True, "skip_download": True, "getcomments": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(value, download=False)
            rows = [_normal_comment(x, "yt-dlp") for x in (info.get("comments") or [])]
            hot = sorted(rows, key=lambda x: (x["like_count"], x["reply_count"]), reverse=True)[:hot_limit]
            recent = sorted(rows, key=lambda x: str(x["published_at"]), reverse=True)[:time_limit]
        except ImportError:
            # 用户常见的是只安装了 yt-dlp 命令行而没有把模块装进桌宠虚拟环境。
            exe = shutil.which("yt-dlp")
            if not exe:
                return {"ok": False, "hot": [], "time": [], "error": "未读取到评论：请确认 URL 可访问或安装 yt-dlp"}
            try:
                p = subprocess.run([exe, "--dump-single-json", "--no-download",
                                    "--no-warnings", "--write-comments", value],
                                   capture_output=True, text=True, timeout=120)
                info = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else {}
                rows = [_normal_comment(x, "yt-dlp") for x in (info.get("comments") or [])]
                hot = sorted(rows, key=lambda x: (x["like_count"], x["reply_count"]), reverse=True)[:hot_limit]
                recent = sorted(rows, key=lambda x: str(x["published_at"]), reverse=True)[:time_limit]
            except Exception as e:
                return {"ok": False, "hot": [], "time": [], "error": f"yt-dlp 评论读取失败：{type(e).__name__}: {e}"}
        except Exception as e:
            return {"ok": False, "hot": [], "time": [], "error": f"评论读取失败：{type(e).__name__}: {e}"}
    return {"ok": bool(hot or recent), "hot": hot, "time": recent,
            "counts": {"hot": len(hot), "time": len(recent)}}
