"""全局配置与路径工具。"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path

# ---------- 音频输出规格（用户要求固定为 44.1kHz / 320kbps MP3） ----------
SAMPLE_RATE = 44100
MP3_BITRATE = "320k"

# ---------- 目录 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_output_root() -> Path:
    """产物根目录。

    优先用环境变量 BILIMUSIC_OUTPUT —— 启动器会把它设成"启动时所在目录/output"，
    这样用户从包根目录启动时，产物就在一眼能看到的地方，而不是藏在 program/ 里。
    没有该变量时退回 `<项目根>/output`。
    """
    override = os.environ.get("BILIMUSIC_OUTPUT")
    if override:
        return Path(override)
    return PROJECT_ROOT / "output"


DEFAULT_OUTPUT_ROOT = _default_output_root()
CACHE_DIR = PROJECT_ROOT / ".cache"

# ---------- 网络 ----------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BILI_REFERER = "https://www.bilibili.com/"
HTTP_TIMEOUT = 20

# ---------- 搜索 ----------
DEFAULT_SEARCH_COUNT = 8

_WIN_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_length: int = 80, fallback: str = "untitled") -> str:
    """把任意字符串转成 Windows 合法文件名（不含扩展名）。"""
    name = unicodedata.normalize("NFKC", name or "")
    name = _WIN_BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    if not name:
        return fallback
    if name.upper().split(".")[0] in _WIN_RESERVED:
        name = "_" + name
    return name


def which(name: str) -> str | None:
    """在 PATH 中查找可执行文件（含 .exe/.cmd/.bat），返回绝对路径。"""
    from shutil import which as _which

    found = _which(name)
    if found:
        return found
    extra_dirs = [
        Path(sys.executable).parent,
        Path(sys.executable).parent / "Scripts",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
    ]
    for d in extra_dirs:
        if not d or not d.exists():
            continue
        for suffix in ("", ".exe", ".cmd", ".bat"):
            candidate = d / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def find_ffmpeg() -> Path | None:
    """定位 ffmpeg。

    优先级：环境变量 BILIMUSIC_FFMPEG > 项目自带 tools/ffmpeg > PATH > winget 安装目录。
    项目自带一份可以让程序在没装 ffmpeg 的机器上直接跑（见 install_deps.py --fetch-ffmpeg）。
    """
    override = os.environ.get("BILIMUSIC_FFMPEG")
    if override and Path(override).is_file():
        return Path(override)

    for candidate in (
        PROJECT_ROOT / "tools" / "ffmpeg" / "ffmpeg.exe",
        PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",
        PROJECT_ROOT / "tools" / "ffmpeg" / "ffmpeg",
    ):
        if candidate.is_file():
            return candidate

    hit = which("ffmpeg")
    if hit:
        return Path(hit)
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        root = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
        if root.exists():
            for candidate in sorted(root.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"), reverse=True):
                return candidate
    return None


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


class JobCancelled(RuntimeError):
    """用户在前端点了取消，长任务主动退出。"""


def run_cancellable(
    cmd: list[str],
    cancel_event=None,
    timeout: float | None = None,
    poll: float = 0.2,
) -> tuple[int, str, str]:
    """运行外部程序，可被 cancel_event 打断。

    返回 (退出码, stdout, stderr)。被取消时抛出 JobCancelled，
    并确保子进程（及其整棵进程树）已被终止——这一点对本项目很重要：
    yt-dlp 与 ffmpeg 都会自己再拉起子进程。

    ⚠ 必须并发读取 stdout/stderr，不能只 wait()：
       用 PIPE 但不读，子进程输出一旦超过管道缓冲区（Windows 约 4~64KB）
       就会**写阻塞**，于是子进程永不退出、调用方永远等下去——直接死锁。
       yt-dlp `--dump-json` 每条结果 2~10KB，多条就会触发。
       这里用两个读取线程持续抽干管道，主线程只负责轮询退出与取消。
    """
    import subprocess
    import threading
    import time

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    chunks: dict[str, list[str]] = {"out": [], "err": []}

    def drain(pipe, key: str) -> None:
        try:
            for line in pipe:
                chunks[key].append(line)
        except Exception:  # noqa: BLE001 - 进程被杀时管道会中断，忽略
            pass
        finally:
            try:
                pipe.close()
            except Exception:  # noqa: BLE001
                pass

    readers = [
        threading.Thread(target=drain, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, "err"), daemon=True),
    ]
    for t in readers:
        t.start()

    deadline = None if timeout is None else time.monotonic() + timeout
    cancelled = False
    timed_out = False
    while True:
        if proc.poll() is not None:
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _kill_tree(proc)
            break
        if deadline is not None and time.monotonic() > deadline:
            timed_out = True
            _kill_tree(proc)
            break
        time.sleep(poll)

    for t in readers:
        t.join(timeout=3.0)

    out = "".join(chunks["out"])
    err = "".join(chunks["err"])
    if cancelled:
        raise JobCancelled("任务已取消")
    if timed_out:
        raise TimeoutError(f"命令超时（{timeout}s）")
    return (proc.returncode if proc.returncode is not None else -1), out, err


def _kill_tree(proc) -> None:
    """结束进程及其子进程。"""
    import subprocess

    try:
        if proc.poll() is not None:
            return
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass
