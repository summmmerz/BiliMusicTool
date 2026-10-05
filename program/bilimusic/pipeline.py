"""共享处理流水线：CLI 与 Web 前端都调用这里的函数，避免两套实现漂移。

对外主要接口：
    decide_bpm(...)       本地音频分析 + 取整规则（纯离线，不查任何外部服务）
    process_audio_file(...) 一个音频文件 -> 补一小节空隙 + 44.1kHz/320kbps MP3 + 曲绘分离
    process_download(...) 按 B 站视频下载并走完整流程
"""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .audio_processor import (
    AudioError,
    gap_for_bpm,
    process_audio,
    probe_duration,
    split_audio_only,
)
from .bili import (
    BiliError,
    SearchResult,
    TrackInfo,
    download_audio,
    download_cover,
    fetch_track_info,
)
from .bpm_detector import quantize_bpm
from .config import (JobCancelled, MP3_BITRATE, SAMPLE_RATE, ensure_dirs,
                     sanitize_filename)
from .cookies import resolve_cookies

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# --------------------------------------------------------------------------- #
# BPM 决策（纯本地，不依赖任何外部服务）
# --------------------------------------------------------------------------- #
@dataclass
class BpmDecision:
    """BPM 最终取值及其依据，便于写进清单也给前端展示。"""

    bpm: float
    source: str  # manual / local
    reason: str
    local_bpm: float | None = None
    local_confidence: float | None = None
    local_candidates: list[float] = field(default_factory=list)
    first_beat: float = 0.0
    quantized_from: float | None = None  # 取整前的值（发生过取整时才有意义）

    @property
    def note(self) -> str:
        return self.reason


def decide_bpm(
    audio_path: Path,
    title: str = "",
    artist: str = "",
    manual_bpm: float | None = None,
    log: LogFn | None = None,
    bpm_strategy: str = "auto",
) -> BpmDecision:
    """确定最终 BPM。

    规则（本项目约定）：
      * 手动指定      -> 原样使用
      * 本地音频分析  -> **四舍五入到整数**

    这里刻意不做任何在线查询：原先挂的两个公开 BPM 库一个已关停
    （getonboard）、一个端点失效（songbpm），拿不到数据还让日志里
    一直显示"在线无结果"，反而误导人。本地分析在合成节拍上实测
    6/6 零误差（相对误差 < 0.15%），足够支撑"补一小节空隙"这个用途。
    """
    log = log or _noop

    if manual_bpm is not None and manual_bpm > 0:
        bpm = float(manual_bpm)
        return BpmDecision(
            bpm=bpm,
            source="manual",
            reason=f"手动指定 BPM = {bpm:g}",
            local_confidence=1.0,
            first_beat=0.0,
        )

    from .bpm_detector import analyze_bpm

    local = analyze_bpm(audio_path)
    log(f"本地分析：BPM ≈ {local.bpm:g}（置信度 {local.confidence:.2f}）"
        + (f"，候选速度 {local.candidates}" if local.candidates else ""))

    final = quantize_bpm(local.bpm, bpm_strategy)
    reason = "本地音频分析"
    quantized_from: float | None = None
    if abs(final - local.bpm) > 1e-9:
        quantized_from = local.bpm
        reason += f"；BPM 取整 {local.bpm:g} -> {final:g}"

    return BpmDecision(
        bpm=final,
        source="local",
        reason=reason,
        local_bpm=local.bpm,
        local_confidence=local.confidence,
        local_candidates=list(local.candidates),
        first_beat=local.first_beat,
        quantized_from=quantized_from,
    )


