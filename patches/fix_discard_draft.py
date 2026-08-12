"""修复 TikTok「过往未保存」→「放弃此次发布？」两步确认弹窗。

原逻辑只点一次「放弃」就返回，二次确认弹窗会挡住上传/文案页，
导致后续「填写描述失败：无法写入 contenteditable」。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("publisher")

_DRAFT_MARKERS = (
    "未保存",
    "继续编辑",
    "Continue editing",
    "not saved",
    "放弃此次发布",
    "Discard this post",
    "permanently discarded",
    "将被永久放弃",
)


def _visible(locator, timeout_ms: int = 800) -> bool:
    try:
        return bool(locator.is_visible(timeout=timeout_ms))
    except Exception:
        return False


def _click_first_visible(page, selectors: list[str], timeout_ms: int = 2000) -> str | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if _visible(loc, 700):
                loc.click(timeout=timeout_ms)
                return sel
        except Exception:
            try:
                loc = page.locator(sel).first
                if _visible(loc, 500):
                    loc.click(timeout=timeout_ms, force=True)
                    return sel + "#force"
            except Exception:
                continue
    return None


def _click_discard_in_scope(page, scope=None) -> str | None:
    """优先在 dialog 作用域内点红色「放弃」，避免点到黄条/错误按钮。"""
    root = scope if scope is not None else page
    candidates = [
        'button:has-text("放弃")',
        'button:has-text("Discard")',
        '[role="button"]:has-text("放弃")',
        '[role="button"]:has-text("Discard")',
        'div:has-text("放弃")',
    ]
    for sel in candidates:
        try:
            loc = root.locator(sel).last
            if _visible(loc, 700):
                loc.click(timeout=2500)
                return f"scope:{sel}"
        except Exception:
            try:
                loc = root.locator(sel).last
                if _visible(loc, 400):
                    loc.click(timeout=2500, force=True)
                    return f"scope:{sel}#force"
            except Exception:
                continue
    # Playwright 语义角色兜底
    try:
        btn = root.get_by_role("button", name="放弃")
        if btn.count() > 0 and _visible(btn.last, 700):
            btn.last.click(timeout=2500)
            return "role:放弃"
    except Exception:
        pass
    try:
        btn = root.get_by_role("button", name="Discard")
        if btn.count() > 0 and _visible(btn.last, 700):
            btn.last.click(timeout=2500)
            return "role:Discard"
    except Exception:
        pass
    return None


def discard_unsaved_draft_fixed(self, page) -> None:
    """循环处理草稿提示 + 二次确认，直到页面干净或达到上限。"""
    discard_selectors = list(self.SELECTORS.get("discard_btn") or []) + [
        '[role="dialog"] button:has-text("放弃")',
        '[role="dialog"] button:has-text("Discard")',
        '[role="dialog"] [role="button"]:has-text("放弃")',
        '[role="dialog"] [role="button"]:has-text("Discard")',
        'button:has-text("放弃")',
        'button:has-text("Discard")',
        'div[role="button"]:has-text("放弃")',
        'div[role="button"]:has-text("Discard")',
        'text=放弃',
    ]
    # 去重且保持顺序
    seen: set[str] = set()
    uniq: list[str] = []
    for s in discard_selectors:
        if s not in seen:
            seen.add(s)
            uniq.append(s)

    for round_i in range(8):
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""

        dlg = None
        has_confirm = False
        try:
            dlg = page.locator('[role="dialog"]').filter(has_text="放弃此次发布")
            if dlg.count() == 0:
                dlg = page.locator('[role="dialog"]').filter(has_text="将被永久放弃")
            if dlg.count() == 0:
                dlg = page.locator('[role="dialog"]').filter(has_text="Discard this post")
            if dlg.count() == 0:
                dlg = page.locator('[role="dialog"]').filter(has_text="permanently discarded")
            has_confirm = dlg.count() > 0 and _visible(dlg.first, 500)
        except Exception:
            has_confirm = False
            dlg = None

        has_banner = any(m in body for m in _DRAFT_MARKERS)
        if not has_confirm and not has_banner:
            return

        clicked = None
        if has_confirm and dlg is not None:
            clicked = _click_discard_in_scope(page, dlg.first)
        if not clicked:
            clicked = _click_discard_in_scope(page)
        if not clicked:
            clicked = _click_first_visible(page, uniq)

        if clicked:
            logger.info(
                "已处理草稿/放弃确认弹窗 (round=%s, sel=%s)", round_i + 1, clicked
            )
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
            continue

        # 点不到时不要死循环
        logger.warning("检测到草稿/放弃弹窗，但未能点击「放弃」(round=%s)", round_i + 1)
        return


def dismiss_blocking_modals_fixed(self, page) -> None:
    """在原有取消「添加链接」之外，顺带清掉放弃确认弹窗。"""
    try:
        discard_unsaved_draft_fixed(self, page)
    except Exception:
        pass

    for _ in range(3):
        try:
            dialog = page.locator('[role="dialog"]').first
            if not _visible(dialog, 400):
                break
            # 优先关掉添加链接类弹窗
            if _click_first_visible(
                page,
                list(self.SELECTORS.get("modal_cancel") or [])
                + [
                    '[role="dialog"] button:has-text("取消")',
                    '[role="dialog"] button:has-text("Cancel")',
                    'button:has-text("暂时不要")',
                ],
            ):
                page.wait_for_timeout(400)
                continue
            # 若是放弃确认，点红色放弃
            if _click_first_visible(
                page,
                [
                    '[role="dialog"] button:has-text("放弃")',
                    '[role="dialog"] button:has-text("Discard")',
                ],
            ):
                page.wait_for_timeout(400)
                continue
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except Exception:
                break
        except Exception:
            break


UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=creator_center"


def _resolve_upload_url(self) -> str:
    target = (getattr(self, "upload_url", None) or "").strip() or UPLOAD_URL
    self.upload_url = target
    return target


def _page_alive(page) -> bool:
    if page is None:
        return False
    try:
        if getattr(page, "is_closed", None) and page.is_closed():
            return False
    except Exception:
        return False
    try:
        _ = page.url
        return True
    except Exception:
        return False


def _is_target_closed_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    msg = str(exc or "").lower()
    return (
        "TargetClosed" in name
        or "target closed" in msg
        or "has been closed" in msg
        or ("closed" in msg and ("page" in msg or "browser" in msg or "context" in msg))
    )


def _is_studio_upload_page(page) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    return "tiktok.com" in url and "tiktokstudio" in url and "/upload" in url


def _get_context(self, page=None):
    old_page = page if page is not None else getattr(self, "page", None)
    try:
        if old_page is not None:
            return old_page.context
    except Exception:
        pass
    browser = getattr(self, "_browser", None)
    if browser is None or not getattr(browser, "contexts", None):
        raise RuntimeError("浏览器未连接，无法新建上传页")
    return browser.contexts[0]


def _close_other_pages(context, keep):
    try:
        for p in list(context.pages):
            if p is keep:
                continue
            try:
                p.close()
            except Exception:
                pass
    except Exception:
        pass


def _open_fresh_upload_page(self, page=None):
    """只在流程起点调用：新建标签 → 先关旧标签 → 再打开上传 URL。"""
    target = _resolve_upload_url(self)
    context = _get_context(self, page)

    new_page = context.new_page()
    self.page = new_page
    self._upload_page_ready = True  # type: ignore[attr-defined]
    _close_other_pages(context, new_page)

    logger.info("新建打开上传页: %s", target)
    _goto_upload_with_retry(new_page, target, attempts=3)
    try:
        new_page.wait_for_timeout(800)
    except Exception:
        pass

    try:
        self._ensure_browser_zoom_100(new_page)
    except Exception:
        pass
    try:
        self._normalize_viewport(new_page)
    except Exception:
        pass

    discard_unsaved_draft_fixed(self, new_page)
    return new_page


def _use_current_upload_page(self, page=None):
    """发布过程中：绝不新建/关闭标签，只在当前工作页继续。"""
    target = _resolve_upload_url(self)
    working = getattr(self, "page", None)
    if not _page_alive(working):
        working = page if _page_alive(page) else None
    if not _page_alive(working):
        # 极端情况：工作页丢了，才允许重建一次
        logger.warning("工作页已失效，重新新建上传页")
        return _open_fresh_upload_page(self, page)

    self.page = working
    if _is_studio_upload_page(working):
        logger.info("已在上传页，继续自动化（不关闭、不换页）: %s", working.url)
        discard_unsaved_draft_fixed(self, working)
        try:
            self._ensure_browser_zoom_100(working)
        except Exception:
            pass
        return working

    logger.info("当前工作页跳转到上传地址: %s", target)
    working.goto(target, wait_until="domcontentloaded", timeout=120000)
    try:
        working.wait_for_timeout(800)
    except Exception:
        pass
    _assert_not_login_page(working, where="工作页跳转上传")
    discard_unsaved_draft_fixed(self, working)
    try:
        self._ensure_browser_zoom_100(working)
    except Exception:
        pass
    return working


def _is_proxy_net_error(exc: BaseException) -> bool:
    if _is_target_closed_error(exc):
        # 窗口被代理掐断时常见 TargetClosed；按网络失败处理（不写长冷却）
        return True
    msg = str(exc or "")
    keys = (
        "PROXY_NETWORK_FAIL",
        "ERR_SOCKS_CONNECTION_FAILED",
        "ERR_PROXY_CONNECTION_FAILED",
        "ERR_TUNNEL_CONNECTION_FAILED",
        "ERR_CONNECTION_RESET",
        "ERR_CONNECTION_CLOSED",
        "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_TIMED_OUT",
        "ERR_TIMED_OUT",
        "ERR_NAME_NOT_RESOLVED",
        "ERR_NETWORK_CHANGED",
        "ERR_INTERNET_DISCONNECTED",
        "浏览器已关闭",
        "无法恢复",
        "页面/浏览器反复关闭",
    )
    return any(k in msg for k in keys)


class ProxyNetworkError(RuntimeError):
    """比特窗口代理/网络打不开 TikTok，应跳过账号且不写长冷却。"""


class AccountLoginRequired(RuntimeError):
    """账号未登录 / 被踢到 TikTok 登录页：跳过本号自动化，继续下一号。"""


def _is_login_required_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, AccountLoginRequired):
        return True
    msg = str(exc or "")
    return "ACCOUNT_LOGIN_REQUIRED" in msg or "TikTok 登录页" in msg


def _is_tiktok_login_page(page) -> bool:
    """新建/跳转后落到登录页（账号失效、掉线、需重新登录）。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if not url or url in ("about:blank", "chrome://newtab/"):
        return False
    # 明确登录入口
    url_hits = (
        "login.tiktok.com",
        "/login",
        "/signup",
        "/sign-up",
        "/passport",
        "/oauth",
        "account/login",
        "phone-or-email",
        "login/phone",
        "login/email",
    )
    if any(k in url for k in url_hits):
        # Studio 上传页 URL 里一般不会带这些；若带了也当登录
        if "tiktokstudio/upload" not in url:
            return True
    # 打开 Studio 却被重定向到非 studio（常见登录落地）
    if "tiktok.com" in url and "tiktokstudio" not in url:
        try:
            body = (page.inner_text("body", timeout=2500) or "")[:4000]
        except Exception:
            body = ""
        body_l = body.lower()
        login_markers = (
            "log in",
            "sign up",
            "登录",
            "登入",
            "註冊",
            "注册",
            "使用手机",
            "手机号",
            "email address",
            "continue with",
            "使用邮箱",
            "password",
            "密码",
        )
        if any(m.lower() in body_l if m.isascii() else m in body for m in login_markers):
            # 排除仍像上传/创作中心的页面
            if not any(
                x in body
                for x in ("选择视频", "Select video", "上传视频", "Upload video", "挂车", "产品链接")
            ):
                return True
    return False


