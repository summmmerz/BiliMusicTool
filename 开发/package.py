r"""打包成可以拷给别人的绿色压缩包 —— 一次产出两个版本。

用法（在「开发」文件夹里运行）：
    python package.py                  # 两个版本都出
    python package.py --lite           # 只出轻量版
    python package.py --embed          # 只出零安装版
    python package.py --no-zip         # 不压缩
    python package.py --out E:\        # 指定输出位置

两个版本的区别（都自带 ffmpeg，都能搜/下/处理）：

  轻量版  BiliMusicTool-1.0.0\             zip 约 37 MB
      不带 Python。目标机器需要自己装 Python 3.10+，
      首次运行联网 pip 安装约 88 MB 依赖。
      → 适合自己用、或对方懂电脑。

  零安装版  BiliMusicTool-1.0.0-standalone\  zip 约 80 MB
      自带嵌入式 Python（program\runtime\），依赖已装好。
      目标机器什么都不用装、首次运行不需要联网。
      → 适合给完全不懂电脑的人。

两者目录结构一致，根目录都只有「启动程序.bat + 使用说明.txt」：

    <包名>\
        启动程序.bat            唯一入口
        使用说明.txt
        output\                 产物
        program\                程序本体（零安装版多一个 runtime\）
        开发\                   进阶：命令行模式.bat / run.bat

注意：
    * ffplay.exe / ffprobe.exe 都不打包（各约 100 MB，用不到）
    * 零安装版的 runtime 必须留在 program\ 里 —— 它的 `._pth` 依赖 `..`
      相对路径找到 bilimusic（见 build_runtime.py 的说明）
    * 不打包 output\ 内容、__pycache__\、.runlogs\、dist\、.cache\
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

# package.py 在「开发」里，项目根在上一级
DEV_DIR = Path(__file__).resolve().parent
ROOT = DEV_DIR.parent
PROGRAM_SRC = ROOT / "program"
RUNTIME_SRC = PROGRAM_SRC / "runtime"

sys.path.insert(0, str(PROGRAM_SRC))

from bilimusic import __version__  # noqa: E402

# program\ 下要整体复制的目录（runtime 单独处理）
COPY_DIRS = ["bilimusic", "web"]
# program\ 下要复制的单个文件
COPY_FILES = [
    "serve.py",
    "install_deps.py",
    "selftest.py",
    "requirements.txt",
    "README.md",
]
# 从 开发\ 拷进 program\ 给用户用的诊断工具（源文件不在 program\ 里）
DIAG_TOOLS = ["diagnose_download.py"]
# 只带这一个 ffmpeg 可执行文件
FFMPEG_FILES = ["ffmpeg.exe"]
# 入口启动器
LAUNCHER = "启动程序.bat"
# 说明：轻量版用通用说明，零安装版用「什么都不用装」那版
GUIDE_NORMAL = "使用说明.txt"
GUIDE_STANDALONE = "使用说明-零安装版.txt"
# 开发\ 下要带给用户的（package.py / build_runtime.py 自己不带）
DEV_FILES = ["命令行模式.bat", "run.bat"]

EXCLUDE_PARTS = {"__pycache__", "output", ".runlogs", ".cache", ".git", "dist", "runtime"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".tmp", ".part"}


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in EXCLUDE_PARTS or Path(n).suffix in EXCLUDE_SUFFIX}


def _size_mb(p: Path) -> float:
    if p.is_file():
        return p.stat().st_size / 1048576
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1048576


def _write_embedded_launcher(target: Path) -> None:
    """把 launcher_standalone.txt 转成纯 ASCII + CRLF 的 启动程序.bat。"""
    src = DEV_DIR / "launcher_standalone.txt"
    if not src.is_file():
        raise SystemExit(f"找不到 {src}（零安装版启动器模板）")
    text = src.read_text(encoding="utf-8")
    bad = sorted({c for c in text if ord(c) > 127})
    if bad:
        raise SystemExit(f"启动器模板含非 ASCII 字符: {bad}")
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")
    (target / "启动程序.bat").write_bytes(data)
    print(f"  文件 启动程序.bat  （零安装版专用，{len(data)} 字节，纯 ASCII+CRLF）")


def build(out_root: Path, *, standalone: bool, make_zip: bool) -> Path:
    suffix = "-standalone" if standalone else ""
    name = f"BiliMusicTool-{__version__}{suffix}"
    target = out_root / name
    if target.exists():
        print(f"清理已存在的 {target}")
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    if not PROGRAM_SRC.is_dir():
        raise SystemExit(f"找不到 {PROGRAM_SRC}，请在「开发」文件夹里运行本脚本")

    print(f"=========== {name} ===========")

    # ---- program\ ----
    prog_dst = target / "program"
    prog_dst.mkdir(parents=True, exist_ok=True)
    print("[program\\]")
    for d in COPY_DIRS:
        src = PROGRAM_SRC / d
        if src.is_dir():
            shutil.copytree(src, prog_dst / d, ignore=_ignore)
            print(f"  目录 {d}\\")
        else:
            print(f"  [跳过] 找不到目录 {d}")
    for f in COPY_FILES:
        src = PROGRAM_SRC / f
        if src.is_file():
            shutil.copy2(src, prog_dst / f)
            print(f"  文件 {f}")
        else:
            print(f"  [跳过] 找不到文件 {f}")
    # 诊断工具从 开发\ 拷进 program\，方便用户直接运行
    for f in DIAG_TOOLS:
        src = DEV_DIR / f
        if src.is_file():
            shutil.copy2(src, prog_dst / f)
            print(f"  文件 {f}  ← 开发\\")
        else:
            print(f"  [跳过] 找不到 {f}")

    # ---- program\tools\ffmpeg\ ----
    src_ff = PROGRAM_SRC / "tools" / "ffmpeg"
    dst_ff = prog_dst / "tools" / "ffmpeg"
    if src_ff.is_dir():
        dst_ff.mkdir(parents=True, exist_ok=True)
        for exe in FFMPEG_FILES:
            cand = src_ff / exe
            if cand.is_file():
                shutil.copy2(cand, dst_ff / exe)
                print(f"  tools\\ffmpeg\\{exe}  ({_size_mb(cand):.1f} MB)")
            else:
                print(f"  [警告] 缺少 {exe}")
    else:
        print("  [警告] 没有 tools\\ffmpeg，目标机器需要自行安装 ffmpeg")

    # ---- 零安装版：带上嵌入式运行时 ----
    if standalone:
        print("[program\\runtime\\]  （嵌入式 Python）")
        if not RUNTIME_SRC.is_dir():
            raise SystemExit(
                f"找不到 {RUNTIME_SRC}。\n"
                f"请先运行：python 开发\\build_runtime.py"
            )
        shutil.copytree(RUNTIME_SRC, prog_dst / "runtime", ignore=_ignore)
        print(f"  运行时 ({_size_mb(RUNTIME_SRC):.1f} MB)")

    # ---- 根目录：入口 + 说明 ----
    print("[根目录]")
    guide_src = DEV_DIR / (GUIDE_STANDALONE if standalone else GUIDE_NORMAL)
    guide_dst_name = "使用说明.txt"
    if standalone:
        _write_embedded_launcher(target)
    else:
        src = ROOT / LAUNCHER
        if src.is_file():
            shutil.copy2(src, target / LAUNCHER)
            print(f"  文件 {LAUNCHER}")
        else:
            print(f"  [警告] 缺少 {LAUNCHER}")

    if guide_src.is_file():
        shutil.copy2(guide_src, target / guide_dst_name)
        tag = "（零安装版专用）" if standalone else ""
        print(f"  文件 {guide_dst_name}{tag}  ← {guide_src.name}")
    elif (ROOT / GUIDE_NORMAL).is_file():
        # 零安装版说明缺失时退回通用说明
        shutil.copy2(ROOT / GUIDE_NORMAL, target / guide_dst_name)
        print(f"  文件 {guide_dst_name}  ← 退回通用说明")
    else:
        print("  [警告] 找不到使用说明")

    out_dir = target / "output"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "把处理好的文件放在这里.txt").write_text(
        "程序处理完成的歌曲会保存在这个文件夹里。\n"
        "每首歌一个子文件夹，里面有 mp3（歌曲）、曲绘.png（封面图）和 info.json。\n",
        encoding="utf-8",
    )
    print("  目录 output\\（含一个说明文件）")

    # ---- 开发\ ----
    dev_dst = target / "开发"
    dev_dst.mkdir(exist_ok=True)
    print("[开发\\]")
    for f in DEV_FILES:
        src = DEV_DIR / f
        if src.is_file():
            shutil.copy2(src, dev_dst / f)
            print(f"  文件 {f}")
        else:
            print(f"  [跳过] 找不到 {f}")

    size = _size_mb(target)
    print(f"\n已生成: {target}\n大小:   {size:.1f} MB")

    if make_zip:
        zip_path = out_root / f"{name}.zip"
        print(f"正在压缩 -> {zip_path}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for f in sorted(target.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(out_root))
        print(f"压缩包: {zip_path}  ({_size_mb(zip_path):.1f} MB)")
    print()
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description="把 BiliMusicTool 打包成可分发的绿色包")
    ap.add_argument("--out", type=Path, default=ROOT / "dist",
                    help="输出目录（默认 项目根\\dist）")
    ap.add_argument("--zip", action="store_true", help="生成 zip（默认就生成）")
    ap.add_argument("--no-zip", action="store_true", help="只生成目录，不压缩")
    ap.add_argument("--lite", action="store_true", help="只出轻量版")
    ap.add_argument("--embed", "--standalone", dest="embed", action="store_true",
                    help="只出零安装版")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    make_zip = not args.no_zip

    want_lite = not args.embed
    want_embed = not args.lite

    if want_lite:
        build(args.out, standalone=False, make_zip=make_zip)
    if want_embed:
        build(args.out, standalone=True, make_zip=make_zip)

    print("完成。dist 内容：")
    for p in sorted(args.out.iterdir()):
        if p.name.startswith("BiliMusicTool"):
            kind = "目录" if p.is_dir() else "文件"
            print(f"  [{kind}] {p.name}  {_size_mb(p):.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
