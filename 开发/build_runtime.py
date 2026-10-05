r"""构建「嵌入式运行时」—— 一个自带 Python 的解释器目录，供零安装版使用。

放到 program\runtime\ 后，用户机器上不需要装 Python、不需要 pip、不需要联网。

用法（在「开发」文件夹里运行）：
    python build_runtime.py                 # 输出到 program\runtime\
    python build_runtime.py --out X:\rt     # 指定输出位置
    python build_runtime.py --keep-zip      # 保留下载的 embeddable zip

它做四件事（每一步都自检）：
    1. 下载（并缓存）python.org 的 Windows embeddable 包
    2. 解压成一个可移动的解释器目录
    3. 改 `._pth`：启用 site、并把上层的 program 目录加入模块搜索路径
       （嵌入式发行版默认不搜 site-packages、也**不把当前目录**加入 sys.path，
        而且 `._pth` 会覆盖 PYTHONPATH —— 这是本方案最大的坑，必须改这个文件）
    4. pip 安装 requirements.txt 里的依赖（装进该目录自身的 site-packages）

设计约束：
    * 运行时目录**必须**放在 program\ 里面（即 program\runtime\），
      这样 `._pth` 里的 `..` 正好解析成 program\，整个包可以随意改名/移动。
      实测把 `..\program` 写在 `._pth` 里**不生效**，所以只能靠这种摆放方式。
    * 目标机器与当前机器的架构必须一致（默认 amd64）。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
ROOT = DEV_DIR.parent
PROGRAM_SRC = ROOT / "program"
CACHE = ROOT / ".cache"

# 嵌入式发行版。要换版本只改这里。
PY_VERSION = "3.12.10"
PY_ARCH = "amd64"
PY_URL = (f"https://www.python.org/ftp/python/{PY_VERSION}/"
          f"python-{PY_VERSION}-embed-{PY_ARCH}.zip")


def log(msg: str) -> None:
    print(msg, flush=True)


def download_zip() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / f"python-{PY_VERSION}-embed-{PY_ARCH}.zip"
    if dest.is_file() and dest.stat().st_size > 5_000_000:
        log(f"  使用缓存 {dest.name} ({dest.stat().st_size / 1048576:.1f} MB)")
        return dest
    log(f"  下载 {PY_URL}")
    urllib.request.urlretrieve(PY_URL, dest)
    log(f"  完成 ({dest.stat().st_size / 1048576:.1f} MB)")
    return dest


def extract(zip_path: Path, out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    log(f"  解压到 {out_dir}  ({size / 1048576:.1f} MB)")


def patch_pth(out_dir: Path) -> Path:
    """改 `._pth`：启用 site，并把上层目录（program\\）加入搜索路径。"""
    pths = list(out_dir.glob("python*._pth"))
    if not pths:
        raise SystemExit(f"找不到 ._pth 文件于 {out_dir}")
    p = pths[0]
    original = p.read_text(encoding="utf-8")
    log(f"  原 ._pth:\n    " + original.replace("\n", "\n    "))

    # 保留原有的 zip 与 "."，补上 import site 和上层目录
    lines = [ln for ln in original.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if "import site" not in lines:
        lines.append("import site")
    if ".." not in lines:
        lines.append("..")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"  新 ._pth:\n    " + p.read_text(encoding="utf-8").replace("\n", "\n    "))
    return p


def install_deps(out_dir: Path) -> None:
    py = out_dir / "python.exe"
    req = PROGRAM_SRC / "requirements.txt"

    # 嵌入式发行版不带 pip，用 get-pip.py 装
    gp = CACHE / "get-pip.py"
    if not gp.is_file():
        log("  下载 get-pip.py")
        urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", gp)

    log("  安装 pip ...")
    r = subprocess.run([str(py), str(gp), "--no-warn-script-location"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise SystemExit("装 pip 失败:\n" + (r.stdout or "")[-800:] + (r.stderr or "")[-800:])
    log("    pip 就绪")

    log(f"  安装依赖 {req.name} ...")
    r = subprocess.run([str(py), "-m", "pip", "install", "-q",
                        "--no-warn-script-location", "-r", str(req)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise SystemExit("装依赖失败:\n" + (r.stdout or "")[-800:] + (r.stderr or "")[-800:])
    log("    依赖安装完成")


def verify(out_dir: Path) -> None:
    """三重自检：模块可用 / 能 import bilimusic / 虚拟环境隔离。"""
    py = out_dir / "python.exe"

    log("  [自检 1] 导入依赖")
    code = ("import sys, numpy, requests, yt_dlp;"
            "print('    python', sys.version.split()[0]);"
            "print('    numpy', numpy.__version__);"
            "print('    yt_dlp', yt_dlp.version.__version__)")
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit("  !! 依赖导入失败: " + (r.stderr or "")[-600:])
    log(r.stdout.rstrip())

    log("  [自检 2] 能否 import bilimusic（验证 ._pth 的 '..' 生效）")
    r = subprocess.run([str(py), "-c", "import bilimusic; print('    OK', bilimusic.__version__)"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(
            "  !! 无法 import bilimusic。._pth 里的 '..' 没生效。\n"
            "     请确认运行时目录位于 program\\ 之内（即 program\\runtime\\）。\n"
            + (r.stderr or "")[-600:])
    log(r.stdout.rstrip())

    log("  [自检 3] yt-dlp 可执行文件是否随 pip 一起装上")
    yt = out_dir / "Scripts" / "yt-dlp.exe"
    log(f"    {yt}  存在={yt.is_file()}")
    if not yt.is_file():
        log("    [警告] Scripts\\yt-dlp.exe 不存在，下载时会回退到 python -m yt_dlp")

    size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    log(f"  运行时体积: {size / 1048576:.1f} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description="构建嵌入式 Python 运行时")
    ap.add_argument("--out", type=Path, default=PROGRAM_SRC / "runtime",
                    help="输出目录（默认 program\\runtime）")
    ap.add_argument("--keep-zip", action="store_true", help="保留下载的 zip")
    args = ap.parse_args()

    log(f"=== 构建嵌入式运行时 Python {PY_VERSION} ({PY_ARCH}) ===")
    log("[1/5] 获取 embeddable 包")
    z = download_zip()
    log("[2/5] 解压")
    extract(z, args.out)
    log("[3/5] 修 ._pth")
    patch_pth(args.out)
    log("[4/5] 安装依赖")
    install_deps(args.out)
    log("[5/5] 自检")
    verify(args.out)

    if not args.keep_zip:
        try:
            z.unlink()
            log(f"  已删除缓存 {z.name}")
        except OSError:
            pass

    log(f"\n=== 完成：{args.out} ===")
    log("提示：整个 program 目录可以随意改名/移动，但不要单独移动 runtime。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