# --------------------------------------------------------------------------- #
# 音频文件 -> 标准产物
# --------------------------------------------------------------------------- #
@dataclass
class ProcessedTrack:
    work_dir: Path
    main_mp3: Path
    audio_only_mp3: Path | None
    cover: Path | None
    manifest_path: Path
    bpm: BpmDecision
    gap_seconds: float
    source_duration: float | None
    output_duration: float | None

    def artifacts(self) -> list[dict]:
        """给前端的产物清单。"""
        out: list[dict] = [
            {"kind": "main", "label": "歌曲（含空隙）", "path": self.main_mp3,
             "name": self.main_mp3.name},
        ]
        if self.audio_only_mp3:
            out.append({"kind": "audio", "label": "纯音频（无封面）",
                        "path": self.audio_only_mp3, "name": self.audio_only_mp3.name})
        if self.cover:
            out.append({"kind": "cover", "label": "曲绘", "path": self.cover,
                        "name": self.cover.name})
        out.append({"kind": "manifest", "label": "信息清单", "path": self.manifest_path,
                    "name": self.manifest_path.name})
        return out


def process_audio_file(
    src_audio: Path,
    work_dir: Path,
    title: str,
    artist: str = "",
    bpm: float | None = None,
    manual_bpm: float | None = None,
    cover_src: Path | None = None,
    cover_name: str = "曲绘.png",
    beats_per_bar: int = 4,
    bpm_strategy: str = "auto",
    file_stem: str | None = None,
    keep_source: bool = False,
    extra_manifest: dict | None = None,
    log: LogFn | None = None,
    cancel_event=None,
) -> ProcessedTrack:
    """把一个音频文件处理成标准产物。

    bpm 已确定时直接传入（跳过检测）；否则用 manual_bpm 或本地音频分析。
    """
    log = log or _noop
    ensure_dirs(work_dir)
    stem = file_stem or sanitize_filename(title)

    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled("任务已取消")

    if bpm is None:
        decision = decide_bpm(
            src_audio, title=title, artist=artist, manual_bpm=manual_bpm,
            log=log, bpm_strategy=bpm_strategy,
        )
    else:
        decision = BpmDecision(
            bpm=float(bpm), source="given", reason=f"BPM 已指定 = {float(bpm):g}",
            local_confidence=1.0,
        )
    bpm_value = decision.bpm

    # 曲绘落到工作目录
    cover_final: Path | None = None
    if cover_src and cover_src.is_file():
        cover_final = work_dir / cover_name
        if cover_src.resolve() != cover_final.resolve():
            shutil.move(str(cover_src), str(cover_final))
    # 清理曲绘的中间临时文件（下载失败时可能残留在工作目录）
    for leftover in work_dir.glob("_cover_tmp*"):
        try:
            leftover.unlink()
        except OSError:
            pass

    gap_seconds = gap_for_bpm(bpm_value, beats_per_bar)
    log(f"最终 BPM = {bpm_value:g}，每拍 {60.0 / bpm_value:.4f}s，"
        f"1 小节（{beats_per_bar}/4）= {gap_seconds:.4f}s")
    log(f"依据：{decision.reason}")

    main_mp3 = work_dir / f"{stem}.mp3"
    result = process_audio(
        src=src_audio,
        dest_mp3=main_mp3,
        gap_seconds=gap_seconds,
        title=title,
        artist=artist,
        bpm=bpm_value,
        cover=cover_final,
        log=log,
        delete_source=not keep_source,
        cancel_event=cancel_event,
    )

    audio_only: Path | None = None
    try:
        audio_only = split_audio_only(main_mp3, work_dir / f"{stem}.audio-only.mp3",
                                       fmt="mp3", log=log, cancel_event=cancel_event)
    except AudioError as exc:
        log(f"[警告] 纯音频分离失败：{exc}")

    manifest = {
        "生成时间": datetime.now().isoformat(timespec="seconds"),
        "工具版本": __version__,
        "BPM": {
            "最终值": bpm_value,
            "来源": decision.source,
            "判定说明": decision.reason,
            "本地分析": decision.local_bpm,
            "本地置信度": decision.local_confidence,
            "本地候选": decision.local_candidates,
            "取整前": decision.quantized_from,
            "每拍秒": round(60.0 / bpm_value, 4),
            "每小节拍数": beats_per_bar,
            "一小节秒": round(gap_seconds, 4),
            "首拍偏移秒": decision.first_beat,
        },
        "音频": {
            "输出MP3": main_mp3.name,
            "纯音频MP3": audio_only.name if audio_only else None,
            "采样率": SAMPLE_RATE,
            "码率": MP3_BITRATE,
            "源文件时长": result.source_duration,
            "输出时长": result.total_duration,
            "开头剔除静音秒": round(result.trimmed_leading, 4),
            "补入空隙秒": round(result.silence_seconds, 4),
        },
        "曲绘": {"文件": cover_final.name if cover_final else None},
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    manifest_path = work_dir / "info.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"完成：{main_mp3.name}"
        + (f" | 纯音频 {audio_only.name}" if audio_only else "")
        + (f" | 曲绘 {cover_final.name}" if cover_final else ""))

    return ProcessedTrack(
        work_dir=work_dir,
        main_mp3=main_mp3,
        audio_only_mp3=audio_only,
        cover=cover_final,
        manifest_path=manifest_path,
        bpm=decision,
        gap_seconds=gap_seconds,
        source_duration=result.source_duration,
        output_duration=result.total_duration,
    )