def _assert_not_login_page(page, *, where: str = "") -> None:
    if page is None:
        return
    try:
        # 给重定向上登录页一点时间
        page.wait_for_timeout(400)
    except Exception:
        pass
    if not _is_tiktok_login_page(page):
        return
    try:
        url = page.url
    except Exception:
        url = "?"
    where_s = f"（{where}）" if where else ""
    logger.error(
        "检测到 TikTok 登录页%s，跳过本账号自动化: %s",
        where_s,
        url,
    )
    raise AccountLoginRequired(
        f"ACCOUNT_LOGIN_REQUIRED: 账号需重新登录 TikTok{where_s}，已跳过。url={url}"
    )


def _goto_upload_with_retry(page, target: str, attempts: int = 3) -> None:
    """打开上传页；SOCKS/代理类错误重试几次，避免秒失败空跑。"""
    last: BaseException | None = None
    for i in range(1, max(1, attempts) + 1):
        try:
            logger.info("打开上传页%s: %s", f"（第{i}次）" if attempts > 1 else "", target)
            page.goto(target, wait_until="domcontentloaded", timeout=120000)
            try:
                page.wait_for_timeout(600)
            except Exception:
                pass
            _assert_not_login_page(page, where=f"打开上传页第{i}次")
            return
        except AccountLoginRequired:
            raise
        except Exception as e:
            last = e
            if not _is_proxy_net_error(e):
                raise
            logger.warning(
                "打开上传页代理/网络失败 (%s/%s): %s",
                i,
                attempts,
                str(e).split("\n", 1)[0][:180],
            )
            if i >= attempts:
                break
            try:
                page.wait_for_timeout(1500 * i)
            except Exception:
                pass
    raise ProxyNetworkError(
        f"PROXY_NETWORK_FAIL: 打开上传页失败（已重试{attempts}次，请检查该窗口 SOCKS 代理）: "
        f"{str(last).split(chr(10), 1)[0][:160]}"
    ) from last


