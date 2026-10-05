"""命令行入口：B 站搜歌 -> 下载 -> 曲绘分离 -> BPM 检测 -> 补一小节空隙 -> 44.1kHz/320k MP3。

用法示例：
    python -m bilimusic "歌曲名"
    python -m bilimusic "歌曲名" -n 10 -y
    python -m bilimusic --from-file D:\\song.mp3 --bpm 174
    python -m bilimusic --analyze D:\\song.mp3
    python -m bilimusic --web              （等价于 python serve.py）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .audio_processor import AudioError, gap_for_bpm
from .bili import BiliError, SearchResult, search_bilibili
from .config import (
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_SEARCH_COUNT,
    JobCancelled,
    ensure_dirs,
    sanitize_filename,
)
from .pipeline import decide_bpm, process_audio_file, process_download


# --------------------------------------------------------------------------- #
# 终端输出
# --------------------------------------------------------------------------- #
class C:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        G = Y = B = R = D = E = ""
    else:
        G = "\033[32m"
        Y = "\033[33m"
        B = "\033[36m"
        R = "\033[31m"
        D = "\033[90m"
        E = "\033[0m"


def info(msg: str) -> None:
    print(f"{C.B}[信息]{C.E} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"{C.G}[完成]{C.E} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{C.Y}[警告]{C.E} {msg}", flush=True)


def err(msg: str) -> None:
    print(f"{C.R}[错误]{C.E} {msg}", file=sys.stderr, flush=True)


def step(n: int, total: int, msg: str) -> None:
    print(f"\n{C.B}=== 步骤 {n}/{total}：{msg} ==={C.E}", flush=True)


# --------------------------------------------------------------------------- #
# 交互式选择
# --------------------------------------------------------------------------- #
def print_results(results: list[SearchResult]) -> None:
    print()
    for r in results:
        views = f"{r.view_count:,}" if r.view_count else "?"
        print(f"  {C.Y}[{r.index}]{C.E} {r.title}")
        print(f"      {C.D}UP: {r.uploader} | 时长: {r.duration_text} | 播放: {views} | {r.bvid}{C.E}")


def choose_result(results: list[SearchResult], assume_yes: bool) -> SearchResult | None:
    if not results:
        return None
    if assume_yes:
        info(f"自动选择第 1 条：{results[0].title}")
        return results[0]
    print_results(results)
    print(f"\n{C.D}输入序号选择（回车=1，q=退出，s=重新搜索）{C.E}")
    while True:
        try:
            raw = input("选择> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.lower() == "q":
            return None
        if raw.lower() == "s":
            return None
        if raw == "":
            return results[0]
        if raw.isdigit() and 1 <= int(raw) <= len(results):
            return results[int(raw) - 1]
        warn("请输入有效的序号")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_pipeline(
    keyword: str,
    out_root: Path,
    count: int = DEFAULT_SEARCH_COUNT,
    assume_yes: bool = False,
    index: int | None = None,
    cookies: str | None = None,
    beats_per_bar: int = 4,
    keep_source: bool = False,
    min_confidence: float = 0.3,
    force_bpm: float | None = None,
    smart: bool = True,
) -> int:
    """搜索 -> 选择 -> 交给共享流水线处理。"""
    ensure_dirs(out_root)

    step(1, 3, f"在 B 站搜索「{keyword}」")
    try:
        results = search_bilibili(
            keyword, count=count, cookies_from_browser=cookies, log=info, heuristic=smart
        )
    except BiliError as exc:
        err(str(exc))
        return 2
    if not results:
        err("没有搜索到任何结果，请换个关键词")
        return 2
    info(f"共获取 {len(results)} 条结果" + ("（已按相关度重排）" if smart else ""))

    if index is not None:
        if not (1 <= index <= len(results)):
            err(f"--index 超出范围（1-{len(results)}）")
            return 2
        chosen = results[index - 1]
    else:
        chosen = choose_result(results, assume_yes)
    if chosen is None:
        warn("已取消")
        return 0
    ok(f"已选择：{chosen.title}（{chosen.id_kind} {chosen.video_id}）")

    step(2, 3, "下载音频、分离曲绘、检测 BPM")
    try:
        result = process_download(
            chosen=chosen,
            out_root=out_root,
            keyword=keyword,
            cookies=cookies,
            beats_per_bar=beats_per_bar,
            manual_bpm=force_bpm,
            keep_source=keep_source,
            log=info,
        )
    except (BiliError, AudioError, JobCancelled) as exc:
        err(str(exc))
        return 3

    step(3, 3, "完成")
    bpm = result.bpm
    if bpm.local_confidence is not None and bpm.local_confidence < min_confidence and force_bpm is None:
        warn(f"BPM 置信度较低（{bpm.local_confidence:.2f}），建议核对；可用 --bpm 手动指定")

    print()
    print(f"{C.G}全部完成！{C.E}")
    print(f"  目录：{result.work_dir}")
    print(f"  歌曲：{result.main_mp3.name}"
          f"（BPM {bpm.bpm:g}，已补 {result.gap_seconds:.3f}s 空隙，44.1kHz/320kbps）")
    if result.audio_only_mp3:
        print(f"  纯音频：{result.audio_only_mp3.name}")
    if result.cover:
        print(f"  曲绘：{result.cover.name}")
    print(f"  清单：info.json")
    print(f"  依据：{bpm.reason}")
    return 0


def run_local_file(
    src: Path,
    out_root: Path,
    force_bpm: float | None = None,
    beats_per_bar: int = 4,
    title: str | None = None,
) -> int:
    """处理本地已有音频（不联网搜索下载）。"""
    if not src.is_file():
        err(f"文件不存在: {src}")
        return 2
    stem = sanitize_filename(title or src.stem)
    info(f"输入：{src}")
    info(f"输出目录：{out_root / stem}")
    try:
        result = process_audio_file(
            src_audio=src,
            work_dir=out_root / stem,
            title=title or src.stem,
            artist="本地导入",
            manual_bpm=force_bpm,
            keep_source=True,
            beats_per_bar=beats_per_bar,
            file_stem=stem,
            log=info,
        )
    except (AudioError, JobCancelled) as exc:
        err(str(exc))
        return 3
    print()
    print(f"{C.G}完成！{C.E} {result.main_mp3}")
    print(f"  BPM {result.bpm.bpm:g} | 补入 {result.gap_seconds:.4f}s 空隙 | "
          f"源 {result.source_duration:.2f}s -> 输出 {result.output_duration:.2f}s")
    return 0


def run_analyze_only(src: Path, beats_per_bar: int = 4) -> int:
    """只分析 BPM 并输出 JSON（不产生任何文件）。"""
    if not src.is_file():
        err(f"文件不存在: {src}")
        return 2
    decision = decide_bpm(src, title=src.stem, log=info)
    print(json.dumps(
        {
            "文件": str(src),
            "本地BPM": decision.local_bpm,
            "置信度": decision.local_confidence,
            "候选": decision.local_candidates,
            "首拍偏移秒": decision.first_beat,
            "取整前": decision.quantized_from,
            "最终BPM": decision.bpm,
            f"一小节秒({beats_per_bar}/4)": round(gap_for_bpm(decision.bpm, beats_per_bar), 4),
            "说明": decision.reason,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bilimusic",
        description="B 站搜歌 -> 下载 -> 曲绘分离 -> BPM 检测 -> 开头补一小节空隙 -> 44.1kHz/320kbps MP3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python -m bilimusic \"千本樱\"\n"
            "  python -m bilimusic \"打上花火\" -n 10 -y --index 2\n"
            "  python -m bilimusic --from-file D:\\song.mp3 --bpm 174\n"
            "  python -m bilimusic --analyze D:\\song.mp3\n"
        ),
    )
    p.add_argument("keyword", nargs="?", help="要搜索的歌曲名")
    p.add_argument("-n", "--count", type=int, default=DEFAULT_SEARCH_COUNT, help="搜索结果条数（默认 8）")
    p.add_argument("-y", "--yes", action="store_true", help="自动选第一条，不交互")
    p.add_argument("--index", type=int, default=None, help="直接指定搜索结果序号（从 1 开始）")
    p.add_argument("--no-smart", action="store_true",
                   help="关闭智能排序（默认会把翻唱/伴奏/短片降权，让原曲和 MV 排在前面）")
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT_ROOT, help="输出根目录")
    p.add_argument("--cookies-from-browser", default=None,
                   help="从浏览器读取 cookie（如 chrome/edge/firefox），用于需要登录的视频")
    p.add_argument("--bpm", type=float, default=None, help="手动指定 BPM，跳过检测")
    p.add_argument("--beats-per-bar", type=int, default=4, help="每小节拍数（默认 4）")
    p.add_argument("--keep-source", action="store_true", help="保留下载的原始音频文件")
    p.add_argument("--min-confidence", type=float, default=0.3, help="低于该置信度时给出警告")
    p.add_argument("--from-file", type=Path, default=None, help="跳过下载，直接处理本地音频文件")
    p.add_argument("--analyze", type=Path, default=None, help="只分析 BPM，不做处理")
    p.add_argument("--web", action="store_true",
                   help="启动网页前端（等同于 python serve.py）")
    p.add_argument("--web-port", type=int, default=8765, help="网页前端端口（默认 8765）")
    p.add_argument("--web-open", action="store_true", help="启动网页前端后自动打开浏览器")
    p.add_argument("-v", "--version", action="version", version=f"BiliMusicTool {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.web:
        from .web import serve

        serve(port=args.web_port, open_browser=args.web_open)
        return 0
    if args.analyze:
        return run_analyze_only(args.analyze, beats_per_bar=args.beats_per_bar)
    if args.from_file:
        return run_local_file(
            args.from_file, args.output, force_bpm=args.bpm,
            beats_per_bar=args.beats_per_bar,
        )
    if not args.keyword:
        build_parser().print_help()
        return 2
    return run_pipeline(
        keyword=args.keyword,
        out_root=args.output,
        count=args.count,
        assume_yes=args.yes,
        index=args.index,
        cookies=args.cookies_from_browser,
        beats_per_bar=args.beats_per_bar,
        keep_source=args.keep_source,
        min_confidence=args.min_confidence,
        force_bpm=args.bpm,
        smart=not args.no_smart,
    )


if __name__ == "__main__":
    sys.exit(main())
