# BiliMusicTool

输入一个歌名，自动完成：**在 B 站搜索 → 下载音频 → 分离曲绘 → 查出 BPM → 在歌曲开头补上「它自己 BPM 的一小节」空隙 → 输出 44.1kHz / 320kbps MP3**。

提供两种用法：**网页界面**（推荐，可搜索选歌、也可导入本地音乐）和**命令行**。

---

## 1. 快速开始

### 方式 A：网页界面

```bat
cd /d D:\BiliMusicTool
python serve.py --open
```

然后浏览器会打开 **http://127.0.0.1:8765/**。页面上有两块：

- **从 B 站搜索下载**：输入歌名 → 看到搜索结果卡片（封面 / 标题 / UP 主 / 时长 / 播放量）→ 点选一条 → 开始处理
- **导入本地音乐**：拖入或选择本地音频文件 → 让程序自动测 BPM，或自己填一个 BPM → 开始处理

也可以在命令行里直接启动：`python -m bilimusic --web --web-open`

### 方式 B：命令行

```bat
run.bat "千本樱"
```

换机器后先跑一次环境自检：

```bat
python install_deps.py --auto
```

（本仓库已经把 ffmpeg 9.0.2 放在 `tools\ffmpeg\`，开箱可用。）

---

## 2. 网页界面说明

### 2.1 搜索下载页

1. 输入歌名，回车或点「搜索」
2. 结果卡片会显示封面、标题、UP 主、时长、播放量
3. 点一张卡片选中（边框变青绿），设置区会显示「已选择：…」
4. 按需调整「处理设置」，点「开始处理」
5. 下方出现进度面板，实时滚动处理日志；完成后显示 BPM 摘要与产物下载/试听

「搜索选项」里可以调：

| 选项 | 说明 |
| --- | --- |
| 结果条数 | 5 / 8 / 12 / 20 |
| 智能排序 | 开启后把「原曲 / MV / 完整版」排在前面，翻唱、伴奏、钢琴版、切片降权 |
| 浏览器 Cookie | 仅用于**下载**会员/受限视频，**不会改变搜索结果**；旁边有「检查」按钮可先确认能不能真读到。详见第 5 节 |

### 2.2 本地导入页

- 支持拖拽，或点击后选择文件；支持 mp3 / m4a / wav / flac / aac / ogg / opus 等，单文件上限 500 MB
- 上传完成后，「处理设置」里选 BPM 来源：
  - **自动检测**：程序对音频做本地分析（完全离线，不请求任何外部服务）
  - **手动指定**：直接填一个 BPM（30–300），跳过检测
- 点「开始处理」，产物同样是「补一小节空隙 + 44.1kHz/320kbps MP3 + 纯音频分离」

### 2.3 处理设置

| 选项 | 说明 |
| --- | --- |
| BPM 来源 | 自动检测（本地分析）/ 手动指定 |
| 每小节拍数 | 4（4/4，最常见）、3（圆舞曲）、6、2 |
| 保留下载的原始音频 | 额外保留 B 站原始音轨（m4a 等） |

### 2.4 完成后可以看到

- **BPM 摘要卡**：最终 BPM、每拍时长、一小节空隙、输出时长、置信度、候选速度、判定依据
- **产物列表**：歌曲（含空隙）、纯音频（无封面）、曲绘、信息清单，每项都有「下载」按钮
- **在线试听**：歌曲与纯音频带内嵌播放器，支持拖动进度（后端实现了 HTTP Range）
- **处理日志**：可折叠，含 ffmpeg 命令与每一步的判定依据

### 2.5 接口一览（想自己接别的界面时用）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | ffmpeg / yt-dlp / 输出目录状态 |
| POST | `/api/search` | `{keyword, count, smart, cookies}` → 搜索结果 |
| POST | `/api/cookies` | `{browser}` → 检查该浏览器的 Cookie 是否真能读到 |
| POST | `/api/download` | 按搜索结果创建下载任务 → `job_id` |
| POST | `/api/upload` | multipart 上传音频 → `upload_id` |
| POST | `/api/process` | 处理上传的文件或服务器本地路径 |
| GET | `/api/job/<id>` | 任务状态、日志、产物 |
| POST | `/api/job/<id>/cancel` | 取消任务（会连同 yt-dlp / ffmpeg 进程树一起终止） |
| GET | `/api/file?job=&kind=&dl=` | 取产物，支持 Range |

---

## 3. BPM 从哪来、怎么取整

**BPM 完全由本地音频分析得出，不请求任何外部服务**（纯离线）。

| 情况 | 处理 |
| --- | --- |
| 本地音频分析（如 153.97 / 128.21） | **一律取整到整数** → 154 / 128；清单里记录取整前的值 |
| 手动指定 | 原样使用，不取整 |

### 为什么删掉了「在线数据库交叉验证」

早期版本查了两个公开 BPM 库，实测**两个都不可用**，已整体移除：

| 曾经的源 | 实测结果 |
| --- | --- |
| `api.getonboard.com` | 域名 DNS 解析失败；`getonboard.com` 返回 DreamHost 的 "Site not found" 停放页 —— **站点已关停** |
| `songbpm.com/searches?q=` | 站点改用 Astro 重构后该路径**恒返回 404** |

也验证过其它候选：GetSongBPM 官方 API 需要付费 key（401）、AcousticBrainz 已下线（404）、
tunebat 与 Deezer 在本机网络不可达、MusicBrainz 根本没有 BPM 字段。
songbpm 的曲目页虽然还能抓到 BPM（如 `@a-ha/take-on-me` → 169），但它是按
`artist/song` 拼 slug 直连、且**只支持拉丁字母**，对 B 站的中文/日文歌名基本命中不了，
收益远不值得维护 —— 所以干脆去掉，让日志和界面不再出现误导性的「在线无结果」。

**这对输出质量没有影响**：本地分析在合成节拍上实测 6/6 零误差（相对误差 < 0.15%），
首拍定位误差 < 5 ms，足够支撑「补一小节空隙」这个用途。

### 为什么本地结果一律取整

本地检测总有零点几个 BPM 的估计误差（实测 < 0.15%）。对 128 BPM 来说 0.15% 就是 ±0.2 BPM，
所以 128.21 这类值几乎必然是误差而非真实速度，取整反而更接近真值。
极端情况下若真值恰为 128.25，取整会带来约 2 ms 的拍长偏差，可以忽略。

界面上「BPM」卡片会标注是否发生过取整（例如「已取整（原值 153.97）」），`info.json` 里有 `取整前` 字段。
`python selftest.py` 会校验「本地结果一律为整数」这条约束。

---

## 4. 常用命令（命令行）

| 命令 | 作用 |
| --- | --- |
| `python -m bilimusic "歌名"` | 搜索并交互式选一条，走完整流程 |
| `python -m bilimusic "歌名" -y` | 自动选第一条（结合智能排序） |
| `python -m bilimusic "歌名" -n 10 --index 3` | 列出 10 条并直接选第 3 条 |
| `python -m bilimusic "歌名" --no-smart` | 关闭智能排序，按 B 站原始顺序 |
| `python -m bilimusic "歌名" --cookies-from-browser edge` | 带上浏览器 cookie（用于会员/受限视频） |
| `python -m bilimusic "歌名" --bpm 174` | 手动指定 BPM |
| `python -m bilimusic "歌名" --beats-per-bar 3` | 一小节按 3 拍算 |
| `python -m bilimusic "歌名" --keep-source` | 保留下载到的原始音频 |
| `python -m bilimusic --from-file D:\song.mp3` | 不下载，直接处理本地音频 |
| `python -m bilimusic --analyze D:\song.mp3` | 只分析 BPM，输出 JSON |
| `python -m bilimusic --web --web-open` | 启动网页界面并打开浏览器 |
| `python serve.py --port 8899` | 换端口启动网页界面 |
| `python selftest.py --full` | 跑算法与音频链路自检 |

命令行和网页走的是**同一套处理流水线**（`bilimusic/pipeline.py`），结果一致。

---

## 5. 关于浏览器 Cookie

### 它有什么用

**只有一个用途：下载会员 / 需要登录的视频。**

- 让 yt-dlp 带上你浏览器里的 `SESSDATA` 等登录凭证去取音频流
- 会员专属、充电专属、部分被限制的视频，匿名请求拿不到音频，带登录态才行

### 它不会做什么（最容易被误解的一点）

- ❌ **不改变搜索结果。** B 站搜索接口本身不按账号个性化排序，所以同一个关键词，
  勾不勾 Cookie 返回的列表完全一样（连顺序都一样，已实测验证）。
  如果你期待「勾了 Cookie 结果变了」，那不是本程序的行为。
- ❌ 不改变 BPM、不改善音质、不影响曲绘。
- ❌ 不能绕过「视频本身没有音频流」这类问题。

### 为什么常常点了没效果

在 Windows 上，**读取浏览器 cookie 本身经常直接失败**，实测本机结果：

| 浏览器 | 结果 |
| --- | --- |
| Edge | `Could not copy Chrome cookie database` —— 浏览器运行时 cookie 库被锁 |
| Chrome | `Failed to decrypt with DPAPI` —— 新版 Chromium 的 App-Bound 加密，yt-dlp 解不开 |
| Firefox | 能读到 cookie，但那个 profile 没登录过 B 站，**没有登录凭证** |

失败后 yt-dlp 是**硬失败**（直接非 0 退出），连普通视频都下不了。
所以程序现在的做法是：

1. 拿到浏览器名后**先探测**能不能真的读到 cookie（有 12 秒超时，防止浏览器锁库把流程卡死）
2. 探测失败 → **自动回落匿名下载**，并在日志里写明失败原因和建议，绝不静默
3. 界面上「检查」按钮可以先单独确认，不必等到下载失败才发现

### 想真正用上 Cookie 该怎么做

1. **先关闭对应浏览器**（cookie 库不再被占用），再点「检查」
2. Chrome 若报 DPAPI 错误：在该浏览器设置里关闭「使用 Windows Hello / 应用绑定加密」
   （新版 Chrome 的 App-Bound Encryption），或改用 Firefox
3. **确认该浏览器已登录 B 站** —— 「检查」按钮会明确告诉你有没有拿到
   `SESSDATA` / `bili_jct` / `DedeUserID`
4. 实在不行，可以手动导出 `cookies.txt`（Netscape 格式），再用 `--cookies` 参数提供

---

## 6. 依赖

| 依赖 | 用途 | 说明 |
| --- | --- | --- |
| Python 3.10+ | 运行 | 本机 3.12；网页后端只用标准库，无需 Flask 等框架 |
| numpy | BPM 算法 | 已在 `requirements.txt` 固定 `<2.0`；**不需要 scipy** |
| requests | B 站接口 | |
| yt-dlp | 音频下载 / 备用搜索 / 读取浏览器 cookie | |
| ffmpeg | 解码/补空隙/编码 MP3/封面转 PNG/取时长 | 项目自带 `tools\ffmpeg\ffmpeg.exe`，**不需要 ffprobe** |

前端是原生 HTML/CSS/JS，**没有 npm、没有构建步骤**，改完文件刷新页面即可。

`bilimusic\config.py` 里 `find_ffmpeg()` 的查找顺序：
环境变量 `BILIMUSIC_FFMPEG` → 项目 `tools\ffmpeg\` → PATH → winget 安装目录。

---

## 7. 产物结构

每首歌一个以歌名命名的目录：

```
output\千本樱\
    info.json                ← 全部元数据与判定依据（BPM、来源、时长等）
    曲绘.png                  ← 从 B 站拿到的上传者原始曲绘（统一转 PNG）
    千本樱.mp3               ← 主产物：开头已补一小节空隙，44.1kHz/320kbps，内嵌曲绘
    千本樱.audio-only.mp3    ← 纯音频（不带封面标签），实现「音频与曲绘分离」