def _connect_and_open_fresh(self, ws_endpoint: str):
    """CDP 连接后立刻新建上传页；禁止先附着旧标签再换页。"""
    from playwright.sync_api import sync_playwright

    target = _resolve_upload_url(self)
    self._pw = sync_playwright().start()
    self._browser = self._pw.chromium.connect_over_cdp(ws_endpoint)
    contexts = self._browser.contexts
    if not contexts:
        raise RuntimeError("CDP 连接成功但没有 browser context")
    context = contexts[0]

    # 先建新标签并立刻关掉所有旧标签，避免用户看到「旧页被操作后又被关掉」
    new_page = context.new_page()
    self.page = new_page
    self._upload_page_ready = True  # type: ignore[attr-defined]
    _close_other_pages(context, new_page)

    logger.info("新建打开上传页: %s", target)
    _goto_upload_with_retry(new_page, target, attempts=3)
    try:
        new_page.wait_for_timeout(800)
    except Exception:
        pass

    try:
        self._ensure_browser_zoom_100(new_page)
    except Exception:
        pass
    try:
        self._normalize_viewport(new_page)
    except Exception:
        pass

    discard_unsaved_draft_fixed(self, new_page)
    logger.info("已连接 Playwright，工作页: %s", new_page.url)
    return new_page


def _file_input_count(page) -> int:
    try:
        return int(page.locator('input[type="file"]').count())
    except Exception:
        return 0


def _looks_like_upload_stage(page) -> bool:
    """是否已处于可选视频/编辑上传的阶段（而不是 Studio 首页）。"""
    if _file_input_count(page) > 0:
        return True
    markers = ("选择视频", "Select video", "选择要上传的视频", "Select video to upload")
    try:
        body = page.inner_text("body") or ""
    except Exception:
        body = ""
    if any(m in body for m in markers):
        return True
    # 首页特征
    if "关键指标" in body or "最近的发布内容" in body:
        return False
    return False


