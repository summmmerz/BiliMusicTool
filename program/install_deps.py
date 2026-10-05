"""环境自检与依赖安装。

做三件事：
  1. 检查 Python 依赖（numpy / requests / yt-dlp）
  2. 检查 ffmpeg，缺失时给出安装指引（可加 --auto 自动用 winget 安装）
  3. 输出一个可操作的诊断报告
"""
from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bilimusic.config import find_ffmpeg, which  # noqa: E402

# 只需要这三个。已移除 scipy（原占约 85 MB）——BPM 检测改用 numpy 等价实现。
PY_DEPS = [
    ("numpy", "numpy"),
    ("requests", "requests"),
    ("yt_dlp", "yt-dlp"),
]


def _module_version(module: str, module_obj) -> str:
    """取版本号：优先 __version__，其次用包元数据（yt-dlp 新版本已不带 __version__）。"""
    ver = getattr(module_obj, "__version__", None)
    if ver:
        return str(ver)
    try:
        from importlib.metadata import version as _v

        return _v("yt-dlp" if module == "yt_dlp" else module)
    except Exception:  # noqa: BLE001
        return "?"


def check_python_deps() -> list[str]:
    missing: list[str] = []
    for module, package in PY_DEPS:
        try:
            m = importlib.import_module(module)
            print(f"  [OK]   {package:<10} {_module_version(module, m)}")
        except ImportError:
            print(f"  [缺失] {package}")
            missing.append(package)
    return missing


def install_python_deps(missing: list[str]) -> bool:
    if not missing:
        return True
    print(f"\n正在安装缺失的 Python 包: {', '.join(missing)}")
    cmd = [sys.executable, "-m", "pip", "install", *missing]
    return subprocess.run(cmd).returncode == 0


def check_ffmpeg() -> bool:
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        try:
            out = subprocess.run(
                [str(ffmpeg), "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace"
            ).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            out = str(ffmpeg)
        print(f"  [OK]   ffmpeg     {out}")
        # 不再需要 ffprobe：时长改为解析 ffmpeg 自己打印的 Duration
        return True
    print("  [缺失] ffmpeg")
    return False


def install_ffmpeg_winget() -> bool:
    winget = which("winget")
    if not winget:
        print("  未找到 winget，请改用 --fetch-ffmpeg 或手动安装 ffmpeg")
        return False
    print("\n正在用 winget 安装 ffmpeg（Gyan.FFmpeg）...")
    cmd = [
        winget, "install", "--id", "Gyan.FFmpeg", "-e",
        "--accept-package-agreements", "--accept-source-agreements",
        "--disable-interactivity",
    ]
    rc = subprocess.run(cmd).returncode
    print(f"  winget 退出码: {rc}")
    return find_ffmpeg() is not None


# 备用下载源：直接抓 GyanD 构建包，并允许通过镜像前缀绕过 GitHub 直连问题。
FFMPEG_BUILD = "ffmpeg-9.0.2-essentials_build.zip"
FFMPEG_URLS = [
    f"https://www.gyan.dev/ffmpeg/builds/{FFMPEG_BUILD}",
    f"https://ghfast.top/https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/{FFMPEG_BUILD}",
    f"https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/{FFMPEG_BUILD}",
]


def fetch_ffmpeg() -> bool:
    """不使用 winget，直接把 ffmpeg 解压到项目 tools/ffmpeg 下。

    优先级：环境变量 BILIMUSIC_FFMPEG 指定的 zip > 多个候选 URL。
    这样即使 winget 因网络问题失败（本机实测 GitHub 直连超时），
    程序也自带一份可用的 ffmpeg。
    """
    import shutil
    import tempfile
    import urllib.request
    import zipfile

    tools = Path(__file__).resolve().parent / "tools"
    target = tools / "ffmpeg"
    if (target / "ffmpeg.exe").is_file() or (target / "bin" / "ffmpeg.exe").is_file():
        print(f"  ffmpeg 已存在于 {target}")
        return True

    print("\n准备下载 ffmpeg（约 110 MB）...")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        zip_path = td_path / "ffmpeg.zip"
        ok = False
        for url in FFMPEG_URLS:
            print(f"  尝试: {url}")
            try:
                with urllib.request.urlopen(url, timeout=180) as resp, open(zip_path, "wb") as fh:
                    shutil.copyfileobj(resp, fh)
                if zip_path.stat().st_size > 10 * 1024 * 1024:
                    ok = True
                    break
                print(f"    文件过小（{zip_path.stat().st_size} 字节），换下一个源")
            except Exception as exc:  # noqa: BLE001
                print(f"    失败: {exc}")
        if not ok:
            print("  所有下载源均失败。可手动下载 ffmpeg 解压到 tools\\ffmpeg\\，" )
            print("  或设置环境变量 BILIMUSIC_FFMPEG 指向 ffmpeg.exe。")
            return False

        print("  解压中 ...")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(td_path / "x")
        exe = next((p for p in (td_path / "x").rglob("ffmpeg.exe")), None)
        if exe is None:
            print("  压缩包内未找到 ffmpeg.exe")
            return False
        tools.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(exe.parent), str(target))
    found = find_ffmpeg()
    print(f"  ffmpeg 就位: {found}")
    return found is not None


def main() -> int:
    ap = argparse.ArgumentParser(description="BiliMusicTool 环境自检 / 依赖安装")
    ap.add_argument("--auto", action="store_true", help="自动安装缺失依赖（pip + winget）")
    ap.add_argument("--fetch-ffmpeg", action="store_true",
                    help="直接下载 ffmpeg 到项目 tools/ 目录（winget 失败时用）")
    args = ap.parse_args()

    print("=" * 66)
    print(" BiliMusicTool 环境自检")
    print("=" * 66)
    print(f"\nPython: {sys.version.split()[0]}  ({sys.executable})")

    print("\n[1/2] Python 依赖:")
    missing = check_python_deps()
    if missing and args.auto:
        ok = install_python_deps(missing)
        if not ok:
            print("  pip 安装失败，请检查网络或手动执行 requirements.txt")
            return 1
        missing = check_python_deps()

    print("\n[2/2] 外部工具:")
    has_ffmpeg = check_ffmpeg()
    if not has_ffmpeg and args.fetch_ffmpeg:
        has_ffmpeg = fetch_ffmpeg()
    elif not has_ffmpeg and args.auto:
        has_ffmpeg = install_ffmpeg_winget()
        if not has_ffmpeg:
            print("  winget 安装未成功，尝试直接下载 ...")
            has_ffmpeg = fetch_ffmpeg()

    print("\n" + "=" * 66)
    if missing and not args.auto:
        print(" 结论：缺少 Python 依赖。执行  python install_deps.py --auto  自动安装")
    elif not has_ffmpeg:
        print(" 结论：缺少 ffmpeg。任选其一：")
        print("       python install_deps.py --auto          （pip + winget）")
        print("       python install_deps.py --fetch-ffmpeg  （直接下载到 tools/）")
    else:
        print(" 结论：环境就绪，可以开始使用：")
        print('       python -m bilimusic "歌曲名"')
    print("=" * 66)
    return 0 if (has_ffmpeg and not missing) else 1


if __name__ == "__main__":
    raise SystemExit(main())
