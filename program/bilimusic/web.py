"""Web 前端后端：纯标准库 HTTP 服务，无第三方依赖、无需 npm 构建。

接口一览：
    GET  /                          -> 页面
    GET  /static/*                  -> 静态资源
    GET  /api/health                -> 环境状态（ffmpeg / yt-dlp / 输出目录）
    POST /api/search   {keyword}    -> B 站搜索结果
    POST /api/cookies  {browser}    -> 检查浏览器 Cookie 是否真的可用
    POST /api/download {result,...} -> 创建下载任务，返回 job_id
    POST /api/upload   multipart    -> 上传本地音频，返回 upload_id
    POST /api/process  {...}        -> 处理上传的音频 / 直接给出本地路径
    POST /api/local-path {...}      -> 用服务器上已有的音频文件路径处理
    GET  /api/job/<id>              -> 任务状态 + 日志 + 产物
    POST /api/job/<id>/cancel       -> 取消任务
    GET  /api/artifacts             -> 最近完成的任务产物
    GET  /api/file?job=&kind=&dl=   -> 取产物（支持 Range，便于试听拖动）
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .audio_processor import AudioError
from .bili import BiliError, SearchResult, search_bilibili
from .config import (
    DEFAULT_OUTPUT_ROOT,
    JobCancelled,
    PROJECT_ROOT,
    ensure_dirs,
    find_ffmpeg,
    sanitize_filename,
    which,
)
from .pipeline import ProcessedTrack, process_audio_file, process_download

WEB_DIR = PROJECT_ROOT / "web"
UPLOAD_DIR = PROJECT_ROOT / "output" / "_uploads"
JOB_TMP_DIR = PROJECT_ROOT / "output" / "_tmp"

# 上传文件大小上限（500 MB）
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

# 重活串行化：yt-dlp / ffmpeg 都很吃资源，并发跑反而更慢且更容易被风控
_HEAVY_LOCK = threading.Semaphore(1)


# --------------------------------------------------------------------------- #
# 任务模型
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    id: str
    kind: str  # "download" | "file"
    title: str = ""
    status: str = "queued"  # queued | running | done | error | cancelled
    stage: str = "排队中"
    progress: float = 0.0
    log: list[str] = field(default_factory=list)
    error: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    artifacts: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    work_dir: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def add_log(self, message: str) -> None:
        if not message:
            return
        self.log.append(message)
        # 日志无限增长没必要，保留最近 400 条
        if len(self.log) > 400:
            del self.log[: len(self.log) - 400]

    def to_dict(self, include_log: bool = True) -> dict:
        data = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "error": self.error,
            "created": self.created,
            "finished": self.finished,
            "artifacts": self.artifacts,
            "artifacts_list": self.artifact_list(),
            "summary": self.summary,
            "work_dir": self.work_dir,
        }
        if include_log:
            data["log"] = self.log[-120:]
        return data

    def artifact_list(self, names: list[str] | None = None) -> list[dict]:
        """把产物整理成前端可用的结构（带下载/试听链接）。"""
        out = []
        for item in self.artifacts:
            if names and item.get("kind") not in names:
                continue
            path = item.get("path")
            if not path or not Path(path).is_file():
                continue
            size = Path(path).stat().st_size
            out.append(
                {
                    "kind": item.get("kind"),
                    "label": item.get("label"),
                    "name": item.get("name"),
                    "size": size,
                    "preview": item.get("kind") in {"main", "audio"},
                    "url": f"/api/file?job={self.id}&kind={item.get('kind')}",
                    "download_url": f"/api/file?job={self.id}&kind={item.get('kind')}&dl=1",
                }
            )
        return out


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()
_FILE_REGISTRY: dict[str, Path] = {}  # upload_id -> 路径


def _get_job(job_id: str) -> Job | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _put_job(job: Job) -> None:
    with _JOBS_LOCK:
        _JOBS[job.id] = job


def recent_jobs(limit: int = 20) -> list[Job]:
    with _JOBS_LOCK:
        items = sorted(_JOBS.values(), key=lambda j: j.created, reverse=True)
    return items[:limit]


# --------------------------------------------------------------------------- #
# 静态资源
# --------------------------------------------------------------------------- #
def _serve_static(handler: BaseHTTPRequestHandler, rel_path: str) -> bool:
    target = (WEB_DIR / rel_path.lstrip("/")).resolve()
    try:
        target.relative_to(WEB_DIR.resolve())
    except ValueError:
        handler.send_error(403, "forbidden")
        return True
    if not target.is_file():
        return False
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
        ctype += "; charset=utf-8"
    data = target.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)
    return True


# --------------------------------------------------------------------------- #
# multipart/form-data 解析（手写，避免依赖已废弃的 cgi 模块）
# --------------------------------------------------------------------------- #
def parse_multipart(handler: BaseHTTPRequestHandler) -> tuple[dict[str, str], dict[str, tuple[str, Path]]]:
    """解析 multipart 表单。

    返回 (普通字段字典, {字段名: (原始文件名, 落盘临时路径)})。
    文件是流式写入临时文件的，不会把整个文件读进内存。
    """
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype:
        raise ValueError("请求不是 multipart/form-data")
    marker = "boundary="
    idx = ctype.find(marker)
    if idx < 0:
        raise ValueError("缺少 boundary")
    boundary = ctype[idx + len(marker):].strip().strip('"')
    if not boundary:
        raise ValueError("boundary 为空")
    delimiter = b"--" + boundary.encode()

    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        raise ValueError("请求体为空")
    if length > MAX_UPLOAD_BYTES + 8 * 1024 * 1024:
        raise ValueError(f"文件过大（上限 {MAX_UPLOAD_BYTES // 1048576} MB）")

    body = handler.rfile.read(length)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, Path]] = {}

    for chunk in body.split(delimiter):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head_end = chunk.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        raw_head = chunk[:head_end].decode("utf-8", "replace")
        payload = chunk[head_end + 4:]
        # 去掉尾部 CRLF
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]

        name = None
        filename = None
        for line in raw_head.splitlines():
            lower = line.lower()
            if lower.startswith("content-disposition:"):
                for part in line.split(";")[1:]:
                    part = part.strip()
                    if part.startswith("name="):
                        name = part[5:].strip().strip('"')
                    elif part.startswith("filename="):
                        filename = part[9:].strip().strip('"')
            if name and filename is not None:
                break
        if not name:
            continue
        if filename is None:
            fields[name] = payload.decode("utf-8", "replace")
        else:
            ensure_dirs(UPLOAD_DIR)
            safe = sanitize_filename(filename, max_length=120, fallback="upload")
            dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:10]}_{safe}"
            dest.write_bytes(payload)
            files[name] = (filename, dest)
    return fields, files


# --------------------------------------------------------------------------- #
# 任务执行
# --------------------------------------------------------------------------- #
def _tail(lines: list[str], n: int = 12) -> str:
    return "\n".join(lines[-n:])


def run_download_job(job: Job, payload: dict) -> None:
    """执行一次「B 站搜索 -> 下载 -> 处理」任务。"""
    from .bili import SearchResult as SR

    chosen = SR(
        index=int(payload.get("index") or 0),
        video_id=str(payload.get("video_id") or ""),
        title=str(payload.get("title") or ""),
        uploader=str(payload.get("uploader") or ""),
        duration=payload.get("duration"),
        url=str(payload.get("url") or ""),
        view_count=payload.get("view_count"),
        thumbnail=payload.get("thumbnail") or None,
    )
    keep_source = bool(payload.get("keep_source"))
    manual_bpm = payload.get("manual_bpm")
    manual_bpm = float(manual_bpm) if manual_bpm not in (None, "", 0) else None

    job.title = chosen.title or chosen.video_id
    job.stage = "下载音频与曲绘"
    job.progress = 0.15
    job.add_log(f"已选择：{chosen.title}（{chosen.id_kind} {chosen.video_id}）")

    def log(message: str) -> None:
        job.add_log(message)

    def on_stage(text: str) -> None:
        job.stage = text

    result = process_download(
        chosen=chosen,
        out_root=Path(payload.get("output_root") or DEFAULT_OUTPUT_ROOT),
        keyword=str(payload.get("keyword") or ""),
        cookies=payload.get("cookies") or None,
        beats_per_bar=int(payload.get("beats_per_bar") or 4),
        manual_bpm=manual_bpm,
        keep_source=keep_source,
        tmp_parent=JOB_TMP_DIR,
        log=log,
        cancel_event=job.cancel_event,
    )
    _finish_with_track(job, result)


def run_file_job(job: Job, payload: dict) -> None:
    """执行一次「本地文件 -> 补空隙转码」任务。"""
    upload_id = payload.get("upload_id")
    src: Path | None = None

    if upload_id:
        src = _FILE_REGISTRY.get(str(upload_id))
    if src is None and payload.get("path"):
        candidate = Path(str(payload["path"])).expanduser()
        src = candidate if candidate.is_file() else None
    if src is None or not src.is_file():
        raise ValueError("找不到输入音频文件（上传可能已过期，请重新上传）")

    display_name = str(payload.get("title") or src.stem)
    manual = payload.get("manual_bpm")
    manual_bpm = float(manual) if manual not in (None, "", 0) else None

    job.title = display_name
    job.stage = "分析 BPM" if manual_bpm is None else "按指定 BPM 处理"
    job.progress = 0.2
    job.add_log(f"输入文件：{src.name}")

    def log(message: str) -> None:
        job.add_log(message)

    out_root = Path(payload.get("output_root") or DEFAULT_OUTPUT_ROOT)
    stem = sanitize_filename(display_name)
    work_dir = out_root / stem

    result = process_audio_file(
        src_audio=src,
        work_dir=work_dir,
        title=display_name,
        artist=str(payload.get("artist") or "本地导入"),
        manual_bpm=manual_bpm,
        cover_src=None,
        beats_per_bar=int(payload.get("beats_per_bar") or 4),
        file_stem=stem,
        keep_source=True,  # 本地上传的源文件由我们统一清理，不在这里删
        extra_manifest={
            "来源": {"平台": "local", "原始文件名": src.name,
                     "原始路径": str(src) if not upload_id else None},
        },
        log=log,
        cancel_event=job.cancel_event,
    )
    _finish_with_track(job, result)


def _finish_with_track(job: Job, result: ProcessedTrack) -> None:
    job.work_dir = str(result.work_dir)
    job.artifacts = [
        {"kind": a["kind"], "label": a["label"], "name": a["name"], "path": str(a["path"])}
        for a in result.artifacts()
    ]
    bpm = result.bpm
    job.summary = {
        "bpm": bpm.bpm,
        "bpm_source": bpm.source,
        "bpm_reason": bpm.reason,
        "bpm_quantized_from": bpm.quantized_from,
        "bpm_confidence": bpm.local_confidence,
        "bpm_candidates": bpm.local_candidates,
        "gap_seconds": round(result.gap_seconds, 4),
        "beat_seconds": round(60.0 / bpm.bpm, 4),
        "source_duration": result.source_duration,
        "output_duration": result.output_duration,
        "work_dir": str(result.work_dir),
    }
    job.stage = "完成"
    job.progress = 1.0


def start_job(kind: str, runner, payload: dict) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], kind=kind)
    _put_job(job)

    def worker() -> None:
        with _HEAVY_LOCK:
            if job.cancel_event.is_set():
                job.status = "cancelled"
                job.stage = "已取消"
                job.finished = time.time()
                return
            job.status = "running"
            job.stage = "开始处理"
            try:
                runner(job, payload)
                job.status = "done"
            except JobCancelled:
                job.status = "cancelled"
                job.stage = "已取消"
                job.add_log("任务已被用户取消")
            except (BiliError, AudioError, ValueError) as exc:
                job.status = "error"
                job.stage = "失败"
                job.error = str(exc)
                job.add_log(f"[错误] {exc}")
            except Exception as exc:  # noqa: BLE001
                job.status = "error"
                job.stage = "失败"
                job.error = f"{type(exc).__name__}: {exc}"
                job.add_log("[错误] 未预期的异常:\n" + traceback.format_exc(limit=6))
            finally:
                job.finished = time.time()

    threading.Thread(target=worker, name=f"job-{job.id}", daemon=True).start()
    return job


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "BiliMusicTool/1.1"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础工具 ----------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 默认会往 stderr 打一堆访问日志，这里静音，避免刷屏
        return

    def _send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc

    # ---------------- GET ----------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in {"/", "/index.html"}:
            if not _serve_static(self, "index.html"):
                self.send_error(404, "web/index.html not found")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            if not _serve_static(self, rel):
                self.send_error(404, "not found")
            return
        if path == "/favicon.ico":
            if not _serve_static(self, "favicon.svg"):
                self.send_error(404, "no favicon")
            return
        if path == "/api/health":
            self._send_json({"ok": True, **_health()})
            return
        if path == "/api/artifacts":
            self._send_json({"ok": True, "jobs": [j.to_dict(include_log=False) for j in recent_jobs()]})
            return
        if path.startswith("/api/job/"):
            job_id = path[len("/api/job/"):].strip("/")
            job = _get_job(job_id)
            if not job:
                self._send_error_json("任务不存在（服务可能已重启）", 404)
                return
            self._send_json({"ok": True, "job": job.to_dict()})
            return
        if path == "/api/file":
            self._serve_artifact(query)
            return
        # 未知路径：回退到静态目录（便于直接请求 /app.js 之类）
        if _serve_static(self, path.lstrip("/")):
            return
        self.send_error(404, "not found")

    # ---------------- POST ----------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/search":
                self._handle_search()
            elif path == "/api/cookies":
                self._handle_cookies()
            elif path == "/api/download":
                self._handle_download()
            elif path == "/api/upload":
                self._handle_upload()
            elif path == "/api/process":
                self._handle_process()
            elif path.endswith("/cancel") and path.startswith("/api/job/"):
                job_id = path[len("/api/job/"):-len("/cancel")].strip("/")
                job = _get_job(job_id)
                if not job:
                    self._send_error_json("任务不存在", 404)
                    return
                job.cancel_event.set()
                job.stage = "正在取消 ..."
                self._send_json({"ok": True})
            else:
                self.send_error(404, "not found")
        except ValueError as exc:
            self._send_error_json(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(f"{type(exc).__name__}: {exc}", 500)

    # ---------------- 接口实现 ----------------
    def _handle_search(self) -> None:
        body = self._read_json()
        keyword = str(body.get("keyword") or "").strip()
        if not keyword:
            self._send_error_json("请输入歌名")
            return
        count = int(body.get("count") or 8)
        count = max(1, min(count, 20))
        cookies = body.get("cookies") or None
        started = time.time()
        search_log: list[str] = []
        try:
            results = search_bilibili(
                keyword, count=count, cookies_from_browser=cookies,
                heuristic=bool(body.get("smart", True)),
                log=search_log.append,
            )
        except BiliError as exc:
            self._send_error_json(str(exc), 502)
            return
        elapsed = time.time() - started
        self._send_json(
            {
                "ok": True,
                "keyword": keyword,
                "elapsed": round(elapsed, 2),
                # 把通道选择过程一并返回，排查"结果缺元数据"时很有用
                "trace": search_log[-8:],
                "results": [
                    {
                        "index": r.index,
                        "video_id": r.video_id,
                        "id_kind": r.id_kind,
                        "title": r.title,
                        "uploader": r.uploader,
                        "duration": r.duration,
                        "duration_text": r.duration_text,
                        "view_count": r.view_count,
                        "url": r.url,
                        "thumbnail": r.thumbnail,
                    }
                    for r in results
                ],
            }
        )

    def _handle_cookies(self) -> None:
        """检查某个浏览器的 Cookie 能否真正被读取，并给出可操作的结论。

        之所以单独做一个接口：Windows 上读浏览器 Cookie 经常直接失败
        （数据库被锁 / 新版加密无法解密），失败后 yt-dlp 会硬失败。
        让用户能先自己确认，比让他在下载失败里猜原因好得多。
        """
        body = self._read_json()
        browser = str(body.get("browser") or "").strip()
        if not browser:
            self._send_error_json("请选择一个浏览器")
            return
        from .cookies import probe_browser_cookies

        probe = probe_browser_cookies(browser)
        self._send_json({"ok": True, "result": probe.to_dict()})

    def _handle_download(self) -> None:
        body = self._read_json()
        if not body.get("video_id") or not body.get("url"):
            self._send_error_json("缺少视频信息，请重新搜索后再选")
            return
        job = start_job("download", run_download_job, body)
        self._send_json({"ok": True, "job": job.to_dict()})

    def _handle_upload(self) -> None:
        fields, files = parse_multipart(self)
        if not files:
            self._send_error_json("没有收到文件")
            return
        _, (orig_name, temp_path) = next(iter(files.items()))
        upload_id = uuid.uuid4().hex[:12]
        final = temp_path.with_name(f"{upload_id}_{temp_path.name.split('_', 1)[-1]}")
        try:
            temp_path.replace(final)
        except OSError:
            final = temp_path
        _FILE_REGISTRY[upload_id] = final
        size = final.stat().st_size
        self._send_json(
            {
                "ok": True,
                "upload_id": upload_id,
                "name": orig_name,
                "size": size,
                "manual_bpm": fields.get("manual_bpm") or None,
            }
        )

    def _handle_process(self) -> None:
        body = self._read_json()
        if not body.get("upload_id") and not body.get("path"):
            self._send_error_json("缺少 upload_id 或 path")
            return
        job = start_job("file", run_file_job, body)
        self._send_json({"ok": True, "job": job.to_dict()})

    # ---------------- 产物读取（支持 Range） ----------------
    def _serve_artifact(self, query: dict) -> None:
        job_id = (query.get("job") or [""])[0]
        kind = (query.get("kind") or [""])[0]
        download = (query.get("dl") or ["0"])[0] == "1"
        job = _get_job(job_id)
        if not job:
            self.send_error(404, "任务不存在")
            return
        match = next((a for a in job.artifacts if a.get("kind") == kind), None)
        if not match:
            self.send_error(404, "产物不存在")
            return
        path = Path(match["path"])
        if not path.is_file():
            self.send_error(404, "文件已被移动或删除")
            return

        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        name = path.name
        quoted = urllib.parse.quote(name)

        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            if "-" in spec:
                a, _, b = spec.partition("-")
                try:
                    if a.strip():
                        start = int(a)
                        end = int(b) if b.strip() else size - 1
                    else:
                        start = max(0, size - int(b))
                        end = size - 1
                    start = max(0, min(start, size - 1))
                    end = max(start, min(end, size - 1))
                    status = 206
                except ValueError:
                    start, end, status = 0, size - 1, 200

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        disposition = "attachment" if download else "inline"
        self.send_header(
            "Content-Disposition",
            f"{disposition}; filename=\"{quoted}\"; filename*=UTF-8''{quoted}",
        )
        self.end_headers()

        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            chunk_size = 256 * 1024
            while remaining > 0:
                data = fh.read(min(chunk_size, remaining))
                if not data:
                    break
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(data)


# --------------------------------------------------------------------------- #
# 环境信息 / 启动
# --------------------------------------------------------------------------- #
def _health() -> dict:
    ffmpeg = find_ffmpeg()
    ytdlp = which("yt-dlp")
    return {
        "ffmpeg": str(ffmpeg) if ffmpeg else None,
        "yt_dlp": str(ytdlp) if ytdlp else "python -m yt_dlp",
        "output_root": str(DEFAULT_OUTPUT_ROOT),
        "python": sys.version.split()[0],
    }


def _prune_uploads(max_age_hours: float = 24.0) -> None:
    """清理过期的上传中间文件。"""
    if not UPLOAD_DIR.exists():
        return
    deadline = time.time() - max_age_hours * 3600
    for p in UPLOAD_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < deadline:
                p.unlink()
        except OSError:
            pass


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    ensure_dirs(UPLOAD_DIR, JOB_TMP_DIR)
    _prune_uploads()
    if not (WEB_DIR / "index.html").is_file():
        raise FileNotFoundError(f"找不到前端页面：{WEB_DIR / 'index.html'}")

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print("=" * 62)
    print("  BiliMusicTool 前端已启动")
    print(f"  地址: {url}")
    print(f"  输出目录: {DEFAULT_OUTPUT_ROOT}")
    print("  按 Ctrl+C 停止")
    print("=" * 62, flush=True)

    if open_browser:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 ...", flush=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
        # 退出时清掉任务临时目录
        shutil.rmtree(JOB_TMP_DIR, ignore_errors=True)