# --------------------------------------------------------------------------- #
# B 站视频 -> 标准产物
# --------------------------------------------------------------------------- #
def process_download(
    chosen: SearchResult,
    out_root: Path,
    keyword: str = "",
    cookies: str | None = None,
    beats_per_bar: int = 4,
    manual_bpm: float | None = None,
    bpm_strategy: str = "auto",
    keep_source: bool = False,
    tmp_parent: Path | None = None,
    log: LogFn | None = None,
    cancel_event=None,
) -> ProcessedTrack:
    """下载 B 站视频音频 + 曲绘，并走完整处理流程。"""
    log = log or _noop
    ensure_dirs(out_root)
    tmp_dir = Path(tempfile.mkdtemp(
        prefix="bilimusic_", dir=str(tmp_parent or out_root)
    ))
    try:
        # 先探测浏览器 Cookie 是否真的可用。不可用就回落匿名——
        # 否则 yt-dlp 会因为读不到 cookie 直接硬失败，连普通视频都下不了。
        browser = resolve_cookies(cookies, log=log)

        track: TrackInfo = fetch_track_info(
            chosen.video_id,
            cookies_from_browser=browser,
            log=log,
            fallback_title=chosen.title,
            fallback_uploader=chosen.uploader,
            fallback_thumbnail=chosen.thumbnail,
        )
        if not track.title:
            track.title = chosen.title or chosen.video_id
        if not track.uploader:
            track.uploader = chosen.uploader
        safe_title = sanitize_filename(track.title)
        work_dir = out_root / safe_title
        ensure_dirs(work_dir)
        log(f"标题：{track.title}")
        log(f"UP主：{track.uploader or '未知'}")

        src_audio = download_audio(chosen.url, tmp_dir, "source", cookies_from_browser=browser,
                                   log=log, cancel_event=cancel_event)

        cover_raw = download_cover(track, work_dir / "_cover_tmp", log=log,
                                  cancel_event=cancel_event)

        return process_audio_file(
            src_audio=src_audio,
            work_dir=work_dir,
            title=track.title,
            artist=track.uploader,
            manual_bpm=manual_bpm,
            cover_src=cover_raw,
            beats_per_bar=beats_per_bar,
            bpm_strategy=bpm_strategy,
            file_stem=safe_title,
            keep_source=keep_source,
            cancel_event=cancel_event,
            extra_manifest={
                "来源": {
                    "平台": "bilibili",
                    "关键词": keyword,
                    "视频ID": track.video_id,
                    "链接": track.url,
                    "标题": track.title,
                    "UP主": track.uploader,
                    "标签": track.tags,
                    "封面原始地址": track.cover_url,
                },
            },
            log=log,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
