"""B 站检索、元数据获取、音频与曲绘（封面）下载。

检索与下载统一走 yt-dlp（它内部处理了 B 站 WBI 签名等反爬细节），
封面原图走公开的 web-interface/view 接口，拿到的 pic 是上传者提供的原始曲绘。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import requests

from .config import (
    BILI_REFERER,
    DEFAULT_SEARCH_COUNT,
    HTTP_TIMEOUT,
    UA,
    ensure_dirs,
    find_ffmpeg,
    which,
)

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# 搜索整体超时（秒）。B 站偶发慢响应或风控限流时，
# 没有这个上限用户就会看到"一直转圈"。
SEARCH_TOTAL_TIMEOUT = 45.0

# 同一个关键词正在进行的搜索，用于并发去重
_INFLIGHT: dict[str, "_Inflight"] = {}
_INFLIGHT_LOCK = threading.Lock()

# 已经过探测、确认可用的浏览器名缓存（避免每次搜索都重新读浏览器 cookie）
_RESOLVED_BROWSERS: dict[str, str | None] = {}


def _resolved_browser(browser: str | None) -> str | None:
    """把用户选的浏览器名，解析成"确实能给 yt-dlp 用"的名字。

    为什么要这一步：yt-dlp 自己也读浏览器 cookie，读不到（库被锁 / 加密解不开）
    就**硬失败退出**，于是搜索直接报错。所以必须先用 cookies 模块探测，
    探测不通过就返回 None，让 yt-dlp 走匿名——而不是带着一个用不了的浏览器名去跑。
    """
    if not browser:
        return None
    if browser in _RESOLVED_BROWSERS:
        return _RESOLVED_BROWSERS[browser]
    try:
        from .cookies import probe_browser_cookies

        probe = probe_browser_cookies(browser, use_cache=True, wait=False)
        resolved = browser if probe.ok else None
    except Exception:  # noqa: BLE001
        resolved = None
    _RESOLVED_BROWSERS[browser] = resolved
    return resolved


class BiliError(RuntimeError):
    pass


_YTDLP_VERSION_CACHE: str | None = None


def ytdlp_version_info() -> str:
    """返回「yt-dlp 来源 + 版本」的可读字符串，只探测一次。

    为什么要打这行日志：B 站接口会变，**旧版 yt-dlp 是"搜索正常但下载失败"
    的经典原因**。零安装版的 yt-dlp.exe 放在 runtime\\Scripts\\ 下、不在 PATH 里，
    日志里看不到版本就很难判断；这里主动探测并缓存，让每次运行都留下痕迹。
    """
    global _YTDLP_VERSION_CACHE
    if _YTDLP_VERSION_CACHE is not None:
        return _YTDLP_VERSION_CACHE
    exe = which("yt-dlp")
    if exe:
        source, probe = exe, [exe, "--version"]
    else:
        source, probe = f"{sys.executable} -m yt_dlp", [sys.executable, "-m", "yt_dlp", "--version"]
    try:
        r = subprocess.run(probe, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=25)
        ver = (r.stdout or r.stderr or "").strip().splitlines()[-1:] or ["?"]
        ver = ver[0][:60] if ver else "?"
    except Exception as exc:  # noqa: BLE001
        ver = f"探测失败({type(exc).__name__})"
    _YTDLP_VERSION_CACHE = f"{ver}  [{source}]"
    return _YTDLP_VERSION_CACHE


def _ytdlp_base(cookies_from_browser: str | None = None) -> list[str]:
    """构造 yt-dlp 调用前缀；优先使用当前 Python 的 -m yt_dlp，保证与安装环境一致。"""
    exe = which("yt-dlp")
    if exe:
        cmd = [exe]
    else:
        cmd = [sys.executable, "-m", "yt_dlp"]
    cmd += [
        "--no-warnings",
        "--no-playlist",
        "--ignore-config",
        "--no-check-certificates",
        # 只加 Referer。注意：**不要**覆盖 User-Agent —— B 站的 WBI 签名
        # 把 UA 纳入计算，覆盖后 yt-dlp 会拿不到搜索结果（实测 HTTP 412）。
        "--referer", BILI_REFERER,
    ]
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        cmd += ["--ffmpeg-location", str(ffmpeg.parent)]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    return cmd


@dataclass
class SearchResult:
    index: int
    video_id: str
    title: str
    uploader: str
    duration: int | None
    url: str
    view_count: int | None = None
    thumbnail: str | None = None

    @property
    def bvid(self) -> str:
        """兼容旧字段名；实际可能是 BV 号或纯数字 av 号。"""
        return self.video_id

    @property
    def id_kind(self) -> str:
        vid = self.video_id.lower()
        if vid.startswith("bv"):
            return "BV"
        if vid.startswith("av") or vid.isdigit():
            return "av"
        return "?"

    @property
    def duration_text(self) -> str:
        if not self.duration:
            return "?:??"
        m, s = divmod(int(self.duration), 60)
        return f"{m}:{s:02d}"


@dataclass
class TrackInfo:
    video_id: str
    title: str
    uploader: str
    url: str
    duration: float | None = None
    cover_url: str | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def bvid(self) -> str:
        return self.video_id


def _run_json(cmd: list[str], log: LogFn | None = None, cancel_event=None,
              timeout: float = 30.0) -> list[dict]:
    from .config import run_cancellable

    log = log or _noop
    log("执行: " + " ".join(cmd[:4]) + " ...")
    rc, out, err = run_cancellable(cmd, cancel_event=cancel_event, timeout=timeout)
    if rc != 0 and not out.strip():
        raise BiliError(
            "yt-dlp 调用失败（退出码 %s）：\n%s" % (rc, (err or "").strip()[-1500:])
        )
    records: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _norm(s: str) -> str:
    """归一化字符串：只留小写字母数字与汉字，用于标题相似度比较。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())


