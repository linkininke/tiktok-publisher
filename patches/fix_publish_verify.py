"""发布成功校验：确认真的发出去了，才算成功、才删源视频。

原流程点完「发布」按钮就直接返回 ok=True、消费映射、删除源视频文件，
中间没有任何校验。一旦提交没走完（代理慢、页面卡、被拦），
结果是账号里没视频，本地源文件却已经被删掉，等于永久丢素材。

判定依据按可靠性排序：
  1. 发布接口 /tiktok/web/project/post/v1/ 返回 200 且 status_code=0
  2. 页面弹出「视频已发布」之类的提示
  3. 页面形态旁证（跳走了 / 表单消失 / 描述框被清空）

特别注意：Studio 发布成功后**仍然把视频和文案留在上传页**，
所以「文案还在描述框」绝不能当成失败依据——早期版本就是这么判的，
把已经成功的发布判成失败，导致下一轮重复发同一条视频。

确认不了就抛错，由 publish 的兜底转成 ok=False，
这样映射不会被消费、视频文件会保留，下轮还能重发。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("publisher")

_POST_HINT = re.compile(r"(post|publish|project|create|aweme|item|video)", re.I)
# 真正的发布接口：/tiktok/web/project/post/v1/
_PUBLISH_API = re.compile(r"/project/post/|/aweme/post/|/item/publish", re.I)
_OK_TOAST = re.compile(r"(视频已发布|已发布|发布成功|上传成功|posted|published)", re.I)
_LOCAL_NOISE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])[:/]", re.I)

_DEFAULT_CONFIRM_TIMEOUT = 45
_POLL_INTERVAL_MS = 1000
# 点击后页面要重绘，立刻探测会撞上表单短暂消失的瞬时状态
_SETTLE_MS = 1500

_PAGE_STATE_JS = """() => {
  const cap = document.querySelector('[data-e2e="caption_container"] [contenteditable="true"]')
    || [...document.querySelectorAll('[contenteditable="true"]')].find(e => e.offsetParent !== null);
  const capText = cap ? (cap.innerText || '').trim() : null;
  const btn = document.querySelector('[data-e2e="post_video_button"]')
    || [...document.querySelectorAll('button')]
         .find(b => /^(发布|Post|Publish)$/.test((b.innerText || '').trim()));
  return {
    url: location.href,
    capFound: !!cap,
    capVisible: !!(cap && cap.offsetParent !== null),
    capText: capText === null ? null : capText.slice(0, 120),
    capLen: capText === null ? -1 : capText.length,
    hasFileInput: !!document.querySelector('input[type="file"]'),
    postBtn: btn
      ? {
          text: (btn.innerText || '').trim().slice(0, 20),
          disabled: !!btn.disabled || btn.getAttribute('aria-disabled') === 'true',
        }
      : null,
    bodyText: (document.body ? (document.body.innerText || '') : '').slice(0, 400),
  };
}"""


_POST_BTN_JS = """() => {
  const btn = document.querySelector('[data-e2e="post_video_button"]')
    || [...document.querySelectorAll('button')]
         .find(b => /^(发布|Post|Publish)$/.test((b.innerText || '').trim()));
  if (!btn) return { found: false };
  const cs = getComputedStyle(btn);
  const r = btn.getBoundingClientRect();
  // 只认「看得见的」转圈：按钮里常年挂着不显示的装饰 svg，认它会一直判成提交中
  const spinner = [...btn.querySelectorAll('svg[class*="loading"], [class*="Loading"], [class*="spin"]')]
    .find(el => {
      const b = el.getBoundingClientRect();
      return b.width > 0 && b.height > 0 && getComputedStyle(el).visibility !== 'hidden';
    });
  const up = (document.body.innerText || '').match(/(上传中|Uploading|正在上传)[^\\n]{0,24}|\\b(\\d{1,3})%/);
  return {
    found: true,
    disabled: !!btn.disabled
      || btn.getAttribute('aria-disabled') === 'true'
      || /disabled/i.test(btn.className || '')
      || cs.pointerEvents === 'none',
    busy: !!spinner,
    busyWhat: spinner ? (spinner.tagName + '.' + String(spinner.getAttribute('class') || '')).slice(0, 60) : '',
    upload: up ? up[0].slice(0, 30) : '',
    text: (btn.innerText || '').trim().slice(0, 20),
    opacity: cs.opacity,
    x: r.x + r.width / 2,
    y: r.y + r.height / 2,
    w: Math.round(r.width),
    h: Math.round(r.height),
  };
}"""


def _post_button_state(page) -> dict:
    try:
        return page.evaluate(_POST_BTN_JS) or {}
    except Exception:
        return {}


def ensure_publish_submitted(
    page, watch, attempts: int = 2, window_sec: int = 12, max_wait: int = 60
) -> None:
    """确认点发布真的发出了提交请求，没发出就补点。

    并发开多个窗口时，叠在最上层那个窗口的自动点击有时不生效：
    日志里有「已点击底部发布按钮」，但迟迟没有提交请求，
    必须人工再点一次才会发出去。这里就是把这一下补上。

    防重复提交只认「发布请求已经发出」（watch.sent）和按钮被禁用，
    这两个是确定的。按钮转圈只当作「再多等一轮」的理由：
    它可能是视频还没传完，也可能只是个一直在转的装饰，
    拿它当拦截条件会把补点整个废掉。
    """
    deadline = time.time() + max_wait
    clicks = 0
    busy_waits = 0
    while time.time() < deadline:
        if _wait_for_publish_post(page, watch, window_sec):
            return
        st = _post_button_state(page)
        if not st.get("found"):
            logger.info("发布按钮已不在页面上，交给后面的发布确认判断")
            return
        if st.get("disabled"):
            logger.info("发布按钮已禁用（提交进行中），继续等，不补点")
            continue
        if st.get("busy") and busy_waits < 1:
            busy_waits += 1
            logger.info(
                "发布按钮在转圈（%s%s），可能视频还没传完，再等一轮，不补点",
                st.get("busyWhat") or "未知元素",
                "｜页面: " + st["upload"] if st.get("upload") else "",
            )
            continue
        if clicks >= attempts:
            break
        clicks += 1
        logger.warning(
            "点了发布但 %ss 内没有提交请求（按钮 禁用=%s 转圈=%s%s），补点一次（第 %d 次）",
            window_sec,
            st.get("disabled"),
            st.get("busy"),
            "｜页面: " + st["upload"] if st.get("upload") else "",
            clicks,
        )
        try:
            page.mouse.click(float(st["x"]), float(st["y"]))
            logger.info(
                "补点发布按钮「%s」(mouse %.0f,%.0f size=%sx%s)",
                st.get("text"),
                float(st["x"]),
                float(st["y"]),
                st.get("w"),
                st.get("h"),
            )
        except Exception as e:
            logger.warning("补点发布按钮失败: %s", e)
            break
    if not watch.saw_publish_post():
        logger.warning("等待 %ss、补点 %d 次后仍未见提交请求，转入发布确认判断", max_wait, clicks)


def _wait_for_publish_post(page, watch, seconds: int) -> bool:
    deadline = time.time() + max(1, seconds)
    while time.time() < deadline:
        if watch is not None and watch.saw_publish_post():
            return True
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    return watch is not None and watch.saw_publish_post()


_SUGGEST_JS = """() => {
  const ae = document.activeElement;
  return {
    editing: !!(ae && (ae.isContentEditable
      || (ae.closest && ae.closest('[data-e2e="caption_container"]')))),
  };
}"""

# 找一个点下去不会触发任何东西的位置：描述框上方的空白
_NEUTRAL_POINT_JS = """() => {
  const cap = document.querySelector('[data-e2e="caption_container"]');
  if (!cap) return null;
  const r = cap.getBoundingClientRect();
  const bad = 'button, a, input, textarea, select, [role="button"], [role="tab"],'
    + ' [contenteditable="true"], [data-e2e="caption_container"]';
  for (let dy = 10; dy <= 70; dy += 10) {
    const x = Math.round(r.left + Math.min(60, r.width / 3));
    const y = Math.round(r.top - dy);
    if (y < 60) continue;
    const el = document.elementFromPoint(x, y);
    if (!el || (el.closest && el.closest(bad))) continue;
    return { x, y };
  }
  return null;
}"""


def dismiss_caption_suggest(page) -> None:
    """点发布前让描述框失焦。

    只看 activeElement 是不是描述框——这是精确信号。
    曾经还按「页面上有没有『历史记录』字样」判断话题联想面板，
    实测那串字一直在，永远为真；而且面板开着照样能正常提交，
    并不影响发布，所以那条判断已经去掉。
    """
    try:
        st = page.evaluate(_SUGGEST_JS) or {}
    except Exception:
        return
    if not st.get("editing"):
        return

    logger.info("点发布前：描述框仍处于编辑态，先移开焦点")
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        if (page.evaluate(_SUGGEST_JS) or {}).get("editing"):
            pt = page.evaluate(_NEUTRAL_POINT_JS)
            if pt:
                page.mouse.click(float(pt["x"]), float(pt["y"]))
                page.wait_for_timeout(200)
        logger.info(
            "失焦后编辑态=%s", (page.evaluate(_SUGGEST_JS) or {}).get("editing")
        )
    except Exception as e:
        logger.debug("移开描述框焦点失败: %s", e)


def wait_post_button_enabled(page, timeout_sec: int = 120) -> dict:
    """等「发布」按钮真正可用再点。

    视频还在上传时按钮是禁用态，此时点击不会报错也不会提交，
    日志只会留下一条「已点击发布」的假象。等不到也不拦，交给发布后确认兜底。
    """
    deadline = time.time() + max(1, int(timeout_sec))
    state: dict = {}
    waited = False
    while time.time() < deadline:
        try:
            state = page.evaluate(_POST_BTN_JS) or {}
        except Exception as e:
            logger.warning("读取发布按钮状态失败：%s", e)
            return {}
        if state.get("found") and not state.get("disabled"):
            if waited:
                logger.info("发布按钮已可用，准备点击")
            return state
        if not waited:
            logger.info(
                "发布按钮暂不可用（存在=%s 禁用=%s），等待中（视频可能还在上传）…",
                state.get("found"),
                state.get("disabled"),
            )
            waited = True
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
    logger.warning(
        "等了 %ss 发布按钮仍不可用（存在=%s 禁用=%s 透明度=%s），仍尝试点击",
        timeout_sec,
        state.get("found"),
        state.get("disabled"),
        state.get("opacity"),
    )
    return state


_TOAST_JS = """() => {
  const sels = [
    '[role="alert"]', '[role="status"]',
    '[class*="toast"]', '[class*="Toast"]',
    '[class*="notice"]', '[class*="Notice"]',
    '[class*="tips"]', '[class*="Tips"]',
    '[class*="error"]', '[class*="Error"]',
  ].join(', ');
  const out = [];
  for (const el of document.querySelectorAll(sels)) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) continue;
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t && t.length <= 160 && !out.includes(t)) out.push(t);
  }
  return out.slice(0, 6);
}"""


def _toasts(page) -> list:
    try:
        return list(page.evaluate(_TOAST_JS) or [])
    except Exception:
        return []


class _PostWatch:
    """盯住点发布之后发出去的请求。

    按钮变暗说明前端确实提交了，但页面上看不出服务端回了什么。
    只有把 POST 的状态码/失败原因记下来，才能分清是网络没通还是被服务端拒了。
    """

    def __init__(self, page):
        self.page = page
        self.responses = []
        self.failures = []
        # 只记发布接口的请求：用来判断「提交已发出但还没回」，避免补点补出两条
        self.sent = []
        # 结论只算一次：返回体读过就没了，而且这个方法每秒会被轮询调用
        self._verdict = None
        self._started = False

    def _on_response(self, resp):
        try:
            if resp.request.method != "POST":
                return
            self.responses.append(resp)
            # 立刻记一条：确认窗口有 45s，中途被关掉也不至于什么都没留下。
            # 这里只读 url/status，不读 body（在事件回调里读 body 会卡住同步 API）
            if _POST_HINT.search(resp.url):
                logger.info("发布期间 POST %s -> %s", resp.url[:110], resp.status)
        except Exception:
            pass

    def _on_request(self, req):
        try:
            if req.method == "POST" and _PUBLISH_API.search(str(req.url)):
                self.sent.append(str(req.url))
                self._log_text_extra(req)
        except Exception:
            pass

    def _log_text_extra(self, req) -> None:
        """提交体里到底带没带话题实体。

        话题在手机端能不能点、能不能进话题流，取决于提交体里的 text_extra。
        描述框里显示成蓝字不代表提交时带上了，这里是唯一能证实的地方。
        """
        try:
            body = req.post_data or ""
        except Exception:
            return
        if not body:
            return
        names = re.findall(r'"hashtag_name"\s*:\s*"([^"]{1,60})"', body)
        if names:
            logger.info("提交体带话题实体 %s 个: %s", len(names), names[:8])
        elif '"text_extra"' in body:
            logger.warning("提交体 text_extra 里没有话题实体，发出去的话题点不动")

    def _on_failed(self, req):
        try:
            url = str(req.url)
            if _LOCAL_NOISE.search(url):
                # 比特浏览器本地探活之类，和发布无关，别刷屏
                return
            why = str(getattr(req, "failure", "") or "")
            self.failures.append((url, why))
            logger.error("发布期间请求失败: %s | %s", url[:110], why[:120])
        except Exception:
            pass

    def saw_publish_post(self) -> bool:
        """只看有没有发过发布请求，不读返回体（读了就没了）。

        请求刚发出、还没等到返回，也算已发出：
        这时候再补一次点击就会把同一条视频发两遍。
        """
        if self._verdict is not None or self.sent:
            return True
        for resp in self.responses:
            try:
                if _PUBLISH_API.search(resp.url):
                    return True
            except Exception:
                continue
        return False

    def publish_api_result(self):
        """发布接口的结论：True 成功 / False 失败 / None 还没看到。

        HTTP 200 只代表请求送达，TikTok 把真正的结果放在返回体的
        status_code / status_msg 里，所以必须读返回体才能定性。
        """
        if self._verdict is not None:
            return self._verdict
        for resp in self.responses:
            try:
                url = resp.url
                if not _PUBLISH_API.search(url):
                    continue
                status = resp.status
            except Exception:
                continue

            body = ""
            body_err = ""
            try:
                body = (resp.text() or "").replace("\n", " ")
            except Exception as e:
                body_err = str(e)[:80]
            # 不管成功失败都完整记一条，这是唯一能定性的东西
            logger.info(
                "发布接口返回 HTTP %s | body前400=%s",
                status,
                body[:400] if body else f"(读取失败: {body_err})",
            )

            if status != 200:
                self._verdict = (False, f"发布接口 HTTP {status} | {body[:150]}")
                return self._verdict
            m = re.search(r'"status_code"\s*:\s*(-?\d+)', body)
            if m and m.group(1) != "0":
                msg = re.search(r'"status_msg"\s*:\s*"([^"]{0,120})"', body)
                self._verdict = (
                    False,
                    f"发布接口 status_code={m.group(1)}"
                    f"{('｜' + msg.group(1)) if msg else ''} | {body[:150]}",
                )
                return self._verdict
            if not body:
                logger.warning("发布接口返回体读不到，只能按 HTTP 200 认定成功")
            self._verdict = (
                True,
                f"发布接口 HTTP 200{('｜' + body[:150]) if body else ''}",
            )
            return self._verdict
        return None, ""

    def start(self):
        try:
            self.page.on("request", self._on_request)
            self.page.on("response", self._on_response)
            self.page.on("requestfailed", self._on_failed)
            self._started = True
        except Exception as e:
            logger.debug("挂网络监听失败: %s", e)

    def stop(self):
        if not self._started:
            return
        for evt, fn in (
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfailed", self._on_failed),
        ):
            try:
                self.page.remove_listener(evt, fn)
            except Exception:
                pass
        self._started = False

    def report(self) -> None:
        shown = 0
        for resp in reversed(self.responses):
            if shown >= 6:
                break
            try:
                url = resp.url
                status = resp.status
            except Exception:
                continue
            if not _POST_HINT.search(url):
                continue
            shown += 1
            body = ""
            try:
                body = (resp.text() or "")[:200].replace("\n", " ")
            except Exception:
                pass
            logger.error("发布期间 POST %s -> %s | 返回前200=%s", url[:110], status, body)
        if not shown and not self.failures:
            logger.error("发布期间没抓到任何相关 POST 请求（前端可能根本没发出提交）")


def _confirm_timeout() -> int:
    try:
        import yaml

        import config_loader

        root = Path(getattr(config_loader, "ROOT", ".") or ".")
        cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
        v = int((cfg.get("publish") or {}).get("publish_confirm_timeout") or 0)
        return v if v > 0 else _DEFAULT_CONFIRM_TIMEOUT
    except Exception:
        return _DEFAULT_CONFIRM_TIMEOUT


def _page_state(page) -> dict:
    try:
        return page.evaluate(_PAGE_STATE_JS) or {}
    except Exception as e:
        return {"error": str(e)}


def _looks_published(state: dict, expected_caption: str) -> str:
    """页面形态上的旁证，返回非空字符串表示已确认。

    注意：Studio 发布成功后**仍然把视频和文案留在上传页**，
    所以「文案还在描述框」不能当成失败依据，只能作为没有其它证据时的兜底判断。
    """
    url = str(state.get("url") or "")
    if url and "/upload" not in url:
        return f"已离开上传页（当前 {url[:80]}）"
    if not state.get("capFound") and not state.get("hasFileInput"):
        return "上传表单整体消失"
    if state.get("capLen") == 0:
        return "描述框已清空（表单已重置）"

    head = (expected_caption or "").strip().replace("\n", " ")[:12]
    cap_text = str(state.get("capText") or "").replace("\n", " ")
    if head and cap_text and head not in cap_text:
        return "描述框内容已被换掉（表单已重置）"
    return ""


def verify_published(
    page, timeout_sec: int, expected_caption: str = "", watch=None
) -> None:
    try:
        page.wait_for_timeout(_SETTLE_MS)
    except Exception:
        time.sleep(_SETTLE_MS / 1000)

    started = time.time()
    deadline = started + max(5, int(timeout_sec))
    last: dict = {}
    seen_toasts: set[str] = set()
    next_tick = started + 15
    while time.time() < deadline:
        if time.time() >= next_tick:
            next_tick += 15
            logger.info(
                "发布确认中… 已等 %.0fs / 上限 %ss（此时关掉窗口会看不到失败原因）",
                time.time() - started,
                timeout_sec,
            )
        # 最硬的证据：发布接口的返回
        if watch is not None:
            api_ok, detail = watch.publish_api_result()
            if api_ok is True:
                logger.info("发布已确认：%s", detail)
                return
            if api_ok is False:
                raise RuntimeError(f"发布失败：{detail}")

        last = _page_state(page)
        if last.get("error"):
            raise RuntimeError(f"发布未确认：无法读取页面状态（{last['error']}）")

        for t in _toasts(page):
            if t not in seen_toasts:
                seen_toasts.add(t)
                logger.warning("发布后页面提示: %s", t)
            if _OK_TOAST.search(t):
                logger.info("发布已确认：页面提示「%s」", t[:40])
                return

        reason = _looks_published(last, expected_caption)
        if reason:
            logger.info("发布已确认：%s", reason)
            return
        try:
            page.wait_for_timeout(_POLL_INTERVAL_MS)
        except Exception:
            time.sleep(_POLL_INTERVAL_MS / 1000)

    btn = last.get("postBtn") or {}
    logger.error(
        "发布未确认：等了 %ss 既没看到发布接口成功、也没看到「已发布」提示"
        " | url=%s | 描述框字数=%s | 发布按钮=%s 禁用=%s | 文件框=%s",
        timeout_sec,
        str(last.get("url") or "")[:90],
        last.get("capLen"),
        btn.get("text"),
        btn.get("disabled"),
        last.get("hasFileInput"),
    )
    logger.error(
        "发布未确认时页面文字前300=%s",
        str(last.get("bodyText") or "").replace("\n", " ")[:300],
    )
    raise RuntimeError(
        f"发布未确认：点了发布但 {timeout_sec}s 内没等到发布接口成功或「已发布」提示，"
        "本次不算成功、保留视频文件下轮重发"
    )


def apply_publish_verify_patches() -> None:
    import tiktok_uploader

    cls = tiktok_uploader.TikTokStudioUploader
    if getattr(cls, "_publish_verify_patched", False):
        return

    original_post = cls._click_post
    timeout_sec = _confirm_timeout()

    def click_post_verified(self, page):
        dismiss_caption_suggest(page)
        wait_post_button_enabled(page)
        watch = _PostWatch(page)
        watch.start()
        try:
            out = original_post(self, page)
            ensure_publish_submitted(page, watch)
            # _publish_caption 由 fix_caption_hashtags 在 publish 期间挂到实例上
            try:
                verify_published(
                    page,
                    timeout_sec,
                    getattr(self, "_publish_caption", "") or "",
                    watch=watch,
                )
            except Exception:
                watch.report()
                raise
        finally:
            watch.stop()
        return out

    cls._click_post = click_post_verified  # type: ignore[method-assign]
    cls._publish_verify_patched = True  # type: ignore[attr-defined]
    logger.info(
        "已应用补丁：点发布后确认提交请求已发出（没发出就补点），"
        "再按「发布接口返回 + 已发布提示」确认（上限 %ss，未确认则保留视频）",
        timeout_sec,
    )
