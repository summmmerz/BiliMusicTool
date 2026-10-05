"""浏览器 Cookie 可用性探测。

为什么需要这个模块：
    在 Windows 上 `--cookies-from-browser` / yt-dlp 的 cookie 读取**经常直接失败**：
      * Edge/Chrome 正在运行 -> cookie 数据库被锁
        `Could not copy Chrome cookie database`（yt-dlp issue #7271）
      * 新版 Chromium 的 App-Bound Encryption -> `Failed to decrypt with DPAPI`
        （yt-dlp issue #10927）
    而且失败是**硬失败**：yt-dlp 直接以非 0 退出，连普通视频都下不了。

    所以不能盲目把 cookie 传给下载流程，必须先探测；探测失败就回落匿名下载，
    并在界面上明确告诉用户"cookie 没用上、原因是什么"，而不是静默失败或假装成功。
"""
from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass, field

# 探测结果缓存：browser -> {"probe": CookieProbe, "jar": [...]}
# 读浏览器 cookie 又慢又容易挂，缓存后搜索路径不再付出这个代价。
_COOKIE_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()
# 正在进行的探测：browser -> threading.Event（set 表示已完成）
# 用于让搜索这类延迟敏感的调用不必等待探测，同时避免并发重复探测。
_PROBING: dict[str, threading.Event] = {}

# 探测用的公开视频（不需要登录即可访问）
_PROBE_URL = "https://www.bilibili.com/video/BV1GJ411x7h7"

# 与 B 站登录态有关的关键 cookie 名
LOGIN_COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID")

# 已知的失败特征 -> 给用户的通俗解释
_FAILURE_HINTS = [
    (r"could not copy .*cookie database",
     "浏览器的 Cookie 数据库被占用",
     "关掉浏览器后重试，或改用另一种方式提供 Cookie"),
    (r"failed to decrypt with dpapi",
     "浏览器用了新的加密方式，yt-dlp 无法解密",
     "在浏览器设置里关闭「使用 Windows Hello / 应用绑定加密」后再试，或改用导出 cookies.txt"),
    (r"could not find .* cookies database",
     "找不到该浏览器的 Cookie 数据库",
     "确认这个浏览器装过并且登录过 B 站，或换一个浏览器"),
    (r"unknown browser",
     "不认识的浏览器名",
     "只能选 edge / chrome / firefox 等受支持的浏览器"),
    (r"unsupported browser",
     "不支持的浏览器",
     "换用 edge / chrome / firefox"),
    (r"permission denied",
     "无权限读取 Cookie 文件",
     "以管理员身份运行，或先关闭浏览器"),
]


@dataclass
class CookieProbe:
    """一次 Cookie 探测的结果。"""

    browser: str
    ok: bool
    reason: str = ""
    advice: str = ""
    login_cookies: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    detail: str = ""

    @property
    def has_login(self) -> bool:
        return bool(self.login_cookies)

    def to_dict(self) -> dict:
        return {
            "browser": self.browser,
            "ok": self.ok,
            "reason": self.reason,
            "advice": self.advice,
            "login_cookies": self.login_cookies,
            "has_login": self.has_login,
            "elapsed": round(self.elapsed, 2),
            "detail": self.detail[:400],
        }


def _explain(stderr: str) -> tuple[str, str]:
    low = (stderr or "").lower()
    for pattern, reason, advice in _FAILURE_HINTS:
        if re.search(pattern, low):
            return reason, advice
    return "Cookie 读取失败", "换一种方式提供 Cookie，或关闭浏览器后重试"