```

`info.json` 里记录了：来源 BV/av 号与链接、UP 主、标签、**本地检测 BPM 与置信度、
最终采用值及理由、取整前的原值**、每拍/每小节时长、补入空隙秒数、源与输出文件时长。

---

## 8. 处理流程

```
歌名 / 本地文件
 └─ 1. 搜索        三条通道依次补齐，按相关度重排（见第 8.1 节）
 └─ 2. 元数据      view 接口取标题/UP主/时长/封面原图/标签（支持 BV 号与 av 号）
 └─ 3. 下载        只取音频流（不下视频）；封面转 PNG 存盘
 └─ 4. BPM         本地音频分析 + 取整规则（纯离线）
 └─ 5. 音频处理    剔源文件自带静音 → 补 1 小节静音 → 重采样 44.1kHz → 320k MP3
 └─ 6. 清单        info.json
```

「补一小节的空隙」的计算方式：

```
每拍时长 = 60 / BPM
一小节   = 每拍时长 × 每小节拍数      （BPM 120 时 = 0.5 × 4 = 2.000 秒）
```

空隙是用数字静音（ffmpeg `adelay`）插在**最前面**的，因此歌曲原本的节拍结构不会被改动；
同时源文件开头若自带静音会先被剔除，保证「空隙」的长度完全由 BPM 决定、可预期。

### 8.1 搜索是怎么做的（三条通道）

按可靠性依次尝试，结果按视频 id 去重合并，不够再补：

| 顺序 | 通道 | 适合 | 提供的字段 |
| --- | --- | --- | --- |
| 1 | **WBI 签名接口** `/x/web-interface/wbi/search/type` | 中文关键词最稳 | BV 号、标题、UP、时长、封面、播放量 |
| 2 | **搜索结果页** `search.bilibili.com/all` | **非中文关键词**（英文/罗马字/假名） | BV 号、标题、UP、时长、封面 |
| 3 | **yt-dlp** flat search | 最后兜底 | **只有 id 和 url** |

实测结论与踩过的坑：

- WBI 接口对**非中文关键词经常返回 0 条**（`flashback riran`、`Never Gonna Give You Up` 都是 0 条），
  所以必须有第 2 条通道，否则就只能落到 yt-dlp
- 搜索结果页是**服务端渲染**的，卡片里字段齐全，实测约 0.5 秒；解析依据
  `bili-video-card__wrap` / `info--tit` / `info--author` / `stats__duration` 这些 class
- yt-dlp 的 flat search **不给标题/UP/封面**，旧版直接展示就成了「(无标题) / 未知 UP / 无封面」。
  现在它只作兜底，且**无标题的条目一律丢弃**，不会再出现残缺卡片
- 搜索页 `title=` 属性里的 HTML 实体是**双层转义**的（`&amp;amp;`、`&amp;#x27;`），
  只解一层会留下可见乱码，需解两层