def _click_sidebar_upload(page) -> bool:
    """点击左侧粉色「上传」入口（不是「作品」）。"""
    selectors = [
        'aside a:has-text("上传")',
        'aside button:has-text("上传")',
        'nav a:has-text("上传")',
        '[class*="sidebar"] a:has-text("上传")',
        '[class*="Sidebar"] a:has-text("上传")',
        'a[href*="tiktokstudio/upload"]',
        'button:has-text("+ 上传")',
        'button:has-text("上传")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if _visible(loc, 800):
                loc.click(timeout=3000)
                logger.info("已点击上传入口: %s", sel)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    # 精确文本，避免点到「作品」
    try:
        btn = page.get_by_role("link", name="上传")
        if btn.count() and _visible(btn.first, 800):
            btn.first.click(timeout=3000)
            logger.info("已点击上传入口: role=link 上传")
            page.wait_for_timeout(1200)
            return True
    except Exception:
        pass
    try:
        btn = page.get_by_role("button", name="上传")
        if btn.count() and _visible(btn.first, 800):
            btn.first.click(timeout=3000)
            logger.info("已点击上传入口: role=button 上传")
            page.wait_for_timeout(1200)
            return True
    except Exception:
        pass
    return False


def ensure_upload_stage(self, page):
    """打开网址后确保进入「上传视频」阶段；若落在首页则点「上传」，绝不点「作品」。"""
    working = page if _page_alive(page) else getattr(self, "page", None)
    if not _page_alive(working):
        raise RuntimeError("页面已关闭，无法进入上传阶段")

    _assert_not_login_page(working, where="进入上传阶段前")

    target = _resolve_upload_url(self)
    # URL 不对时先回到上传地址
    try:
        cur = (working.url or "").lower()
    except Exception:
        cur = ""
    if "tiktokstudio/upload" not in cur:
        logger.info("当前不在上传 URL，跳转: %s (was %s)", target, cur)
        working.goto(target, wait_until="domcontentloaded", timeout=120000)
        working.wait_for_timeout(1000)
        _assert_not_login_page(working, where="跳转上传 URL 后")

    discard_unsaved_draft_fixed(self, working)

    if _looks_like_upload_stage(working):
        logger.info("已在上传视频阶段（检测到 file input / 选择视频）")
        self.page = working
        return working

    logger.warning("打开后未进入上传阶段（可能停在首页），点击侧栏「上传」…")
    if not _click_sidebar_upload(working):
        # 再强刷一次上传 URL
        working.goto(target, wait_until="domcontentloaded", timeout=120000)
        working.wait_for_timeout(1200)
        _assert_not_login_page(working, where="强刷上传 URL 后")
        discard_unsaved_draft_fixed(self, working)
        _click_sidebar_upload(working)

    _assert_not_login_page(working, where="等待进入上传阶段时")

    for _ in range(10):
        if _looks_like_upload_stage(working):
            logger.info("已进入上传视频阶段")
            self.page = working
            return working
        try:
            working.wait_for_timeout(500)
        except Exception:
            break
        _assert_not_login_page(working, where="轮询上传阶段时")

    # 最后兜底：即使判断失败也返回当前页，让后续 set_input_files 报更明确错误
    _assert_not_login_page(working, where="上传阶段兜底")
    logger.warning("未能确认上传阶段 UI，继续尝试。url=%s", getattr(working, "url", ""))
    self.page = working
    return working


def _recover_page_if_closed(self, page, *, reopen_upload: bool = False):
    """选视频后若原 page 被关，切换到 context 里最新可用页；必要时新建上传页。"""
    if _page_alive(page):
        self.page = page
        return page
    logger.warning("工作页已关闭，尝试切换/重建上传页…")
    browser = getattr(self, "_browser", None)
    try:
        contexts = list(getattr(browser, "contexts", None) or []) if browser else []
    except Exception:
        contexts = []
    if not browser or not contexts:
        raise RuntimeError("浏览器已关闭（TargetClosed）：比特窗口可能被关掉或代理中断")

    context = contexts[0]
    try:
        pages = list(context.pages)
    except Exception:
        pages = []

    # 优先带 upload 的存活页
    for p in reversed(pages):
        if not _page_alive(p):
            continue
        try:
            if "upload" in ((p.url or "").lower()):
                self.page = p
                logger.info("已切换到上传相关页: %s", p.url)
                return p
        except Exception:
            continue
    for p in reversed(pages):
        if _page_alive(p):
            self.page = p
            logger.info("已切换到最新页: %s", getattr(self.page, "url", ""))
            if reopen_upload or not _is_studio_upload_page(p):
                try:
                    target = _resolve_upload_url(self)
                    _goto_upload_with_retry(p, target, attempts=2)
                    discard_unsaved_draft_fixed(self, p)
                except Exception as e:
                    logger.warning("旧页重开上传地址失败，将新建标签: %s", e)
                    break
                else:
                    return p
            return p

    # 无可用页：新建标签打开上传页（不强制关掉其它页，避免误杀）
    try:
        new_page = context.new_page()
    except Exception as e:
        raise RuntimeError(f"浏览器上下文不可用，无法新建页面: {e}") from e
    self.page = new_page
    self._upload_page_ready = True  # type: ignore[attr-defined]
    target = _resolve_upload_url(self)
    logger.info("无存活标签，新建上传页: %s", target)
    _goto_upload_with_retry(new_page, target, attempts=3)
    try:
        new_page.wait_for_timeout(800)
    except Exception:
        pass
    discard_unsaved_draft_fixed(self, new_page)
    return new_page


def _pick_video_file_input(page):
    """必须选 accept=video/* 的 input；原逻辑用最后一个 input，会误选照片框 image/*。"""
    inputs = page.locator('input[type="file"]')
    count = inputs.count()
    if count == 0:
        return None, 0, "none"

    # 优先显式 video
    for i in range(count):
        inp = inputs.nth(i)
        try:
            accept = (inp.get_attribute("accept") or "").lower()
        except Exception:
            accept = ""
        if "video" in accept:
            return inp, i, accept or "video"

    # 其次：非 image 的通用 file
    for i in range(count):
        inp = inputs.nth(i)
        try:
            accept = (inp.get_attribute("accept") or "").lower()
        except Exception:
            accept = ""
        if "image" in accept and "video" not in accept:
            continue
        return inp, i, accept or "any"

    # 最后才退回第一个（仍避免最后一个照片 input）
    inp = inputs.first
    try:
        accept = (inp.get_attribute("accept") or "").lower()
    except Exception:
        accept = ""
    return inp, 0, accept or "first"


def _upload_video_via_video_input(self, page, video_path) -> None:
    from pathlib import Path

    video_path = Path(video_path)
    if not video_path.exists():
        raise RuntimeError(f"视频不存在: {video_path}")

    page = ensure_upload_stage(self, page)

    # 若当前在「照片」页签，先切回「视频」
    try:
        tab = page.get_by_role("tab", name="视频")
        if tab.count() and _visible(tab.first, 500):
            tab.first.click(timeout=2000)
            page.wait_for_timeout(400)
            logger.info("已切换到「视频」页签")
    except Exception:
        try:
            loc = page.locator('button:has-text("视频"), div[role="tab"]:has-text("视频")').first
            if _visible(loc, 500):
                loc.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:
            pass

    target, idx, accept = _pick_video_file_input(page)
    if target is None:
        # 尝试点「选择视频」露出 input
        for text in ("选择视频", "Select video", "选择要上传的视频"):
            try:
                page.get_by_text(text, exact=False).first.click(timeout=2000)
                page.wait_for_timeout(600)
            except Exception:
                continue
        target, idx, accept = _pick_video_file_input(page)
    if target is None:
        raise RuntimeError("找不到视频上传 input[type=file][accept*=video]")

    logger.info("使用文件框 input[%s] accept=%s 上传: %s", idx, accept, video_path.name)
    if "image" in accept and "video" not in accept:
        raise RuntimeError(f"拒绝使用照片上传框 accept={accept}，请检查页面是否在「视频」页签")

    target.set_input_files(str(video_path))
    logger.info("已选择视频文件: %s", video_path.name)
    self.page = page


def _wait_upload_ready_with_progress(self, page, timeout_sec: int = 180):
    import time

    page = _recover_page_if_closed(self, page, reopen_upload=False)
    deadline = time.time() + max(30, int(timeout_sec or 180))
    last_log = 0.0
    caption_sels = list(self.SELECTORS.get("caption") or [])
    post_sels = list(self.SELECTORS.get("post_btn") or [])
    sels = caption_sels + post_sels
    closed_hits = 0

    while time.time() < deadline:
        try:
            page = _recover_page_if_closed(self, page, reopen_upload=closed_hits >= 1)
            for sel in sels:
                try:
                    if page.locator(sel).first.is_visible(timeout=400):
                        logger.info("上传界面已就绪 (%s)", sel)
                        self.page = page
                        return page
                except Exception as e:
                    if _is_target_closed_error(e):
                        raise
                    continue
            now = time.time()
            if now - last_log >= 10:
                last_log = now
                left = int(deadline - now)
                try:
                    body = page.inner_text("body") or ""
                except Exception as e:
                    if _is_target_closed_error(e):
                        raise
                    body = ""
                stuck = "选择要上传的视频" in body or "选择视频" in body
                logger.info(
                    "等待上传编辑页… 剩余约 %ss%s",
                    left,
                    "（仍停在选择视频页，可能未真正选中视频）" if stuck else "",
                )
            page.wait_for_timeout(800)
        except Exception as e:
            if not _is_target_closed_error(e):
                raise
            closed_hits += 1
            logger.warning(
                "等待上传就绪时页面关闭 (#%s)，尝试恢复: %s",
                closed_hits,
                str(e).split("\n", 1)[0][:160],
            )
            if closed_hits >= 6:
                raise RuntimeError(
                    "上传页反复关闭（TargetClosed），请检查比特窗口是否被关、代理是否中断"
                ) from e
            try:
                page = _recover_page_if_closed(
                    self, getattr(self, "page", None), reopen_upload=True
                )
            except Exception as e2:
                logger.warning("恢复上传页失败: %s", e2)
                raise RuntimeError(
                    f"浏览器页面已关闭且无法恢复: {e2}"
                ) from e2
            continue

    logger.warning("等待上传就绪超时，继续尝试后续步骤")
    self.page = page
    try:
        body = page.inner_text("body") or ""
    except Exception:
        body = ""
    if "选择要上传的视频" in body or "选择视频" in body or "Select video" in body:
        raise RuntimeError("上传就绪超时：仍停在选择视频页，视频可能未选中")
    return page


def ensure_desktop_layout(self, page, width: int | None = None, height: int | None = None):
    """恢复/放大比特窗口并强制桌面视口，避免最小化后点不到「添加」。"""
    if not _page_alive(page):
        page = _recover_page_if_closed(self, page)

    vw = width or int((getattr(self, "viewport", None) or (1920, 1080))[0] or 1920)
    vh = height or int((getattr(self, "viewport", None) or (1920, 1080))[1] or 1080)
    # 挂车区域需要足够高度，下限抬高
    vw = max(vw, 1600)
    vh = max(vh, 1000)

    try:
        session = page.context.new_cdp_session(page)
        try:
            info = session.send("Browser.getWindowForTarget")
            wid = info.get("windowId")
            if wid is not None:
                # 先从最小化恢复，再设尺寸
                session.send(
                    "Browser.setWindowBounds",
                    {"windowId": wid, "bounds": {"windowState": "normal"}},
                )
                session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": wid,
                        "bounds": {
                            "windowState": "normal",
                            "left": 0,
                            "top": 0,
                            "width": vw,
                            "height": vh,
                        },
                    },
                )
                logger.info("已恢复并调整浏览器窗口到 %sx%s", vw, vh)
        except Exception as e:
            logger.warning("调整浏览器窗口失败（继续强制 CSS 视口）: %s", e)

        session.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": vw,
                "height": vh,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        logger.info("已强制 CSS 视口 %sx%s@1x（挂车前）", vw, vh)
    except Exception as e:
        logger.warning("ensure_desktop_layout CDP 失败: %s", e)

    try:
        self._ensure_browser_zoom_100(page)
    except Exception:
        pass

    # 把「添加链接」滚进可视区，避免窄屏/最小化后按钮在屏幕外
    try:
        page.evaluate(
            """() => {
              const nodes = [...document.querySelectorAll('div,span,h2,h3,section,label')];
              const hit = nodes.find(e => {
                const t = (e.innerText || '').replace(/\\s+/g,' ').trim();
                return t === '添加链接' || t === 'Add link';
              });
              if (hit) hit.scrollIntoView({block:'center', inline:'nearest'});
              const add = [...document.querySelectorAll('button,[role="button"],div,span')]
                .find(e => {
                  const t = (e.innerText || '').replace(/\\s+/g,' ').trim();
                  if (!(t === '添加' || t === '+ 添加' || t === '+添加')) return false;
                  const r = e.getBoundingClientRect();
                  return r.width >= 180 && r.width <= 450;
                });
              if (add) add.scrollIntoView({block:'center', inline:'nearest'});
            }"""
        )
        page.wait_for_timeout(400)
    except Exception:
        pass
    return page