def probe_browser_cookies(
    browser: str,
    timeout: float = 12.0,
    use_cache: bool = True,
    wait: bool = True,
) -> CookieProbe:
    """探测指定浏览器的 Cookie 是否真的能被读取。

    用 yt-dlp 的 cookie 读取接口直接读一次（能拿到哪些 cookie、有没有登录态），
    整个过程放在子线程里并加超时，避免浏览器 cookie 库被锁时把调用方卡死。

    use_cache=True 时复用上一次的探测结果（**包括失败结果**）。
    理由：读浏览器 cookie 又慢又容易挂，而失败通常是环境性的
    （浏览器正在运行、加密解不开）。每次搜索都重试只会让界面一直转圈。

    wait=False 时若已有一次探测在跑，立刻返回"正在后台读取"而不等待——
    搜索路径用这个，保证界面不会为了读 cookie 而卡十几秒。
    """
    import time

    if use_cache:
        with _CACHE_LOCK:
            hit = _COOKIE_CACHE.get(browser)
            inflight = _PROBING.get(browser)
        if hit is not None:
            return hit["probe"]
        if inflight is not None:
            if not wait:
                # 已有一次探测在跑。搜索这类对延迟敏感的场景不等它，
                # 直接当作"暂时不可用"——后台探测完成后会自动进缓存，
                # 下一次请求就能用上，用户不需要为此多等十几秒。
                return CookieProbe(
                    browser=browser,
                    ok=False,
                    reason=f"正在后台读取 {browser} 的 Cookie，本次先按匿名处理",
                    advice="稍等几秒再试，或点「检查」查看结果",
                )
            inflight.wait(timeout)
            with _CACHE_LOCK:
                hit = _COOKIE_CACHE.get(browser)
            if hit is not None:
                return hit["probe"]

    result: dict = {}

    def worker() -> None:
        t0 = time.time()
        try:
            from yt_dlp.cookies import extract_cookies_from_browser  # type: ignore

            jar = extract_cookies_from_browser(browser)
            cookies = list(jar)
            names = [c.name for c in cookies]
            login = [n for n in names if n in LOGIN_COOKIE_NAMES]
            result["ok"] = True
            result["count"] = len(names)
            result["login"] = login
            result["jar"] = cookies
            result["elapsed"] = time.time() - t0
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["elapsed"] = time.time() - t0

    started = threading.Event()
    original_worker = worker

    def tracked_worker() -> None:
        try:
            original_worker()
        finally:
            started.set()

    with _CACHE_LOCK:
        _PROBING[browser] = started
    try:
        thread = threading.Thread(target=tracked_worker, daemon=True)
        thread.start()
        thread.join(timeout)
        alive = thread.is_alive()
    finally:
        with _CACHE_LOCK:
            _PROBING.pop(browser, None)

    if alive:
        probe = CookieProbe(
            browser=browser,
            ok=False,
            reason=f"读取 {browser} 的 Cookie 超过 {timeout:.0f} 秒仍未返回",
            advice="通常是 Cookie 数据库被浏览器锁住；关掉该浏览器后重试",
            elapsed=timeout,
        )
        with _CACHE_LOCK:
            _COOKIE_CACHE[browser] = {"probe": probe, "jar": []}
        return probe

    if result.get("ok"):
        login = result.get("login") or []
        probe = CookieProbe(
            browser=browser,
            ok=True,
            reason=f"读取成功，共 {result.get('count')} 个 Cookie"
                   + (f"，其中登录凭证 {', '.join(login)}" if login
                      else "，但没有登录凭证（该浏览器未登录 B 站，会员内容仍不可用）"),
            advice="" if login else "请先在该浏览器登录 B 站，再重新检查",
            login_cookies=login,
            elapsed=result.get("elapsed", 0.0),
        )
        with _CACHE_LOCK:
            _COOKIE_CACHE[browser] = {"probe": probe, "jar": result.get("jar") or []}
        return probe

    reason, advice = _explain(result.get("error", ""))
    probe = CookieProbe(
        browser=browser,
        ok=False,
        reason=reason,
        advice=advice,
        elapsed=result.get("elapsed", 0.0),
        detail=result.get("error", ""),
    )
    with _CACHE_LOCK:
        _COOKIE_CACHE[browser] = {"probe": probe, "jar": []}
    return probe


def resolve_cookies(
    browser: str | None,
    log=None,
    force: bool = False,
) -> str | None:
    """决定这次任务到底要不要用 Cookie。

    返回可安全交给 yt-dlp / requests 的浏览器名；若 Cookie 不可用则返回 None，
    并把原因通过 log 讲清楚（绝不静默降级）。

    探测结果会**按浏览器缓存**：读浏览器 cookie 又慢又容易挂，
    而且失败通常是环境性的（浏览器在运行、加密解不开），
    重试既无用又会让每次搜索都白等一轮。想强制重新探测传 force=True。
    """
    if not browser:
        return None
    log = log or (lambda _m: None)

    probe = probe_browser_cookies(browser, use_cache=not force, wait=True)
    if not probe.ok:
        log(f"[警告] 无法使用 {browser} 的 Cookie：{probe.reason}")
        if probe.advice:
            log(f"        建议：{probe.advice}")
        log("        已自动改用匿名方式继续（普通视频不受影响；会员/需登录视频仍会失败）")
        return None

    if probe.has_login:
        log(f"Cookie：已从 {browser} 读取登录态（{', '.join(probe.login_cookies)}）")
    else:
        log(f"Cookie：{probe.reason}")
    return browser


def cached_login_cookies(browser: str) -> list:
    """取出某个浏览器已缓存探测到的登录 cookie 对象（没有则返回空列表）。

    给 BiliSession 用：搜索这类对延迟敏感的操作不该再现场读一遍浏览器数据库。
    不等后台探测结果（wait=False），拿不到就先按匿名走。
    """
    probe = probe_browser_cookies(browser, use_cache=True, wait=False)
    if not probe.ok:
        return []
    return list(_COOKIE_CACHE.get(browser, {}).get("jar") or [])