「智能排序」（原曲/MV 提前、翻唱伴奏降权）在第 1、2 条通道的结果上都会生效。

---

## 9. BPM 检测是怎么做的

只用 **numpy** 自己实现（**刻意不依赖 librosa** —— 它会强制升级 numpy 2.x / numba；**也不需要 scipy** —— 原先用到的 `stft`、`find_peaks`、`resample_poly`、`correlate` 已改为 numpy 等价实现，省下约 85 MB 安装量）。

1. **起音包络**：单声道 → 重采样到 22.05kHz → STFT → 对数压缩谱 → 半波整流谱通量（低频加权、高频压制）→ 轻平滑。
2. **起音事件**：包络局部极大 + 能量质心做亚帧细化；再补偿谱通量固定的时间偏置（常量 `_ONSET_LAG_FRAMES = 3.0` 帧，由合成节拍曲标定，残余偏差 < 0.3ms）。
3. **速度**：对每一对起音事件，把跨越的拍数取整后求隐含周期 = 时间差 / 拍数，取中位数；**再用上一轮结果重算拍数并迭代**，从而剔除取整边界造成的 ±半拍错位对。合成节拍上实测可精确复原 BPM（相对误差 0，置信度 ~0.6）。
4. **倍频歧义**：先用自相关给出提示值，并对 2× / 0.5× / 3× 等关系加权投票，避免把 174 判成 87 或把它判成 348。
5. **首拍相位**：事件时刻对拍周期取模后做加权循环均值（自动排除不落在网格上的干扰事件）。合成测试中定位误差 **< 5 ms（约 1% 拍）**。
6. **取整**：本地结果四舍五入到整数（理由见第 3 节）。