def _click_wide_add_button(page) -> bool:
    """点击「添加链接」下方的中等宽度「+ 添加」（约 180–450px）。

    注意：不要选最宽的候选——曾误点 width≈1190 的整行容器，看起来像点了但不出挂车弹窗。
    """
    try:
        handle = page.evaluate_handle(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g,' ').trim();
              const isAddText = (t) => (
                t === '添加' || t === '+ 添加' || t === '+添加' || t === '＋ 添加' || t === '＋添加'
                || t === 'Add' || t === '+ Add' || t === '+Add'
                || t === 'เพิ่ม' || t === '+ เพิ่ม' || t === '+เพิ่ม'
              );
              const isLinkTitle = (t) => (
                t === '添加链接' || t === 'Add link' || t === 'Add product' || t === '添加商品'
                || t === 'เพิ่มลิงก์' || t.includes('添加链接') || t.includes('Add link')
              );
              // 先找「添加链接」标题，限定在其附近找按钮
              const title = [...document.querySelectorAll('div,span,h2,h3,section,label,p')]
                .find(e => {
                  const t = norm(e.innerText || e.textContent);
                  return t && t.length <= 24 && isLinkTitle(t);
                });
              const titleBox = title ? title.getBoundingClientRect() : null;

              const nodes = [...document.querySelectorAll('button,[role="button"]')];
              const cands = [];
              for (const el of nodes) {
                // 只用自身短文本，避免父容器把整段「添加链接…」算进来变成 1190 宽
                const own = norm(el.childNodes && [...el.childNodes]
                  .filter(n => n.nodeType === 3)
                  .map(n => n.textContent).join('') || '');
                const t = own || norm((el.innerText || '').split('\\n')[0] || '');
                if (!isAddText(t)) continue;
                if ((el.innerText || '').length > 16) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 180 || r.width > 450) continue; // 排除 47 小图标与 1190 假宽条
                if (r.height < 28 || r.height > 80) continue;
                if (r.bottom < 0 || r.top > innerHeight) continue;
                // 有标题时：按钮应在标题下方不远处
                if (titleBox) {
                  if (r.top + 8 < titleBox.top) continue;
                  if (r.top - titleBox.bottom > 220) continue;
                }
                cands.push({
                  el,
                  w: r.width,
                  dist: titleBox ? Math.abs(r.top - titleBox.bottom) : 0,
                });
              }
              // 优先：离标题近、宽度适中（约 280 的真实宽按钮）
              cands.sort((a, b) => (a.dist - b.dist) || Math.abs(a.w - 280) - Math.abs(b.w - 280));
              return cands.length ? cands[0].el : null;
            }"""
        )
        el = handle.as_element()
        if el is None:
            return False
        box = el.bounding_box()
        el.scroll_into_view_if_needed(timeout=3000)
        el.click(timeout=5000)
        logger.info(
            "已点击宽「添加」按钮 width=%.0f",
            (box or {}).get("width") or 0,
        )
        page.wait_for_timeout(800)
        return True
    except Exception as e:
        logger.warning("宽「添加」按钮点击失败: %s", e)
        return False


def _modal_root_visible(page) -> bool:
    sels = (
        '[role="dialog"]',
        '[class*="TUXModal"]',
        '[class*="Modal"]',
        '[class*="modal"]',
        '[class*="Dialog"]',
    )
    for sel in sels:
        try:
            loc = page.locator(sel).last
            if _visible(loc, 400):
                return True
        except Exception:
            continue
    return False


def _click_modal_primary_fixed(self, page, labels, wait_ms: int = 12000) -> bool:
    """点弹窗右下角主按钮；避免只 hover 不点、或点到 disabled 灰按钮。"""
    import re
    import time

    labels = list(labels or ())
    if not labels:
        return False
    # 最后一步「添加」常见同义/大小写
    lower = {x.lower() for x in labels if isinstance(x, str)}
    if "添加" in labels or "add" in lower:
        for extra in (
            "添加",
            "Add",
            "ADD",
            "Add to video",
            "添加到视频",
            "เพิ่ม",
            "เพิ่มลงในวิดีโอ",
        ):
            if extra not in labels:
                labels.append(extra)
    if any(x in labels for x in ("下一步", "Next")) or "next" in lower:
        for extra in ("下一步", "Next", "NEXT", "ถัดไป"):
            if extra not in labels:
                labels.append(extra)
    deadline = time.time() + max(3.0, wait_ms / 1000.0)
    logged_dump = False

    js_find = """(labels) => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const want = (labels || []).map(norm).filter(Boolean);
      const matchText = (t) => {
        const s = norm(t);
        if (!s) return false;
        for (const w of want) {
          if (s === w) return true;
          if (s === ('+ ' + w) || s === ('+' + w) || s === ('＋' + w) || s === ('＋ ' + w)) return true;
          // has-text：短文本包含目标（避免点到「添加链接」长文案）
          if (s.length <= Math.max(12, w.length + 6) && s.includes(w)) return true;
        }
        return false;
      };
      const roots = [...document.querySelectorAll(
        '[role="dialog"], [class*="TUXModal"], [class*="Modal"], [class*="Dialog"], [class*="drawer"], [class*="Drawer"], [class*="Popup"], [class*="popup"]'
      )].filter(r => {
        const st = getComputedStyle(r);
        const box = r.getBoundingClientRect();
        return st.display !== 'none' && st.visibility !== 'hidden' && box.width > 80 && box.height > 80;
      });
      // 底栏常挂在 portal：在所有可见弹层 + body 里找
      const scopes = roots.length ? roots : [document.body];
      if (roots.length) scopes.push(document.body);
      const seen = new Set();
      const nodes = [];
      for (const scope of scopes) {
        for (const el of scope.querySelectorAll(
          'button, [role="button"], div[class*="Button"], span[class*="Button"], div[class*="btn"], span[class*="btn"]'
        )) {
          if (seen.has(el)) continue;
          seen.add(el);
          nodes.push(el);
        }
      }
      const scope = roots.length ? roots[roots.length - 1] : document.body;
      const cands = [];
      for (const el of nodes) {
        const own = norm(el.innerText || el.textContent);
        const childTexts = [...el.querySelectorAll('span,div,p,label')]
          .map(n => norm(n.innerText || n.textContent))
          .filter(Boolean);
        const texts = [own, ...childTexts];
        if (!texts.some(matchText)) continue;
        // 排除明显不是主按钮的长文案
        if (own.length > 20) continue;
        if (own.includes('添加链接') || own.includes('Add link')) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 36 || r.height < 18) continue;
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) continue;
        const disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true'
          || el.getAttribute('data-disabled') === 'true'
          || /disabled|is-disabled|btn-disabled/i.test(el.className || ''));
        const pe = st.pointerEvents === 'none';
        // 即便略出视口也收录，稍后 scroll
        cands.push({
          el,
          text: own,
          disabled: disabled || pe,
          x: r.x + r.width / 2,
          y: r.y + r.height / 2,
          w: r.width,
          h: r.height,
          bottom: r.bottom,
          right: r.right,
          inView: r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth,
          inModal: roots.some(root => root.contains(el)),
          // 主页面宽「+ 添加」通常 >450；弹窗主按钮一般 80–320
          tooWide: r.width > 360,
        });
      }
      // 有可见弹窗时：只允许点弹窗内按钮，避免点到主页面「添加」却报挂车完成
      let pool = cands;
      if (roots.length) {
        const inM = cands.filter(c => c.inModal && !c.tooWide);
        if (inM.length) pool = inM;
        else pool = cands.filter(c => c.inModal);
      }
      pool.sort((a, b) => {
        if (a.disabled !== b.disabled) return a.disabled ? 1 : -1;
        if (a.inModal !== b.inModal) return a.inModal ? -1 : 1;
        if (a.tooWide !== b.tooWide) return a.tooWide ? 1 : -1;
        if (a.inView !== b.inView) return a.inView ? -1 : 1;
        return (b.bottom + b.right) - (a.bottom + a.right) || (b.w * b.h) - (a.w * a.h);
      });
      if (!pool.length) {
        const dump = nodes.slice(0, 80).map(el => {
          const t = norm(el.innerText || el.textContent);
          if (!t || t.length > 24) return null;
          const r = el.getBoundingClientRect();
          return {t, w: Math.round(r.width), h: Math.round(r.height), y: Math.round(r.y)};
        }).filter(Boolean);
        return {miss: true, dump, scopeTag: scope.tagName, scopeClass: (scope.className||'').toString().slice(0,80)};
      }
      const best = pool[0];
      try { best.el.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
      const r2 = best.el.getBoundingClientRect();
      return {
        miss: false,
        text: best.text,
        disabled: best.disabled,
        x: r2.x + r2.width / 2,
        y: r2.y + r2.height / 2,
        w: r2.width,
        h: r2.height,
      };
    }"""

    def _playwright_fallback() -> bool:
        """原逻辑风格：dialog / page 上 has-text / get_by_role。"""
        pattern = "|".join(re.escape(x) for x in labels if x)
        if not pattern:
            return False
        rx = re.compile(f"^\\s*(\\+\\s*)?({pattern})\\s*$", re.I)
        scopes = []
        for sel in (
            '[role="dialog"]',
            '[class*="TUXModal"]',
            '[class*="Modal"]',
            '[class*="Dialog"]',
        ):
            try:
                loc = page.locator(sel)
                n = min(loc.count(), 5)
                for i in range(n - 1, -1, -1):
                    scopes.append(loc.nth(i))
            except Exception:
                pass
        scopes.append(page)
        for scope in scopes:
            for label in labels:
                try:
                    btn = scope.get_by_role("button", name=rx)
                    if btn.count() == 0:
                        btn = scope.locator(f'button:has-text("{label}")')
                    if btn.count() == 0:
                        btn = scope.get_by_text(label, exact=True)
                    if btn.count() == 0:
                        continue
                    target = btn.last
                    if not _visible(target, 800):
                        continue
                    try:
                        target.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    try:
                        target.click(timeout=2500)
                    except Exception:
                        target.click(timeout=2500, force=True)
                    logger.info("已点击弹窗主按钮「%s」(playwright fallback)", label)
                    page.wait_for_timeout(500)
                    return True
                except Exception:
                    continue
        return False

    while time.time() < deadline:
        try:
            info = page.evaluate(js_find, labels)
        except Exception as e:
            logger.debug("查找弹窗主按钮失败: %s", e)
            info = None

        if not info or info.get("miss"):
            if info and info.get("dump") is not None and not logged_dump:
                logger.info(
                    "弹窗主按钮未匹配 labels=%s scope=%s.%s dump=%s",
                    labels,
                    info.get("scopeTag"),
                    info.get("scopeClass"),
                    info.get("dump")[:25],
                )
                logged_dump = True
            if _playwright_fallback():
                return True
            page.wait_for_timeout(280)
            continue

        if info.get("disabled"):
            logger.info(
                "弹窗主按钮「%s」仍为禁用/不可点，等待可用… text=%s",
                labels[0],
                info.get("text"),
            )
            page.wait_for_timeout(400)
            continue

        x, y = float(info["x"]), float(info["y"])
        label = info.get("text") or labels[0]
        try:
            page.mouse.move(x, y)
            page.wait_for_timeout(80)
            page.mouse.down()
            page.wait_for_timeout(60)
            page.mouse.up()
            logger.info(
                "已点击弹窗主按钮「%s」(mouse %.0f,%.0f size=%.0fx%.0f)",
                label,
                x,
                y,
                float(info.get("w") or 0),
                float(info.get("h") or 0),
            )
            page.wait_for_timeout(500)
            return True
        except Exception as e1:
            logger.debug("mouse 点击主按钮失败: %s", e1)

        if _playwright_fallback():
            return True

        try:
            # DOM 直接 click 右下角匹配按钮
            clicked = page.evaluate(
                """(labels) => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const want = (labels || []).map(norm);
                  const roots = [...document.querySelectorAll(
                    '[role="dialog"], [class*="TUXModal"], [class*="Modal"], [class*="Dialog"]'
                  )];
                  const scope = roots.length ? roots[roots.length - 1] : document.body;
                  const nodes = [...scope.querySelectorAll('button, [role="button"]')];
                  let best = null, bestScore = -1;
                  for (const el of nodes) {
                    const t = norm(el.innerText || el.textContent);
                    if (!want.some(w => t === w || (t.length <= 12 && t.includes(w)))) continue;
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 36 || r.height < 18) continue;
                    const score = r.bottom + r.right;
                    if (score > bestScore) { best = el; bestScore = score; }
                  }
                  if (!best) return false;
                  best.click();
                  return true;
                }""",
                labels,
            )
            if clicked:
                logger.info("已点击弹窗主按钮「%s」(DOM click)", labels[0])
                page.wait_for_timeout(500)
                return True
        except Exception as e2:
            logger.debug("DOM 点击主按钮失败: %s", e2)

        page.wait_for_timeout(300)

    # 最后再兜底一次 Playwright
    if _playwright_fallback():
        return True
    logger.warning("未点到弹窗主按钮 labels=%s", labels)
    return False


def apply_patches() -> None:
    import tiktok_uploader

    cls = tiktok_uploader.TikTokStudioUploader
    original_attach = cls._attach_product
    original_click_add = getattr(cls, "_click_add_under_link_section", None)

    cls._discard_unsaved_draft = discard_unsaved_draft_fixed  # type: ignore[method-assign]
    cls._dismiss_blocking_modals = dismiss_blocking_modals_fixed  # type: ignore[method-assign]

    def connect_fixed(self, ws_endpoint: str):
        page = _connect_and_open_fresh(self, ws_endpoint)
        page = ensure_upload_stage(self, page)
        return ensure_desktop_layout(self, page)

    def open_upload_page_fixed(self, force: bool = False):
        """优先复用 connect 已打开的上传页，避免 force 误拆导致页面被关。"""
        page = getattr(self, "page", None)
        alive = _page_alive(page)
        on_upload = alive and (
            getattr(self, "_upload_page_ready", False)
            or _is_studio_upload_page(page)
            or _looks_like_upload_stage(page)
        )
        # 已在可用上传页：直接复用（含 publisher 预打开常带 force=True 的情况）
        if on_upload:
            logger.info("复用已新建的上传页: %s", page.url)
            page = ensure_upload_stage(self, page)
            return ensure_desktop_layout(self, page)

        logger.info(
            "工作页%s，重新新建上传页",
            "强制刷新" if force else "未就绪/非上传页",
        )
        if getattr(self, "_browser", None) is not None:
            page = _open_fresh_upload_page(self, page)
            page = ensure_upload_stage(self, page)
            return ensure_desktop_layout(self, page)
        raise RuntimeError("请先 connect()")

    def _goto_upload_fixed(self, page):
        working = _use_current_upload_page(self, page)
        working = ensure_upload_stage(self, working)
        return ensure_desktop_layout(self, working)

    def _wait_upload_ready_fixed(self, page, timeout_sec: int = 180):
        return _wait_upload_ready_with_progress(self, page, timeout_sec)

    def _upload_video_fixed(self, page, video_path):
        last_err: BaseException | None = None
        timeout = int(getattr(self, "upload_timeout", 0) or 0) or min(
            180, int(getattr(self, "content_check_timeout", 180) or 180)
        )
        for attempt in range(1, 4):
            try:
                if attempt == 1:
                    page = ensure_upload_stage(self, page)
                    page = ensure_desktop_layout(self, page)
                else:
                    logger.warning(
                        "上传过程页面关闭，恢复后重试 (%s/3): %s",
                        attempt,
                        str(last_err).split("\n", 1)[0][:160] if last_err else "",
                    )
                    page = _recover_page_if_closed(
                        self, getattr(self, "page", None), reopen_upload=True
                    )
                    page = ensure_upload_stage(self, page)
                    page = ensure_desktop_layout(self, page)
                _upload_video_via_video_input(self, page, video_path)
                page = _recover_page_if_closed(
                    self, getattr(self, "page", None) or page, reopen_upload=False
                )
                try:
                    page.wait_for_timeout(1000)
                except Exception as e:
                    if _is_target_closed_error(e):
                        page = _recover_page_if_closed(
                            self, getattr(self, "page", None), reopen_upload=True
                        )
                    else:
                        page = _recover_page_if_closed(self, page)
                return _wait_upload_ready_with_progress(self, page, timeout)
            except Exception as e:
                last_err = e
                if not _is_target_closed_error(e) and "无法恢复" not in str(e):
                    raise
                if attempt >= 3:
                    break
                continue
        raise RuntimeError(
            f"上传失败：页面/浏览器反复关闭（已重试3次）。"
            f"请确认比特窗口未被关掉，并检查该窗口 SOCKS 代理。"
            f" 最后错误: {str(last_err).split(chr(10), 1)[0][:160]}"
        ) from last_err

    def _attach_product_fixed(self, page, product_id, product_display_name=None):
        page = ensure_desktop_layout(self, page)
        self.page = page
        return original_attach(self, page, product_id, product_display_name)

    def _click_add_under_link_section_fixed(self, page):
        page = ensure_desktop_layout(self, page)
        # 先走原版：历史证明 width≈47/280 能真正弹出挂车；宽按钮曾误点 1190 假条
        if original_click_add is not None:
            try:
                ok = original_click_add(self, page)
                if ok or _modal_root_visible(page):
                    logger.info("原版「添加」已拉起挂车弹窗")
                    return True
            except Exception as e:
                logger.warning("原版点击「添加」异常: %s", e)

        # 原版失败再试约束后的宽按钮（180–450，贴着「添加链接」）
        wide_ok = _click_wide_add_button(page)
        if wide_ok:
            for _ in range(20):  # ~6s
                if _modal_root_visible(page):
                    logger.info("宽「添加」后弹窗已出现")
                    return True
                page.wait_for_timeout(300)
            logger.warning("宽「添加」后未检测到弹窗")
        return False

    cls.connect = connect_fixed  # type: ignore[method-assign]
    cls.open_upload_page = open_upload_page_fixed  # type: ignore[method-assign]
    cls._goto_upload = _goto_upload_fixed  # type: ignore[method-assign]
    cls._wait_upload_ready = _wait_upload_ready_fixed  # type: ignore[method-assign]
    cls._upload_video = _upload_video_fixed  # type: ignore[method-assign]
    cls._attach_product = _attach_product_fixed  # type: ignore[method-assign]
    cls._click_modal_primary = _click_modal_primary_fixed  # type: ignore[method-assign]
    if original_click_add is not None:
        cls._click_add_under_link_section = _click_add_under_link_section_fixed  # type: ignore[method-assign]
    logger.info(
        "已应用补丁：挂车「下一步」强制真实点击；宽「添加」后不点小图标"
    )
