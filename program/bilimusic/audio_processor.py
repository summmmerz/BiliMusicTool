"""音频处理：用 ffmpeg 做精确的对齐 / 补空隙 / 重采样 / MP3 编码。

处理链路（bpm 对齐模式）：
  1. 解码源音频为 44.1kHz 立体声 PCM（浮点 WAV）
  2. 去掉源文件开头自带的静音（保持"空隙"只由我们添加，长度可控）
  3. 在最前面拼接 一个小节（4 拍）时长的数字静音——这就实现了
     "歌曲开始前有它 BPM 一个小节的空隙"
  4. （可选）再微调，让检测到的首拍落在小节线上
  5. 编码为 44.1kHz / 320kbps MP3，写入 ID3 标签（标题/艺术家/BPM/曲绘）
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import (
    MP3_BITRATE,
    SAMPLE_RATE,
    ensure_dirs,
    find_ffmpeg,
    run_cancellable,
)

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


class AudioError(RuntimeError):
    pass


def run_ffmpeg(args: list[str], log: LogFn | None = None, cancel_event=None) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise AudioError("未找到 ffmpeg，请先安装（winget install Gyan.FFmpeg）")
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    (log or _noop)("ffmpeg " + " ".join(args[:6]) + (" ..." if len(args) > 6 else ""))
    rc, _out, err = run_cancellable(cmd, cancel_event=cancel_event)
    if rc != 0:
        raise AudioError("ffmpeg 失败:\n" + (err or "").strip()[-1500:])


def probe_duration(path: Path, cancel_event=None) -> float | None:
    """取媒体时长（秒）。

    这里**刻意不依赖 ffprobe**：ffprobe.exe 是独立二进制（约 100 MB，
    和 ffmpeg.exe 一样大），而我们只需要它输出一个时长数字。
    ffmpeg 自己在 stderr 里就会打印 `Duration: 00:04:04.10`，
    直接解析即可，精度一样（百分秒），而且进程启动更快。
    这样分发包可以少带 100 MB。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    rc, _out, err = run_cancellable(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(path), "-f", "null", "-"],
        cancel_event=cancel_event,
        timeout=120.0,
    )
    return _parse_duration_from_ffmpeg(err or "")


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.(\d+)")


def _parse_duration_from_ffmpeg(text: str) -> float | None:
    """从 ffmpeg 的 stderr 里解析 `Duration: HH:MM:SS.cc`。"""
    m = _DURATION_RE.search(text or "")
    if not m:
        return None
    hours, minutes, seconds, frac = m.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + float("0." + frac)