自检结果（`python selftest.py --full`）：

```
BPM 准确度：真实 90/100/120/128/150/174 → 检测全部 0.00% 误差
首拍相位：  误差 4.4~4.6 ms（约 1% 拍）
取整规则：  8 项用例全通过，auto 模式输出全部为整数
补空隙链路：源 10.000s + 1小节 1.600s → 输出 11.600s，偏差 0.000s
```

---

## 10. 常见问题

**Q：网页打不开 / 端口被占用？**
换端口：`python serve.py --port 8899`。

**Q：搜索很慢或一直转圈？**
已做过这些防护，正常情况下中文关键词 1~2 秒、英文关键词 1~3 秒：

| 防护 | 说明 |
| --- | --- |
| 整体超时 | 一次搜索最多 45 秒，超时直接报错并说明可能原因，不会无限转圈 |
| 前端秒数提示 | 等待时状态栏会走秒，超过 8 秒会提示「B 站可能在限流」 |
| 并发去重 | 同一关键词若已有搜索在跑，直接复用，不重复发请求 |
| yt-dlp 通道限时 | 备用搜索通道单独限时，不会拖死整个搜索 |
| Cookie 不阻塞 | 读浏览器 Cookie 改为后台进行 + 结果缓存，搜索**不再**为此等待 |

