"""Web 前端启动入口。

用法：
    python serve.py                 # 默认 http://127.0.0.1:8765/
    python serve.py --port 9000
    python serve.py --open          # 启动后自动打开浏览器
    python serve.py --host 0.0.0.0  # 局域网可访问（注意安全）

关于"输出目录"：
    默认写到 `<启动时所在目录>/output/`。这样从包根目录启动时，
    产物就在用户一眼能看到的地方，而不是藏在 program/ 里面。
    想要固定位置可以用环境变量 BILIMUSIC_OUTPUT 覆盖。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bilimusic.web import serve  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="BiliMusicTool 前端页面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    # 把输出目录指向"启动时所在目录"（通常是包根），方便用户找产物
    if not os.environ.get("BILIMUSIC_OUTPUT"):
        os.environ["BILIMUSIC_OUTPUT"] = str(Path.cwd() / "output")

    try:
        serve(host=args.host, port=args.port, open_browser=args.open)
    except OSError as exc:
        print(f"[错误] 无法监听 {args.host}:{args.port} —— {exc}", file=sys.stderr)
        print("       端口可能被占用，换一个：python serve.py --port 8899", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
