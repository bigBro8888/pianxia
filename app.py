"""片匣 — local media fetch UI backed by yt-dlp."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / "downloads"
STATIC = ROOT / "static"
DOWNLOADS.mkdir(exist_ok=True)

app = FastAPI(title="片匣", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

Mode = Literal["video", "audio"]
Quality = Literal["best", "2160", "1440", "1080", "720", "480", "360"]

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


class ProbeRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    mode: Mode = "video"
    quality: Quality = "1080"
    filename_style: Literal["title", "id"] = "title"


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (cleaned or "media")[:120]


def _format_duration(seconds: Any) -> str | None:
    if seconds is None:
        return None
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _base_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 8,
        "fragment_retries": 8,
        "retry_sleep_functions": {"http": lambda n: min(2 ** n, 12)},
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"],
            }
        },
    }


def _probe_sync(url: str) -> dict[str, Any]:
    opts = {**_base_opts(), "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("无法解析该链接")
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise RuntimeError("播放列表为空")
        info = entries[0]

    thumb = None
    thumbs = info.get("thumbnails") or []
    if thumbs:
        thumb = thumbs[-1].get("url")
    thumb = thumb or info.get("thumbnail")

    return {
        "id": info.get("id"),
        "title": info.get("title") or "未命名",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": _format_duration(info.get("duration")),
        "duration_seconds": info.get("duration"),
        "thumbnail": thumb,
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor"),
    }


def _build_ydl_opts(
    *,
    mode: Mode,
    quality: Quality,
    outtmpl: str,
    job_id: str,
) -> dict[str, Any]:
    def hook(d: dict[str, Any]) -> None:
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                pct = (downloaded / total * 100) if total else 0
                job.update(
                    {
                        "state": "downloading",
                        "percent": round(pct, 1),
                        "speed": d.get("speed"),
                        "eta": d.get("eta"),
                        "message": "下载中",
                    }
                )
            elif status == "finished":
                job.update(
                    {
                        "state": "processing",
                        "percent": 99,
                        "message": "正在处理（合并/转码）",
                        "temp_file": d.get("filename"),
                    }
                )

    opts: dict[str, Any] = {
        **_base_opts(),
        "outtmpl": outtmpl,
        "progress_hooks": [hook],
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10 * 1024 * 1024,
    }

    if mode == "audio":
        opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    else:
        if quality == "best":
            fmt = "bv*+ba/b"
        else:
            h = int(quality)
            fmt = (
                f"bv*[height<={h}]+ba/"
                f"b[height<={h}]/"
                f"bv*+ba/b"
            )
        opts.update(
            {
                "format": fmt,
                "merge_output_format": "mp4",
            }
        )
    return opts


def _download_sync(job_id: str, url: str, mode: Mode, quality: Quality) -> None:
    with jobs_lock:
        jobs[job_id].update({"state": "starting", "percent": 0, "message": "准备下载"})

    outtmpl = str(DOWNLOADS / f"{job_id}.%(ext)s")
    opts = _build_ydl_opts(mode=mode, quality=quality, outtmpl=outtmpl, job_id=job_id)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get("_type") == "playlist":
                entries = info.get("entries") or []
                info = entries[0] if entries else info
            requested = ydl.prepare_filename(info) if info else None

        # After audio extract, extension may change to mp3
        candidates = list(DOWNLOADS.glob(f"{job_id}.*"))
        # Prefer final media over part files
        candidates = [p for p in candidates if p.suffix.lower() not in {".part", ".ytdl", ".temp"}]
        if not candidates and requested:
            path = Path(requested)
            if path.exists():
                candidates = [path]
        if not candidates:
            raise RuntimeError("下载完成但未找到文件")

        path = max(candidates, key=lambda p: p.stat().st_mtime)
        title = (info or {}).get("title") or "media"
        nice = _safe_name(title) + path.suffix
        final = DOWNLOADS / f"{job_id}_{nice}"
        if final.exists():
            final.unlink()
        path.rename(final)

        with jobs_lock:
            jobs[job_id].update(
                {
                    "state": "done",
                    "percent": 100,
                    "message": "完成",
                    "filename": final.name,
                    "title": title,
                    "download_url": f"/api/file/{job_id}",
                }
            )
    except Exception as exc:  # noqa: BLE001 — surface to UI
        with jobs_lock:
            jobs[job_id].update(
                {
                    "state": "error",
                    "percent": 0,
                    "message": str(exc)[:400],
                }
            )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/api/probe")
async def probe(body: ProbeRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_probe_sync, str(body.url))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)[:400]) from exc


@app.post("/api/download")
async def start_download(body: DownloadRequest) -> dict[str, str]:
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "state": "queued",
            "percent": 0,
            "message": "排队中",
            "url": str(body.url),
            "mode": body.mode,
            "quality": body.quality,
        }

    thread = threading.Thread(
        target=_download_sync,
        args=(job_id, str(body.url), body.mode, body.quality),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.get("/api/progress/{job_id}")
async def progress(job_id: str) -> StreamingResponse:
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_stream():
        last = None
        while True:
            with jobs_lock:
                payload = dict(jobs.get(job_id) or {})
            # drop non-serializable / internal keys if any
            payload.pop("temp_file", None)
            encoded = json.dumps(payload, ensure_ascii=False)
            if encoded != last:
                yield f"data: {encoded}\n\n"
                last = encoded
            if payload.get("state") in {"done", "error"}:
                break
            await asyncio.sleep(0.35)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/file/{job_id}")
async def get_file(job_id: str) -> FileResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("state") != "done":
            raise HTTPException(status_code=404, detail="文件未就绪")
        name = job.get("filename")
    if not name:
        raise HTTPException(status_code=404, detail="文件缺失")
    path = DOWNLOADS / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已被清理")
    return FileResponse(
        path,
        filename=name.split("_", 1)[-1] if "_" in name else name,
        media_type="application/octet-stream",
    )


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "片匣"}