若仍长时间等待，多半是 B 站在限流。建议：等几分钟再试、把「结果条数」调小、
或在「浏览器 Cookie」里选一个已登录 B 站的浏览器。

**Q：搜索报 `HTTP Error 412: Precondition Failed`？**
B 站对匿名搜索有风控。程序已用 WBI 签名接口并带 `--referer` 规避大部分情况。
仍失败时：在「搜索选项 → 浏览器 Cookie」里选你登录过 B 站的浏览器，或稍等几分钟再试。
注意：**不要**给 yt-dlp 覆盖 `User-Agent`，B 站 WBI 签名把 UA 纳入计算，覆盖后必然 412。

**Q：勾了浏览器 Cookie，搜索结果没变化？**
这是**正常的** —— Cookie 不影响搜索，只影响能否下载会员/受限视频。详见第 5 节。

**Q：勾了 Cookie 之后下载反而失败？**
本程序在下载前会先探测 Cookie，探测失败会自动回落匿名并说明原因，因此不会因 Cookie 而全盘失败。
若日志显示回落后仍失败，那多半是视频本身需要登录，请按第 5 节先修好 Cookie 读取。

**Q：英文/日文歌名搜不到？**
已支持。搜索有三条通道（详见第 8.1 节），其中「搜索结果页」通道对非中文关键词效果很好，
实测 `flashback riran`、`Never Gonna Give You Up` 都能在 1 秒左右返回带标题/UP/封面/时长的结果。
若某个关键词确实没结果，可试试更短的词，或加中文译名。

**Q：结果卡片显示「(无标题) / 未知 UP / 无封面」？**
这是早先版本的问题，已修复。原因是 yt-dlp 的 flat 搜索**只返回视频 id、没有标题/UP/封面**，
旧版把它当成正式结果展示了。现在：新增了能拿全字段的「搜索结果页」通道，
并且**会丢弃没有任何标题的条目**，不会再出现这种残缺卡片。

**Q：BPM 明显不对怎么办？**
先看 BPM 卡片里的「候选速度」是否包含正确值 —— BPM 的倍频/半频是天然歧义。
在「处理设置」里改成「手动指定 BPM」填正确值重跑即可。

**Q：上传大文件失败？**
单文件上限 500 MB。超过的话先用别的工具转成 mp3 再导入。

**Q：提示未取得曲绘？**
说明详情接口失败（此时程序会用搜索结果里的封面兜底）。通常是风控，换 Cookie 重试即可。

**Q：任务取消后进度条卡住？**
取消会终止 yt-dlp / ffmpeg 整棵进程树，状态随即变为「已取消」，稍等一两秒刷新即可。

**Q：网页服务重启后看不到之前的任务了？**
任务记录保存在内存里，重启即清空；但**产物文件仍在 output 目录**，可以直接去那里取。

---

## 11. 打包给别的电脑用

```bat
cd 开发
python package.py            :: 一次产出版本 A 和版本 B
python package.py --lite     :: 只出版本 A
python package.py --embed    :: 只出版本 B
```

会产出**两个版本**，给不同的人：

