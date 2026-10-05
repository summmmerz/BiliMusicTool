# BiliMusicTool

输入一个歌名，自动完成整条流水线：

> **在 B 站搜索 → 下载音频 → 分离曲绘 → 检测 BPM → 在歌曲开头补一个「它自己 BPM 的一小节」纯静音空隙 → 输出 44.1kHz / 320kbps MP3**

也可以反过来：**拖入本地音乐**，程序自己测 BPM（或者你手动填一个），再走同样的处理。

两种用法：**网页界面**（推荐）和**命令行**。

## 快速开始

双击 **`启动程序.bat`**，或者：

```bat
cd /d D:\BiliMusicTool
python program\serve.py --open
```

浏览器会打开 <http://127.0.0.1:8765/>。命令行用法：

```bat
program\开发\run.bat "千本樱"
python -m bilimusic "千本樱" --cookies-from-browser edge
```

## 文档

| 文档 | 内容 |
|---|---|
| [program/README.md](program/README.md) | 完整文档：两种用法、HTTP API、Cookie 机制、全部 FAQ、实现细节 |
| [使用说明.txt](使用说明.txt) | 面向完全不懂电脑的人的中文说明 |

## 实现里的几个关键点

- **搜索走 WBI 签名接口**：老接口 `/x/web-interface/search/type` 对匿名请求直接 412，必须签名（mixin key 由 nav 接口的 `img_url`/`sub_url` 重排取前 32 位，参数排序 + `wts` 后拼 key 取 md5 作 `w_rid`）。
- **英文歌名兜底**：WBI 搜索基本只覆盖中文关键词，英文歌名会返回 0 条，因此补了一路 `yt-dlp bilisearch`。
- **给 yt-dlp 加 `User-Agent` 反而会 412**（UA 参与 WBI 签名），只加 `--referer https://www.bilibili.com/`。
- **不引入 librosa / scipy**：librosa 会强升 numpy 2.x 并拖进 numba，scipy 装完约 85 MB 只为几个 DSP 调用——这些调用都用 numpy 重写了。依赖只有 `numpy<2` / `requests` / `yt-dlp`。
- **前端零构建**：原生 HTML/CSS/JS，后端只用 Python 标准库。
- **bat 全 ASCII + CRLF**：cmd.exe 按控制台 OEM 代码页解析 `.bat`，中文会乱码；靠 `chcp 65001` 救不了，因为解析在 chcp 生效之前就开始了。

## 依赖

- Python 3.10+
- `numpy>=1.24,<2.0`、`requests`、`yt-dlp`（见 `program/requirements.txt`）
- `ffmpeg`（音视频处理）

仓库内不含 Python 运行时与 ffmpeg 二进制（`.gitignore` 已排除 `program/runtime/`、`program/tools/`），首次运行由启动脚本自行准备。

## 许可

[MIT](LICENSE)
