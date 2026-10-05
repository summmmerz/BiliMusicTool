r"""下载环境诊断：在出问题的电脑上运行，把结果整段发回即可定位。

用法：
    在 program 目录里执行：  python 诊断下载.py
    或指定一个 BV 号：        python 诊断下载.py BV1Uwa46BE61

它会逐项检查并打印报告：
    1. 运行环境（Python / 架构 / 路径）
    2. 所需组件是否齐全（numpy / requests / yt-dlp / ffmpeg）
    3. 输出目录与临时目录是否可写
    4. 网络能否连上 B 站（含 TLS）
    5. 用 yt-dlp 真实下载一次音频，**把完整输出都打印出来**

报告全部是 ASCII 标签 + 中文说明，可直接复制回传。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 打包后本脚本位于 program\（与 bilimusic 同级）；
# 在源码树里则位于 开发\，此时 bilimusic 在 ..\program\。
for _cand in (HERE, HERE.parent / "program"):
    if (_cand / "bilimusic").is_dir():
        sys.path.insert(0, str(_cand))
        break
else:
    sys.path.insert(0, str(HERE))

OK = "[ OK ]"
BAD = "[FAIL]"
WARN = "[WARN]"


def hdr(t: str) -> None:
    print(f"\n{'=' * 62}\n {t}\n{'=' * 62}")


def main() -> int:
    bv = sys.argv[1] if len(sys.argv) > 1 else "BV1Uwa46BE61"
    url = bv if bv.startswith("http") else f"https://www.bilibili.com/video/{bv}"

    hdr("1. 运行环境")
    print(f"  {OK} Python {sys.version.split()[0]}")
    print(f"  {OK} 解释器 {sys.executable}")
    print(f"  {OK} 架构 {'64位' if sys.maxsize > 2**32 else '32位'}  "
          f"平台 {sys.platform}")
    print(f"  {OK} 冻结打包 {bool(getattr(sys, 'frozen', False))}")
    print(f"  {OK} 当前目录 {Path.cwd()}")
    print(f"  {OK} 程序目录 {HERE}")

    hdr("2. 组件检查")
    for mod, label in (("numpy", "numpy"), ("requests", "requests"), ("yt_dlp", "yt-dlp")):
        try:
            m = __import__(mod)
            v = getattr(getattr(m, "version", None), "__version__", None) or \
                getattr(m, "__version__", "?")
            print(f"  {OK} {label} {v}")
        except Exception as e:  # noqa: BLE001
            print(f"  {BAD} {label} 导入失败：{type(e).__name__}: {e}")

    from bilimusic.config import find_ffmpeg
    ff = find_ffmpeg()
    print(f"  {OK if ff else BAD} ffmpeg {ff or '未找到'}")
    if ff:
        r = subprocess.run([str(ff), "-version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        print(f"       {r.stdout.splitlines()[0] if r.stdout else r.stderr[:120]}")

    # 关键：必须测「程序实际会用的那一个 yt-dlp」。
    # 之前这里固定用 python -m yt_dlp，而程序在有 yt-dlp.exe 时优先用它，
    # 于是出现"诊断成功、实际失败"的假阴性。现在两边用完全相同的选择逻辑。
    from bilimusic.bili import _ytdlp_base, ytdlp_version_info
    print(f"  {OK} 程序实际会用: {ytdlp_version_info()}")
    base_cmd = _ytdlp_base(None)
    probe = base_cmd[:1] + ["--version"]
    r = subprocess.run(probe, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    print(f"       {base_cmd[0]} --version -> "
          f"rc={r.returncode} out={(r.stdout or '').strip()[:60]!r} "
          f"err={(r.stderr or '').strip()[:120]!r}")
    if not (r.stdout or "").strip():
        print(f"  {WARN} 这个 yt-dlp 连版本号都吐不出来，很可能是它本身有问题")
        print(f"       对照测试 python -m yt_dlp：")
        r2 = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=30)
        print(f"       rc={r2.returncode} out={(r2.stdout or '').strip()[:60]!r} "
              f"err={(r2.stderr or '').strip()[:120]!r}")

    hdr("3. 目录可写性")
    from bilimusic.config import DEFAULT_OUTPUT_ROOT
    for d in (DEFAULT_OUTPUT_ROOT, Path(tempfile.gettempdir())):
        try:
            d.mkdir(parents=True, exist_ok=True)
            f = d / ".bmt_write_test"
            f.write_text("x", encoding="utf-8")
            f.unlink()
            print(f"  {OK} 可写 {d}")
        except Exception as e:  # noqa: BLE001
            print(f"  {BAD} 不可写 {d} -> {type(e).__name__}: {e}")

    hdr("4. 网络连通性")
    for host, label in (("https://www.bilibili.com/", "主站"),
                        ("https://api.bilibili.com/x/web-interface/nav", "API"),
                        ("https://upos-sz-mirrorcos.bilivideo.com/", "音频CDN")):
        t0 = time.time()
        try:
            import requests
            r = requests.get(host, timeout=12, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            print(f"  {OK} {label} HTTP {r.status_code}  {time.time()-t0:.1f}s")
        except Exception as e:  # noqa: BLE001
            print(f"  {BAD} {label} {type(e).__name__}: {str(e)[:160]}")

    hdr("5. 真实下载测试（完整输出）")
    print(f"  视频: {url}")
    tmp = Path(tempfile.mkdtemp(prefix="bmt_diag_"))
    print(f"  临时目录: {tmp}")
    # 用程序自己的前缀，保证和界面下载走完全相同的可执行文件与参数
    cmd = list(_ytdlp_base(None))
    print(f"  使用 yt-dlp: {cmd[0]}")
    cmd += ["-f", "ba/bestaudio", "-x", "--audio-format", "best",
            "--no-part", "--print", "after_move:filepath",
            "--newline", "-o", str(tmp / "probe.%(ext)s"), url]
    print("  命令:\n    " + " ".join(cmd))
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        print(f"\n  退出码: {r.returncode}   耗时 {time.time()-t0:.1f}s")
        print(f"  --- stdout ({len(r.stdout or '')} 字符) ---")
        print((r.stdout or "(空)")[-3000:])
        print(f"  --- stderr ({len(r.stderr or '')} 字符) ---")
        print((r.stderr or "(空)")[-3000:])
    except Exception as e:  # noqa: BLE001
        print(f"  {BAD} 调用异常: {type(e).__name__}: {e}")
        traceback.print_exc()

    files = [p for p in tmp.iterdir() if p.is_file()]
    print(f"\n  下载目录里的文件: {[p.name for p in files] or '(无)'}")
    if files:
        print(f"  {OK} 下载成功！说明这条链路是通的，问题可能在界面/配置层。")
    else:
        print(f"  {BAD} 没有下载到任何文件 —— 请把上面这份完整报告发回。")

    try:
        for p in tmp.iterdir():
            p.unlink()
        tmp.rmdir()
    except Exception:  # noqa: BLE001
        pass

    hdr("报告结束")
    print("  请把以上内容整段复制回传。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