| | 版本 A（轻量版） | 版本 B（零安装版） |
| --- | --- | --- |
| 目录 | `BiliMusicTool-1.0.0\` | `BiliMusicTool-1.0.0-standalone\` |
| 解压后大小 | 约 101 MB | 约 195 MB |
| zip | 约 37 MB | 约 69 MB |
| 自带 Python | ❌ | ✅（`program\runtime\`，依赖已装好） |
| 目标机器要装什么 | **Python 3.10+** | **什么都不用装** |
| 首次运行要联网吗 | 要（pip 装约 88 MB 依赖） | **不要** |
| 适合谁 | 自己用、或对方懂电脑 | **完全不懂电脑的人** |

两者目录结构一样，都自带 ffmpeg，功能完全一致：

```
BiliMusicTool-1.0.0[-standalone]\
    启动程序.bat            唯一入口（双击 -> 菜单）
    使用说明.txt            零安装版用「什么都不用装」那一版措辞
    output\                 产物
    program\
        bilimusic\ web\ tools\ffmpeg\
        serve.py install_deps.py selftest.py requirements.txt README.md
        runtime\            ← 仅零安装版：嵌入式 Python 3.12.10
    开发\                   命令行模式.bat / run.bat
```

### 版本 B 是怎么做出来的

```bat
cd 开发
python build_runtime.py      :: 生成 program\runtime\（约 121 MB）
python package.py --embed    :: 带上它打包
```

`build_runtime.py` 会下载 python.org 的 Windows embeddable 包、解压、
改 `._pth`、再用该解释器 pip 装 `requirements.txt`，最后做三重自检。

### 嵌入式 Python 的三个坑（都已解决）

这三个坑是这套方案能不能用的关键，记下来免得以后重踩：

1. **`._pth` 会禁掉「当前目录进 sys.path」**
   嵌入式发行版不执行常规的 `site` 初始化，`python -m bilimusic` 会报
   `No module named bilimusic`。必须把路径写进 `._pth`。
2. **`._pth` 里的 `..` 相对路径不生效**
   实测写 `..\program` 无效，写绝对路径有效但包就不能移动了。
   **解决**：把运行时放在 `program\runtime\`，于是 `._pth` 里写 `..`
   正好解析成 `program\`，整个包可以随意改名/移动（已实测）。
3. **`._pth` 会覆盖 `PYTHONPATH`**
   所以不能靠环境变量补路径，只能改 `._pth` 本身。

另外嵌入式发行版**不带 pip**（要先用 `get-pip.py` 装），
且默认不搜 `site-packages`（要取消 `._pth` 里 `import site` 的注释）。

### 目标机器需要什么

**版本 A**：

| 需要 | 说明 |
| --- | --- |
| Windows | 已在 Windows 10/11 上验证 |
| **Python 3.10+** | 唯一需要用户自己装的东西。启动器会自动检测并给出带网址的提示 |
| 联网 | 首次运行装依赖、之后搜歌下载都需要（BPM 检测本身不需要联网） |

**版本 B**：只需要 Windows 与联网（联网仅用于搜歌下载，**首次启动不需要**）。

**两个版本都不需要**用户另外装：ffmpeg（已打包 `tools\ffmpeg\`，纯静态构建无外部 DLL 依赖）、
yt-dlp、numpy/requests、任何 Node.js 或前端工具链。

### 关于 PyInstaller（评估过，未采用）

试过用 PyInstaller 打单 exe（实测 size 57.8 MB），但**冻结后下载功能会失效**：

| 测试项 | 结果 |
| --- | --- |
| 冻结后调 ffmpeg（含 `_MEIPASS` 路径） | ✅ 正常 |
| `shutil.which("yt-dlp")` 找外部 yt-dlp | ✅ 正常 |
| **`sys.executable -m yt_dlp`** | ❌ **失效** —— 冻结后 `sys.executable` 指向 exe 自己，变成递归调用，卡满超时 |
| `import yt_dlp` 当模块用 | ❌ `ModuleNotFoundError: No module named 'unicodedata'` |

本程序正是用 `sys.executable -m yt_dlp` 调 yt-dlp 的，所以要么重构下载逻辑
（改成进程内调用），要么就用嵌入式 Python 方案。后者改动小得多，故选它。

### 已经做过的可移植性处理

- **代码里没有任何硬编码绝对路径**：项目根目录由 `Path(__file__)` 推导，ffmpeg 相对项目定位
- **路径含空格和中文也没问题**：已实测在 `...\portable test\我的工具 副本\` 下完整跑通
- **`.bat` 启动器全 ASCII + CRLF 行尾**：cmd.exe 按控制台 OEM 代码页解析 bat，
  非 ASCII 内容在其它区域的 Windows 上会解析错乱；CRLF 则是 cmd 解析块语句的要求。
  中文只保留在**文件名**里（NTFS 用 UTF-16 存名字，安全）
- **Python 输出走 UTF-8**：启动器设置 `PYTHONUTF8=1` 与 `chcp 65001`，中文提示不乱码
- **pip 慢时的镜像**：启动器失败提示里给了清华源命令

### 已知限制

- 需要 Python，**不是**单文件 exe。想彻底免 Python 可以用 PyInstaller 打包，但体积和
  首次启动都会明显变差，且 ffmpeg 仍需外置，所以没有采用
- 首次运行要等 pip 装依赖（一两分钟）
- B 站对匿名搜索有风控，同一网络下高频使用可能需要配浏览器 Cookie

---

## 12. 目录结构

### 12.1 开发时的源码目录

```
BiliMusicTool\                  ← 项目根（非 git 仓库）
    启动程序.bat                 ← 唯一入口：双击 -> 菜单 -> 网页/命令行/自检
    使用说明.txt                 ← 给完全不懂电脑的人看的说明
    output\                      ← 产物落这里（启动器的 BILIMUSIC_OUTPUT 指向它）
    program\                     ← 程序本体
        serve.py                网页界面启动入口
        install_deps.py         环境自检 / 依赖安装 / 下载 ffmpeg
        selftest.py             算法与链路自检（含 BPM 取整规则校验）
        requirements.txt
        README.md               本文件
        web\                    前端（原生 HTML/CSS/JS，无构建）
            index.html
            style.css
            app.js
            favicon.svg
        tools\ffmpeg\           自带 ffmpeg（开箱可用，不含 ffprobe）
        bilimusic\
            __main__.py         python -m bilimusic 入口
            cli.py              命令行、交互、参数解析
            web.py              网页后端：HTTP 服务、任务引擎、上传、产物托管
            pipeline.py         共享处理流水线（CLI 与网页共用）
            cookies.py          浏览器 Cookie 可用性探测（含超时保护与失败解释）
            config.py           路径/常量/ffmpeg 定位/可取消子进程/文件名清洗
            bili.py             搜索（三通道）、元数据、下载音频、下载曲绘
            bili_api.py         B 站 WBI 签名会话、搜索、详情、标签
            bpm_detector.py     BPM 检测（只用 numpy，纯离线）
            audio_processor.py  静音检测、补空隙、重采样、MP3 编码、纯音频分离
    开发\                        ← 维护者与进阶工具
        package.py              打包成两个版本（输出到 项目根\dist）
        build_runtime.py        生成 program\runtime\（嵌入式 Python）
        run.bat                 命令行入口（短名）
        命令行模式.bat           命令行入口（中文名）
        使用说明.txt             说明源文件（轻量版）
        使用说明-零安装版.txt    说明源文件（零安装版）
    dist\                       打包输出（可分发给别人，不属于源码）
```

### 12.2 分发给别人的样子

`package.py` 打出来的包**根目录只放"一个入口 + 一份说明"**，其余收进子目录，
避免新手面对一堆文件不知点哪个：

```
BiliMusicTool-1.0.0[-standalone]\
    启动程序.bat                ← 唯一需要双击的文件
    使用说明.txt
    output\                     ← 产物，含一份说明文件
    program\                    ← 程序本体
        runtime\                ← 仅零安装版：嵌入式 Python
    开发\                       ← 进阶：命令行模式.bat / run.bat
```

关于这个结构的两个实现要点：

- **产物目录**：启动器设置环境变量 `BILIMUSIC_OUTPUT=<包根>\output`，
  `config.py` 优先读它，所以产物出现在包根而不是 `program\` 里面
- **启动器必须纯 ASCII + CRLF**：cmd.exe 按控制台 OEM 代码页解析 `.bat`，
  非 ASCII 内容在别的 Windows 区域会解析错乱；`if (...)` 块内的字面量 `)`
  还必须写成 `^)`。中文只留在**文件名**里（NTFS 用 UTF-16 存名字，安全）