def detect_leading_silence(
    path: Path,
    silence_db: float = -45.0,
    min_duration: float = 0.05,
    cancel_event=None,
) -> float:
    """检测文件开头自带的静音时长（秒）。失败返回 0。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return 0.0
    rc, out, err = run_cancellable(
        [
            str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(path),
            "-af", f"silencedetect=noise={silence_db}dB:d={min_duration}",
            "-f", "null", "-",
        ],
        cancel_event=cancel_event,
    )
    text = (err or "") + (out or "")
    for line in text.splitlines():
        if "silence_end" in line and "silence_start: 0" in text:
            import re

            m = re.search(r"silence_end:\s*([0-9.]+)", line)
            if m:
                return float(m.group(1))
    return 0.0


@dataclass
class ProcessResult:
    output_mp3: Path
    silence_seconds: float
    gap_seconds: float
    total_duration: float | None
    source_duration: float | None
    trimmed_leading: float


def process_audio(
    src: Path,
    dest_mp3: Path,
    gap_seconds: float,
    leading_trim: float = 0.0,
    extra_leading: float = 0.0,
    title: str | None = None,
    artist: str | None = None,
    bpm: float | None = None,
    cover: Path | None = None,
    log: LogFn | None = None,
    delete_source: bool = False,
    cancel_event=None,
) -> ProcessResult:
    """把 src 处理成目标 MP3：去掉开头静音 -> 补 gap+extra 空隙 -> 44.1kHz/320k 编码。

    delete_source=True 时在成功后删除源文件（用于下载来的中间文件）。
    """
    log = log or _noop
    ensure_dirs(dest_mp3.parent)
    if not src.is_file():
        raise AudioError(f"源音频不存在: {src}")

    if leading_trim <= 0:
        detected = detect_leading_silence(src, cancel_event=cancel_event)
        leading_trim = detected
        if detected > 0.01:
            log(f"源文件开头自带 {detected:.3f}s 静音，将从处理起点剔除")

    total_pad = max(0.0, gap_seconds) + max(0.0, extra_leading)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        aligned_wav = td_path / "aligned.wav"
        filter_chain = _build_filter_chain(leading_trim, total_pad)
        run_ffmpeg(
            [
                "-y",
                "-i", str(src),
                "-af", filter_chain,
                "-ar", str(SAMPLE_RATE),
                "-ac", "2",
                "-c:a", "pcm_s16le",
                str(aligned_wav),
            ],
            log,
            cancel_event,
        )
        if not aligned_wav.is_file():
            raise AudioError("音频对齐阶段未产出中间文件")

        args = [
            "-y",
            "-i", str(aligned_wav),
        ]
        if cover and cover.is_file():
            args += ["-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
        args += [
            "-ar", str(SAMPLE_RATE),
            "-ac", "2",
            "-c:a", "libmp3lame",
            "-b:a", MP3_BITRATE,
        ]
        if title:
            args += ["-metadata", f"title={title}"]
        if artist:
            args += ["-metadata", f"artist={artist}"]
        if bpm:
            args += ["-metadata", f"TBPM={bpm:g}", "-metadata", f"bpm={bpm:g}"]
        args += ["-id3v2_version", "3", str(dest_mp3)]
        run_ffmpeg(args, log, cancel_event)

    src_dur = probe_duration(src)
    out_dur = probe_duration(dest_mp3)
    log(f"输出完成: {dest_mp3.name} ({dest_mp3.stat().st_size / 1048576:.1f} MB, {out_dur or 0:.2f}s)")
    if delete_source:
        try:
            src.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            log(f"[警告] 源文件删除失败（忽略）: {exc}")
    return ProcessResult(
        output_mp3=dest_mp3,
        silence_seconds=total_pad,
        gap_seconds=gap_seconds,
        total_duration=out_dur,
        source_duration=src_dur,
        trimmed_leading=leading_trim,
    )


def _build_filter_chain(leading_trim: float, total_pad: float) -> str:
    """构造 ffmpeg -af 滤镜链。使用 adelay 在开头插入精确时长的静音。"""
    parts: list[str] = []
    if leading_trim > 0.001:
        parts.append(f"atrim=start={leading_trim:.6f}")
        parts.append("asetpts=PTS-STARTPTS")
    if total_pad > 0.0005:
        delay_ms = int(round(total_pad * 1000.0))
        if delay_ms < 1:
            delay_ms = 1
        # adelay 以毫秒为单位，all=1 对所有声道生效
        parts.append(f"adelay={delay_ms}:all=1")
    if not parts:
        return "anull"
    return ",".join(parts)


def gap_for_bpm(bpm: float, beats_per_bar: int = 4) -> float:
    """一小节（默认 4/4）时长，单位秒。"""
    if bpm <= 0:
        raise ValueError("BPM 必须为正数")
    return 60.0 / bpm * beats_per_bar


def split_audio_only(src: Path, dest: Path, fmt: str = "wav", log: LogFn | None = None,
                     cancel_event=None) -> Path:
    """把音频与曲绘真正分离：导出一份不含封面标签的纯音频文件。"""
    log = log or _noop
    ensure_dirs(dest.parent)
    if fmt.lower() == "mp3":
        run_ffmpeg(
            ["-y", "-i", str(src), "-map", "0:a", "-c:a", "libmp3lame",
             "-b:a", MP3_BITRATE, "-ar", str(SAMPLE_RATE), str(dest)],
            log,
            cancel_event,
        )
    else:
        run_ffmpeg(
            ["-y", "-i", str(src), "-map", "0:a", "-c:a", "pcm_s16le",
             "-ar", str(SAMPLE_RATE), str(dest)],
            log,
            cancel_event,
        )
    return dest


def copy_asset(src: Path, dest: Path) -> Path:
    ensure_dirs(dest.parent)
    shutil.copy2(src, dest)
    return dest