def _relevance_score(r: SearchResult, keyword: str) -> float:
    """给搜索结果打分，用于把「原曲 / MV / 完整版」排在翻唱、伴奏、切片前面。

    打分规则（经验值，只影响排序不影响能否搜到）：
      + 标题包含完整关键词、时长 2~8 分钟        —— 更像正式作品
      + 标题含 原曲/原版/MV/PV/完整版/官方/投稿     —— 更像官方或完整投稿
      - 标题含 翻唱/cover/伴奏/纯音乐/翻弹/instrumental/karaoke/教程/教学
      - 时长 < 90 秒（多为切片、试听、短版）
    """
    text = (r.title or "").lower()
    score = 0.0
    norm_title = _norm(text)
    norm_kw = _norm(keyword)
    if norm_kw:
        if norm_kw in norm_title:
            score += 3.0
        elif len(norm_kw) >= 3 and norm_kw[: max(2, len(norm_kw) // 2)] in norm_title:
            score += 1.0
    for token in ("原曲", "原版", "mv", "pv", "完整版", "官方", "投稿", "无损", "高音质", "full"):
        if token in text:
            score += 1.5
    for token in ("翻唱", "cover", "伴奏", "纯音乐", "翻弹", "instrumental",
                  "karaoke", "教程", "教学", "钢琴", "吉他", "古筝", "笛", "指弹"):
        if token in text:
            score -= 3.0
    d = r.duration or 0
    if d:
        if d < 90:
            score -= 4.0
        elif 120 <= d <= 480:
            score += 1.5
    if r.view_count:
        import math

        score += min(2.0, math.log10(max(r.view_count, 1)) / 3.5)
    return score


def rank_results(results: list[SearchResult], keyword: str) -> list[SearchResult]:
    """按启发式相关度重排，并刷新 index（保持 1..n 连续）。"""
    ordered = sorted(results, key=lambda r: _relevance_score(r, keyword), reverse=True)
    for i, r in enumerate(ordered, 1):
        r.index = i
    return ordered


def search_bilibili(
    keyword: str,
    count: int = DEFAULT_SEARCH_COUNT,
    cookies_from_browser: str | None = None,
    log: LogFn | None = None,
    heuristic: bool = False,
    total_timeout: float = SEARCH_TOTAL_TIMEOUT,
) -> list[SearchResult]:
    """在 B 站搜索歌曲，返回前 count 条结果。

    走"双通道，谁有结果用谁"的策略：
      1. WBI 签名的 Web API —— 中文关键词效果好、字段完整（含 BV 号）
      2. yt-dlp 的 bilisearch —— 对英文/拉丁关键词更有效（返回的是 av 号）
    两条通道的结果按视频 id 去重后合并，API 结果优先。

    两处防卡措施：
      * `total_timeout`：整体加超时。任何一步（拿 WBI 密钥、搜索请求、
        yt-dlp 子进程）慢过这个时间就整体放弃并报错，不会无限转圈。
      * **同关键词并发去重**：同一个词如果还有一次搜索在跑，这一次直接等它，
        不重复发请求（前端连点、或一次失败后立刻重试都能受益）。

    heuristic=True 时按「原曲/MV 优先、翻唱/伴奏降权」重排。
    """
    keyword = keyword.strip()
    if not keyword:
        raise BiliError("搜索关键词为空")
    log = log or _noop

    with _INFLIGHT_LOCK:
        inflight = _INFLIGHT.get(keyword)
        leader = inflight is None
        if leader:
            inflight = _Inflight()
            _INFLIGHT[keyword] = inflight

    if not leader:
        log(f"「{keyword}」已有一次搜索在进行，复用它 ...")
        if not inflight.done.wait(total_timeout):
            raise BiliError(f"搜索「{keyword}」超时（{total_timeout:.0f} 秒）")
        if inflight.error:
            raise inflight.error
        return _finalize(list(inflight.results), count, keyword, heuristic)

    try:
        results = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_search_impl, keyword, count, cookies_from_browser, log)
            try:
                results = future.result(timeout=total_timeout)
            except FuturesTimeout as exc:
                # 不等待底层线程收尾；它最多再跑完自己的 HTTP 超时就结束了
                raise BiliError(
                    f"搜索「{keyword}」超过 {total_timeout:.0f} 秒未完成，已放弃。\n"
                    "可能原因：网络慢、B 站风控限流、或 yt-dlp 卡住。\n"
                    "建议：稍后重试、换更短的关键词，或在搜索选项里选一个已登录的浏览器。"
                ) from exc
        inflight.results = results
        return _finalize(results, count, keyword, heuristic)
    except Exception as exc:  # noqa: BLE001
        inflight.error = exc if isinstance(exc, BiliError) else BiliError(str(exc))
        raise
    finally:
        inflight.done.set()
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(keyword, None)


class _Inflight:
    """同一个关键词正在进行的搜索，供并发请求复用。"""

    __slots__ = ("done", "results", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.results: list[SearchResult] = []
        self.error: Exception | None = None


def _finalize(
    results: list[SearchResult],
    count: int,
    keyword: str,
    heuristic: bool,
) -> list[SearchResult]:
    merged: list[SearchResult] = []
    seen: set[str] = set()

    def _key(item: SearchResult) -> str:
        vid = item.video_id.lower()
        return vid[2:] if vid.startswith(("bv", "av")) else vid

    for item in results:
        k = _key(item)
        if not k or k in seen:
            continue
        seen.add(k)
        merged.append(item)
        if len(merged) >= count:
            break

    for i, r in enumerate(merged, 1):
        r.index = i
    if heuristic:
        merged = rank_results(merged, keyword)
    return merged


def _search_impl(
    keyword: str,
    count: int,
    cookies_from_browser: str | None,
    log: LogFn,
) -> list[SearchResult]:
    """真正的搜索逻辑，三条通道按可靠性依次补齐。

    1. **WBI 接口**：中文关键词最稳，字段完整（含 BV 号、播放量）
    2. **搜索结果页**：服务端渲染，非中文关键词也能拿到标题/UP/封面/时长
    3. **yt-dlp**：只作最后兜底。它 flat search **只给 id 和 url，没有元数据**，
       拿到的条目在界面上会显示成「(无标题) / 未知 UP / 无封面」，
       所以只在没有标题的兜底场景使用，并且会把无标题的条目过滤掉。
    """
    results: list[SearchResult] = []
    need = max(3, count // 2)

    try:
        results = _search_via_api(keyword, count, cookies_from_browser, log)
    except Exception as exc:  # noqa: BLE001
        log(f"[警告] 直连搜索接口失败（{exc}）")

    if len(results) < need:
        # 非中文关键词在 WBI 接口上常常 0 条，用搜索结果页补齐
        try:
            page_results = _search_via_page(keyword, count, cookies_from_browser, log)
            log(f"搜索结果页返回 {len(page_results)} 条")
            results = _merge_results(results, page_results)
        except Exception as exc:  # noqa: BLE001
            log(f"[警告] 搜索结果页解析失败（{exc}）")

    if len(results) < need:
        # 最后兜底。注意 yt-dlp flat search 没有标题/UP/封面，
        # 只有 id；这里仍然取，但下游会把无标题条目过滤掉。
        try:
            yt = _search_via_ytdlp(
                keyword, count, _resolved_browser(cookies_from_browser), log
            )
            log(f"yt-dlp 返回 {len(yt)} 条（可能缺元数据）")
            results = _merge_results(results, yt)
        except BiliError as exc:
            if not results:
                raise
            log(f"[警告] yt-dlp 搜索失败，仅使用已有结果：{exc}")

    if not results:
        raise BiliError(
            "没有搜索到任何结果。\n"
            "提示：可换用中文歌名/译名再搜；B 站搜索对匿名请求风控较严时，"
            "也可在「搜索选项 → 浏览器 Cookie」里选一个已登录 B 站的浏览器。"
        )
    return results


def _merge_results(*groups: list[SearchResult]) -> list[SearchResult]:
    """按视频 id 去重合并，保留靠前通道的结果。

    同时丢弃**没有任何元数据**的条目（yt-dlp flat search 的产物），
    免得界面上出现「(无标题) / 未知 UP / 无封面」这种没法用的卡片。
    """
    merged: list[SearchResult] = []
    seen: set[str] = set()

    def _key(item: SearchResult) -> str:
        vid = item.video_id.lower()
        return vid[2:] if vid.startswith(("bv", "av")) else vid

    for group in groups:
        for item in group:
            k = _key(item)
            if not k or k in seen:
                continue
            # 无标题的一律丢掉；标题为空说明拿不到任何可用信息
            if not (item.title or "").strip():
                continue
            seen.add(k)
            merged.append(item)
    for i, r in enumerate(merged, 1):
        r.index = i
    return merged


def _clean_html(text: str) -> str:
    """去掉搜索结果标题里的 <em class="keyword"> 高亮标签等 HTML。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    return (
        text.replace("&quot;", '"')
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
        .strip()
    )


def _search_via_api(
    keyword: str,
    count: int,
    cookies_from_browser: str | None,
    log: LogFn,
) -> list[SearchResult]:
    from .bili_api import BiliSession

    session = BiliSession(cookies_from_browser=cookies_from_browser)
    log("使用 B 站 WBI 签名搜索接口 ...")
    raw = session.search_video(keyword, page=1)
    results: list[SearchResult] = []
    for item in raw:
        bvid = item.get("bvid") or ""
        if not bvid:
            continue
        pic = item.get("pic") or ""
        if pic.startswith("//"):
            pic = "https:" + pic
        results.append(
            SearchResult(
                index=len(results) + 1,
                video_id=bvid,
                title=_clean_html(item.get("title") or ""),
                uploader=(item.get("author") or "").strip(),
                duration=_parse_duration(item.get("duration")),
                url=f"https://www.bilibili.com/video/{bvid}",
                view_count=item.get("play"),
                thumbnail=pic or None,
            )
        )
        if len(results) >= count:
            break
    return results


# --- B 站搜索结果页（服务端渲染）的解析 ------------------------------------- #
# 为什么需要这条通道：
#   WBI 搜索接口对**非中文关键词**（英文、罗马字、日文假名）经常返回 0 条；
#   而 yt-dlp 的 flat search 只有 id 和 url，没有标题/UP/封面，无法用来展示。
#   搜索结果页 https://search.bilibili.com/all?keyword=... 是服务端渲染的，
#   卡片里标题、UP 主、封面、时长、BV 号齐全，实测 0.5 秒返回。
_PAGE_HEADERS = {
    "User-Agent": UA,
    "Referer": BILI_REFERER,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&nbsp;": " ", "&#34;": '"', "&#x27;": "'",
}
_ENTITY_RE = re.compile(r"&(?:#x?[0-9a-fA-F]+|[a-zA-Z]+);")


def _unescape(text: str) -> str:
    """解开 HTML 实体。命名实体查表，数字实体直接转字符。"""
    if not text:
        return text

    def _sub(m: re.Match) -> str:
        token = m.group(0)
        if token in _HTML_ENTITIES:
            return _HTML_ENTITIES[token]
        if token.startswith("&#"):
            try:
                body = token[2:-1]
                code = int(body[1:], 16) if body[:1].lower() == "x" else int(body)
                return chr(code)
            except (ValueError, OverflowError):
                return token
        return token

    return _ENTITY_RE.sub(_sub, text)


def _decode_title(text: str) -> str:
    """标题解码。B 站搜索页把 title 属性**转义了两次**。

    实测原始 HTML：
        title="Flashback Ft. Issei Suzuki &amp;amp; Yuu Fujita"
        title="瘋狗回歸 - Flash Back 90&amp;#x27;s【國動】"
    只解一层会留下 `&amp;` / `&#x27;` 这种可见的乱码，所以要解两层。
    最多两轮，避免极端输入下反复解码（也顺手兜住三层转义）。
    """
    if not text:
        return text
    prev = text
    for _ in range(3):
        cur = _unescape(prev)
        if cur == prev:
            break
        prev = cur
    return prev


def _search_via_page(
    keyword: str,
    count: int,
    cookies_from_browser: str | None,
    log: LogFn,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """解析 B 站搜索结果页（服务端渲染），拿到带完整元数据的结果。"""
    import requests

    log("使用 B 站搜索结果页 ...")
    session = requests.Session()
    session.headers.update(_PAGE_HEADERS)
    try:
        # 带 cookie 时先套上已缓存的登录态（只为降风控，读不到也不影响）
        if cookies_from_browser:
            from .cookies import cached_login_cookies

            for cookie in cached_login_cookies(cookies_from_browser):
                session.cookies.set_cookie(cookie)
    except Exception:  # noqa: BLE001
        pass

    resp = session.get(
        "https://search.bilibili.com/all",
        params={"keyword": keyword},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise BiliError(f"搜索结果页返回 HTTP {resp.status_code}")
    return _parse_search_page(resp.text, count)


def _parse_search_page(html: str, count: int) -> list[SearchResult]:
    """从搜索结果页 HTML 里解析视频卡片。

    卡片结构（B 站 2026 年版，服务端渲染）：
        <div class="bili-video-card__wrap">
          <a href="//www.bilibili.com/video/BVxxxx/"><picture><source srcset="//i2.hdslb.com/....jpg@672w...">
          ...<span class="bili-video-card__stats__duration">02:17</span>
          <h3 class="bili-video-card__info--tit" title="完整标题"><em>高亮</em>普通文本</h3>
          <span class="bili-video-card__info--author">UP主</span>
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    # 只取视频链接，避免把用户/专栏卡片混进来
    anchor = re.compile(r'href="(?:https?:)?//www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})')

    for match in anchor.finditer(html):
        bvid = match.group(1)
        if bvid in seen:
            continue
        # 截取该卡片所在的 HTML 片段：向后取一段（卡片边界用下一个视频卡片或固定长度）
        seg = html[match.start(): match.start() + 3500]

        # 标题：优先 h3 的 title 属性（无损），否则取 h3 文本
        title = ""
        m = re.search(r'class="bili-video-card__info--tit"[^>]*title="([^"]*)"', seg)
        if m:
            title = _decode_title(m.group(1)).strip()
        if not title:
            m = re.search(r'<h3[^>]*class="[^"]*info--tit[^"]*"[^>]*>(.*?)</h3>', seg, re.S)
            if m:
                title = _clean_html(m.group(1)).strip()
        if not title:
            # 退而求其次：封面图的 alt 就是标题
            m = re.search(r'alt="([^"]{2,})"', seg)
            if m:
                title = _decode_title(m.group(1)).strip()
        if not title:
            # 没标题的卡片没法用，跳过（这正是之前显示 "(无标题)" 的来源）
            continue

        up = ""
        m = re.search(r'info--author"[^>]*>([^<]{1,60})<', seg)
        if m:
            up = _decode_title(m.group(1)).strip()
        if not up:
            # 少数卡片（活动页/合集）没有作者节点，退而取 owner 链接的 title
            m = re.search(r'class="[^"]*info--owner[^"]*"[^>]*title="([^"]{1,60})"', seg)
            if m:
                up = _decode_title(m.group(1)).strip()

        cover = None
        m = re.search(r'(?:srcset|src)="(//[^"\s]+\.(?:jpg|png|webp|avif)[^"\s]*)"', seg)
        if m:
            # 去掉 @672w_378h... 这类尺寸后缀，拿原图
            cover = "https:" + re.sub(r"@[^/]*$", "", m.group(1))
        if not cover:
            m = re.search(r'alt="[^"]*"[^>]*src="(//[^"\s]+?)(?:@[^"\s]*)?"', seg)
            if m:
                cover = "https:" + m.group(1)

        duration = None
        m = re.search(r'stats__duration[^>]*>([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)<', seg)
        if m:
            duration = _parse_duration(m.group(1))

        seen.add(bvid)
        results.append(
            SearchResult(
                index=len(results) + 1,
                video_id=bvid,
                title=title,
                uploader=up,
                duration=duration,
                url=f"https://www.bilibili.com/video/{bvid}",
                view_count=None,
                thumbnail=cover,
            )
        )
        if len(results) >= count:
            break
    return results


def _parse_duration(value) -> int | None:
    """B 站搜索返回的 duration 可能是 "4:03" 也可能已是秒数。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    try:
        total = 0
        for p in parts:
            total = total * 60 + int(p)
        return total
    except ValueError:
        return None


def _search_via_ytdlp(
    keyword: str,
    count: int,
    cookies_from_browser: str | None,
    log: LogFn,
) -> list[SearchResult]:
    cmd = _ytdlp_base(cookies_from_browser) + [
        "--dump-json",
        "--flat-playlist",
        "--playlist-end",
        str(max(1, count)),
        f"bilisearch{max(1, count)}:{keyword}",
    ]
    try:
        records = _run_json(cmd, log, timeout=SEARCH_TOTAL_TIMEOUT)
    except BiliError as exc:
        raise BiliError(
            str(exc)
            + "\n提示：B 站搜索接口对匿名请求风控较严，"
            "可在「搜索选项 → 浏览器 Cookie」里选一个已登录 B 站的浏览器"
            "（网页版），或加 --cookies-from-browser edge（命令行版），再稍后重试。"
        ) from exc
    results: list[SearchResult] = []
    for rec in records:
        vid = str(rec.get("id") or "").strip()
        url = rec.get("webpage_url") or rec.get("url") or ""
        if not vid:
            # 从 url 里兜底解析 av/BV 号
            m = re.search(r"(BV[0-9A-Za-z]+|av\d+)", url, re.IGNORECASE)
            if m:
                vid = m.group(1)
        if not vid:
            continue
        if not url:
            url = (
                f"https://www.bilibili.com/video/{vid}"
                if vid.lower().startswith("bv")
                else f"https://www.bilibili.com/video/av{vid}"
            )
        thumbs = rec.get("thumbnails") or []
        thumbnail = thumbs[-1].get("url") if thumbs else None
        results.append(
            SearchResult(
                index=len(results) + 1,
                video_id=vid,
                title=(rec.get("title") or "").strip(),
                uploader=(rec.get("uploader") or rec.get("channel") or "").strip(),
                duration=rec.get("duration"),
                url=url,
                view_count=rec.get("view_count"),
                thumbnail=thumbnail,
            )
        )
        if len(results) >= count:
            break
    return results


def fetch_track_info(
    video_id: str,
    cookies_from_browser: str | None = None,
    log: LogFn | None = None,
    fallback_title: str = "",
    fallback_uploader: str = "",
    fallback_thumbnail: str | None = None,
) -> TrackInfo:
    """获取单个视频的详细元数据（标题/UP主/时长/封面原图/标签）。

    支持 BV 号与 av 号；接口失败时退回搜索阶段已有的信息，保证流程不中断。
    """
    log = log or _noop
    url = (
        f"https://www.bilibili.com/video/{video_id}"
        if video_id.lower().startswith("bv")
        else f"https://www.bilibili.com/video/av{video_id}"
    )
    info = TrackInfo(
        video_id=video_id,
        title=fallback_title or video_id,
        uploader=fallback_uploader,
        url=url,
        cover_url=fallback_thumbnail,
    )
    try:
        from .bili_api import BiliSession

        session = BiliSession(cookies_from_browser=cookies_from_browser)
        data = session.view(video_id)
        info.title = (data.get("title") or info.title).strip()
        info.uploader = (data.get("owner") or {}).get("name", "") or info.uploader
        info.duration = data.get("duration") or info.duration
        info.cover_url = data.get("pic") or info.cover_url
        info.description = (data.get("desc") or "").strip()
        info.extra["bvid"] = data.get("bvid")
        info.extra["aid"] = data.get("aid")
        info.extra["cid"] = data.get("cid")
        info.extra["pubdate"] = data.get("pubdate")
        info.extra["view"] = (data.get("stat") or {}).get("view")
        bvid = data.get("bvid") or info.extra.get("bvid")
        if bvid:
            url = f"https://www.bilibili.com/video/{bvid}"
            info.url = url
            info.tags = session.tags(bvid)
    except Exception as exc:  # noqa: BLE001 - 元数据失败不应中断流程
        log(f"[警告] 详情接口请求失败（使用搜索结果信息）: {exc}")
    return info


def download_cover(
    track: TrackInfo,
    dest_no_ext: Path,
    log: LogFn | None = None,
    cancel_event=None,
) -> Path | None:
    """下载曲绘（B 站上传者提供的封面原图），统一转成 PNG 保存。"""
    from .config import run_cancellable

    log = log or _noop
    if not track.cover_url:
        log("[警告] 未取得封面地址，跳过曲绘分离")
        return None
    url = track.cover_url
    if url.startswith("//"):
        url = "https:" + url
    # B 站图片支持 @ 后缀指定尺寸；去掉后缀拿原图
    url = re.sub(r"@[^/]*$", "", url)
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": BILI_REFERER})
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log(f"[警告] 曲绘下载失败: {exc}")
        return None

    ensure_dirs(dest_no_ext.parent)
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        tmp_raw = dest_no_ext.with_suffix(".cover.raw")
        tmp_raw.write_bytes(resp.content)
        out = dest_no_ext.with_suffix(".png")
        cmd = [
            str(ffmpeg), "-y", "-loglevel", "error",
            "-i", str(tmp_raw),
            "-frames:v", "1",
            str(out),
        ]
        proc_rc, _o, proc_err = run_cancellable(cmd, cancel_event=cancel_event)
        tmp_raw.unlink(missing_ok=True)
        if proc_rc == 0 and out.is_file():
            log(f"曲绘已保存: {out.name}")
            return out
        log(f"[警告] 曲绘转 PNG 失败，改存原始字节: {(proc_err or '').strip()[:200]}")
    # 退化路径：直接写原始图片字节
    ext = ".jpg" if resp.content[:3] == b"\xff\xd8\xff" else ".png"
    out = dest_no_ext.with_suffix(ext)
    out.write_bytes(resp.content)
    log(f"曲绘已保存（原始格式）: {out.name}")
    return out


def _download_failure_message(
    cmd: list[str], rc: int, out: str, err: str, dest_dir: Path, url: str,
    cookies_from_browser: str | None = None,
) -> str:
    """拼出**永远不为空**的下载失败信息。

    背景：原先写的是 `err or out`，当 yt-dlp 无任何输出就失败时（进程被杀、
    被安全软件拦截、代理/TLS 层直接断流等），两者都是空字符串，于是界面只显示
    「音频下载失败：」后面什么都没有 —— 等于把真正的失败原因吞掉了。
    这里改成：无论有没有输出，都附上退出码、命令、目录、yt-dlp 来源与版本，
    这样"什么都没输出"这件事本身就成为一条可用的线索。
    """
    detail = (err or "").strip() or (out or "").strip()
    # 复用带缓存的版本探针，避免这里再起一次进程（也与日志里那行保持一致）
    ytdlp = which("yt-dlp")
    try:
        ver = ytdlp_version_info()
    except Exception:  # noqa: BLE001
        ver = "?"
    lines = ["音频下载失败："]
    if detail:
        lines.append(detail[-1500:])
    else:
        lines.append(
            f"（yt-dlp 没有输出任何错误信息就退出了，退出码 {rc}）\n"
            "常见原因：杀毒/安全软件拦截了下载、公司或校园网的代理拦截了流量、"
            "临时目录不可写、或网络在中途被断开。"
        )
    lines += [
        "",
        f"退出码：{rc}",
        f"yt-dlp：{ver}",
        f"浏览器 Cookie：{('使用 ' + cookies_from_browser) if cookies_from_browser else '未使用（匿名下载）'}",
        f"下载目录：{dest_dir}",
        f"工作目录：{Path.cwd()}",
        f"输出根目录：{os.environ.get('BILIMUSIC_OUTPUT') or '(未设置，用默认)'}",
        f"视频地址：{url}",
        "命令：" + " ".join(cmd),
        "",
        "建议依次尝试：",
        "  1. 换一首歌，或稍等几分钟重试（B 站偶发风控）",
        "  2. 若上方显示「使用 某浏览器」，把它改成「不使用 Cookie」再试一次",
        "  3. 关闭杀毒软件的实时防护后重试",
        "  4. 运行「检查下载环境」诊断（启动器菜单里有）",
    ]
    return "\n".join(lines)


def download_audio(
    url: str,
    dest_dir: Path,
    stem: str,
    cookies_from_browser: str | None = None,
    log: LogFn | None = None,
    cancel_event=None,
) -> Path:
    """只下载音频流（不下载视频流），返回下载后的音频文件路径。

    用 `--print after_move:filepath` 直接拿到最终文件路径，比事后猜扩展名可靠。
    """
    from .config import run_cancellable

    log = log or _noop
    ensure_dirs(dest_dir)
    before = {p for p in dest_dir.iterdir() if p.is_file()}
    cmd = _ytdlp_base(cookies_from_browser) + [
        "-f", "ba/bestaudio",
        "-x", "--audio-format", "best",
        "--no-part",
        "--print", "after_move:filepath",
        "--newline",
        "-o", str(dest_dir / f"{stem}.%(ext)s"),
        url,
    ]
    log("开始下载音频流 ...")
    log(f"yt-dlp {ytdlp_version_info()}")
    rc, out, err = run_cancellable(cmd, cancel_event=cancel_event)

    reported: Path | None = None
    for line in (out or "").splitlines():
        line = line.strip()
        if line and not line.startswith("[") and ("\\" in line or "/" in line):
            cand = Path(line)
            if cand.is_file():
                reported = cand
    if reported is not None:
        log(f"音频下载完成: {reported.name} ({reported.stat().st_size / 1048576:.1f} MB)")
        return reported

    after = [p for p in dest_dir.iterdir() if p.is_file() and p not in before]
    after = [p for p in after if p.suffix.lower() not in {".part", ".ytdl", ".temp"}]
    if not after:
        after = [
            p for p in dest_dir.glob(f"{stem}.*")
            if p.suffix.lower() not in {".part", ".ytdl"}
        ]
    if not after:
        raise BiliError(_download_failure_message(
            cmd, rc, out, err, dest_dir, url,
            cookies_from_browser=cookies_from_browser,
        ))
    audio = max(after, key=lambda p: p.stat().st_size)
    log(f"音频下载完成: {audio.name} ({audio.stat().st_size / 1048576:.1f} MB)")
    return audio
