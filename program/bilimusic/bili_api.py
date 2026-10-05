"""B 站 Web API 直连：WBI 签名、搜索、视频详情、标签。

背景：B 站的老搜索接口 `/x/web-interface/search/type` 对无签名请求直接返回
412 request was banned，而 **WBI 签名**接口
`/x/web-interface/wbi/search/type` 可用。WBI 签名要求：
  1. 从 `/x/web-interface/nav` 取 img_url / sub_url 两个文件名
  2. 拼接后按固定 64 位置换表重排，取前 32 位作为 mixin key
  3. 参数按 key 升序拼接 + wts 时间戳，末尾追加 mixin key 后取 md5 作为 w_rid
另外先访问一次 www.bilibili.com 拿 buvid3 / b_nut cookie 能显著降低被风控概率。
"""
from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

import requests

from .config import BILI_REFERER, UA

# WBI mixin 置换表（B 站前端固定常量）
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_API = "https://api.bilibili.com"

# 把已缓存的 cookie 装进 requests 会话的超时（秒）。纯内存操作，
# 给个短超时只为极端情况兜底。
COOKIE_APPLY_TIMEOUT = 5.0

# 单次 HTTP 请求超时（秒）。B 站偶发慢响应，超时后由上层重试或报错，
# 不能让请求无限挂着——那正是"搜索卡住"的主因之一。
HTTP_TIMEOUT = 15.0


class BiliApiError(RuntimeError):
    pass


class BiliSession:
    """带 WBI 签名与 cookie 初始化的 B 站会话。"""

    def __init__(self, cookies_from_browser: str | None = None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Referer": BILI_REFERER,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": "https://www.bilibili.com",
            }
        )
        self._mixin_key: str | None = None
        self._warmed = False
        self._browser_cookies = cookies_from_browser
        self.cookie_error: str | None = None  # 读取浏览器 cookie 失败时的原因
        self.cookie_count: int = 0            # 成功读到的 cookie 数量

    # ------------------------------------------------------------------ #
    def warm_up(self) -> None:
        """访问首页获取 buvid3 等 cookie，降低 412 概率。"""
        if self._warmed:
            return
        self._warmed = True
        if self._browser_cookies:
            self._load_browser_cookies()
        try:
            self.session.get("https://www.bilibili.com/", timeout=HTTP_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass

    def _load_browser_cookies(self) -> None:
        """把（已缓存的）浏览器登录态套到本会话上。

        这里刻意**不现场读浏览器数据库** —— 那是慢操作，最坏会把调用方卡死
        （浏览器运行时 cookie 库被锁）。真正的一次性探测交给
        `cookies.probe_browser_cookies`，它有子线程超时且结果带缓存；
        本方法只做"把缓存里的登录 cookie 装进来"这种极快的操作。

        这样用户即使勾了浏览器，每次搜索也**不会**再等读 cookie。
        """
        import threading

        from .cookies import cached_login_cookies, probe_browser_cookies

        if not self._browser_cookies:
            return
        jar = cached_login_cookies(self._browser_cookies)
        if not jar:
            # 从未探测过、或上次探测失败：不影响匿名请求，
            # 但记下原因供上层提示（不阻塞、不假装成功）。
            # wait=False：搜索路径绝不为读 cookie 停下来等十几秒。
            probe = probe_browser_cookies(self._browser_cookies, use_cache=True, wait=False)
            self.cookie_error = None if probe.ok else probe.reason
            return

        done: dict = {}

        def worker() -> None:
            try:
                for cookie in jar:
                    self.session.cookies.set_cookie(cookie)
                done["count"] = len(jar)
            except Exception as exc:  # noqa: BLE001
                done["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(COOKIE_APPLY_TIMEOUT)
        if thread.is_alive():
            self.cookie_error = "应用浏览器 Cookie 超时"
        elif done.get("error"):
            self.cookie_error = done["error"]
        else:
            self.cookie_count = done.get("count", 0)

    # ------------------------------------------------------------------ #
    def mixin_key(self) -> str:
        if self._mixin_key:
            return self._mixin_key
        self.warm_up()
        resp = self.session.get(f"{_API}/x/web-interface/nav", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        wbi = data.get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        raw = img + sub
        if len(raw) < 64:
            raise BiliApiError("未能获取 WBI 密钥（nav 接口返回异常）")
        self._mixin_key = "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]
        return self._mixin_key

    def wbi_sign(self, params: dict[str, Any]) -> dict[str, Any]:
        key = self.mixin_key()
        signed = dict(params)
        signed["wts"] = int(time.time())
        items = sorted((k, "" if v is None else str(v)) for k, v in signed.items())
        query = urllib.parse.urlencode(items)
        signed = dict(items)
        signed["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
        return signed

    # ------------------------------------------------------------------ #
    def get(self, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> dict:
        self.warm_up()
        use = self.wbi_sign(params or {}) if signed else (params or {})
        resp = self.session.get(f"{_API}{path}", params=use, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise BiliApiError(f"{path} 返回 HTTP {resp.status_code}")
        payload = resp.json()
        return payload

    # ------------------------------------------------------------------ #
    def search_video(self, keyword: str, page: int = 1) -> list[dict]:
        """WBI 视频搜索，返回原始 result 列表。"""
        payload = self.get(
            "/x/web-interface/wbi/search/type",
            {"search_type": "video", "keyword": keyword, "page": page},
            signed=True,
        )
        if payload.get("code") != 0:
            raise BiliApiError(
                f"搜索接口返回 code={payload.get('code')} {payload.get('message', '')}"
            )
        return (payload.get("data") or {}).get("result") or []

    def view(self, video_id: str) -> dict:
        """按 BV 号或 av 号取视频详情。

        B 站 view 接口对 av 号用 aid 参数；顶层有 `id` 时直接用它最省事
        （yt-dlp 搜索返回的就是纯数字 av 号）。
        """
        video_id = (video_id or "").strip()
        if not video_id:
            raise BiliApiError("视频 id 为空")
        if video_id.lower().startswith("bv"):
            params: dict[str, Any] = {"bvid": video_id}
        elif video_id.lower().startswith("av"):
            params = {"aid": video_id[2:]}
        elif video_id.isdigit():
            params = {"aid": video_id}
        else:
            params = {"bvid": video_id}
        payload = self.get("/x/web-interface/view", params)
        if payload.get("code") != 0:
            raise BiliApiError(
                f"详情接口返回 code={payload.get('code')} {payload.get('message', '')}"
            )
        return payload.get("data") or {}

    def tags(self, bvid: str) -> list[str]:
        try:
            payload = self.get("/x/tag/archive/tags", {"bvid": bvid})
            return [t.get("tag_name", "") for t in (payload.get("data") or []) if t.get("tag_name")]
        except Exception:  # noqa: BLE001
            return []
