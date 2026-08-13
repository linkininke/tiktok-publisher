"""审核不通过（内容可能受限 / 同质化）时不点发布，删视频再点「替换」。

正常处理流程（按客户实际操作）：
    检查区出现红字提示 → 不点发布 → 删掉这个视频 → 到页面右上方点「替换」→ 换下一个
点了发布才会弹出「内容可能受限」的详情框，所以只要判定准，那个弹窗根本不会出现。


原版 _detect_check_status 的第一条规则是：
    if '未发现问题' in body and '内容快速检查' in body: return PASS
页面上「音乐版权检查」是「未发现问题」，「内容快速检查」才是红字「内容可能受限」，
两段文字都在 body 里，于是第一条规则直接判通过，压根走不到不通过关键词那一步，
然后就去点发布 —— 点完 TikTok 弹「内容可能受限」的详情框，卡在那儿发不出去。

这里在原版之前先判两层：
  1. 整页命中明确的不通过措辞（也覆盖那个详情弹窗的文案）；
  2. 只截「内容快速检查」标签后面那一小段，看里面是不是警告，
     这样音乐版权检查的「未发现问题」不会再干扰判断。
判为不通过后，run_with_replace_loop 会点「替换」换下一个视频（最多
publish.max_replace_attempts 个，默认 3）。弹窗开着时优先点弹窗里的「替换视频」。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("fix_review_replace")

# 整页命中即判不通过。含详情弹窗（「违规原因：非原创内容、劣质内容和二维码内容」）
_FAIL_ZH = (
    "内容可能受限",
    "内容受限",
    "非原创内容",
    "劣质内容",
    "不符合推荐资格",
    "同质化",
    "内容重复",
    "重复内容",
    "重复度过高",
    "相似度过高",
    "高度相似",
    "疑似搬运",
    "搬运视频",
    "内容雷同",
)
_FAIL_EN = (
    "content may be restricted",
    "unoriginal",
    "duplicate content",
    "duplicated content",
    "too similar",
    "highly similar",
    "similar to other",
    "repetitive content",
    "reposted content",
    "not eligible for recommendation",
)
# 「内容快速检查」这一段里出现这些字样，就算措辞没见过也判不通过
_SECTION_WARN = (
    "受限",
    "不符合",
    "违规",
    "修改内容",
    "同质化",
    "重复",
    "相似",
    "非原创",
    "劣质",
    "restricted",
    "violat",
    "unoriginal",
    "duplicate",
    "similar",
    "not eligible",
)
_CHECK_LABELS = ("内容快速检查", "Content quick check", "Content check")
_PASS_KEYS = ("内容检查通过", "Content check passed", "检查已通过", "未发现问题")
_PROBE_WORDS = ("检查", "check", "受限", "restrict", "相似", "similar", "重复", "duplicate", "替换", "replace")

# 弹窗容器：TikTok Studio 的弹窗不一定带 role="dialog"
_MODAL_SEL = '[role="dialog"], [class*="TUXModal"], [class*="Modal"], [class*="modal"]'
_MODAL_REPLACE_SEL = (
    'button:has-text("替换视频")',
    'button:has-text("替换")',
    'button:has-text("Replace video")',
    'button:has-text("Replace")',
    '[aria-label*="替换"]',
    '[aria-label*="Replace"]',
)
_MODAL_CLOSE_SEL = (
    '[aria-label*="Close"]',
    '[aria-label*="关闭"]',
    'button:has-text("×")',
    '[class*="close"]',
)

_logged_pages: set[int] = set()


def _first_hit(body: str) -> str:
    for key in _FAIL_ZH:
        if key in body:
            return key
    low = body.lower()
    for key in _FAIL_EN:
        if key in low:
            return key
    return ""


def _check_section(body: str, span: int = 200) -> str:
    """截「内容快速检查」标签后面那一小段。"""
    for label in _CHECK_LABELS:
        i = body.find(label)
        if i >= 0:
            return body[i + len(label) : i + len(label) + span]
    return ""


def _section_warning(body: str) -> str:
    section = _check_section(body)
    if not section:
        return ""
    # 这一段自己说了没问题就不再往下挑刺
    if any(k in section for k in ("未发现问题", "No issues", "no issues")):
        return ""
    low = section.lower()
    for word in _SECTION_WARN:
        if word in section or word in low:
            return word
    return ""


def _snippet(body: str, limit: int = 2) -> str:
    """挑出提示原文。带警告字样的整句优先，纯标题（如「内容快速检查」）排后面。"""
    strong: list[str] = []
    weak: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or len(line) > 160:
            continue
        low = line.lower()
        if not any(w in line or w in low for w in _PROBE_WORDS + _SECTION_WARN):
            continue
        bucket = strong if (len(line) >= 10 and any(w in line or w in low for w in _SECTION_WARN)) else weak
        if line not in bucket:
            bucket.append(line)
    return " | ".join((strong + weak)[:limit])


def _click_guarding_file_chooser(page, do_click):
    """点「替换」会触发选文件对话框。

    Playwright 只在挂了 filechooser 监听时才拦截，否则会弹出系统原生对话框，
    把整个浏览器窗口卡住。这里挂上监听并直接取消，反正下一次 publish 会重新
    导航到上传页、从头传新视频。
    """
    seen = {"n": 0}

    def on_chooser(chooser):
        seen["n"] += 1
        try:
            chooser.set_files([])
        except Exception:
            pass

    try:
        page.on("filechooser", on_chooser)
    except Exception:
        return do_click()
    try:
        return do_click()
    finally:
        if seen["n"]:
            logger.info("已拦下「替换」弹出的选文件对话框（新视频由下一轮上传）")
        try:
            page.remove_listener("filechooser", on_chooser)
        except Exception:
            pass


# 页面右上方那个「替换」：文字短、在视频预览旁边。按位置挑，不认死选择器。
_FIND_REPLACE_JS = """
() => {
  const re = /^(替换视频|替换|Replace video|Replace)$/i;
  const vw = window.innerWidth || 1440, vh = window.innerHeight || 900;
  const best = {node: null, score: -1e9};
  document.querySelectorAll('[data-ttp-replace]').forEach(n => n.removeAttribute('data-ttp-replace'));
  for (const el of document.querySelectorAll('button, [role="button"], a, label, span, div')) {
    const text = (el.innerText || '').trim();
    const aria = el.getAttribute('aria-label') || '';
    if (!re.test(text) && !re.test(aria.trim())) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8 || r.bottom < 0 || r.top > vh) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) continue;
    // 只要最内层那个，父容器跳过
    let inner = false;
    for (const c of el.querySelectorAll('*')) {
      const ct = (c.innerText || '').trim();
      if (re.test(ct) && c.getBoundingClientRect().width >= 8) { inner = true; break; }
    }
    if (inner) continue;
    // 越靠右越靠上分越高
    const score = (r.left + r.width / 2) / vw - (r.top + r.height / 2) / vh;
    if (score > best.score) { best.score = score; best.node = el; }
  }
  if (!best.node) return null;
  best.node.setAttribute('data-ttp-replace', '1');
  const r = best.node.getBoundingClientRect();
  return {text: (best.node.innerText || best.node.getAttribute('aria-label') || '').trim().slice(0, 20),
          x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
}
"""


def _visible_modal(page):
    try:
        loc = page.locator(_MODAL_SEL)
        for i in range(min(loc.count(), 8)):
            node = loc.nth(i)
            if node.is_visible():
                return node
    except Exception:
        pass
    return None


def apply_review_replace_patches() -> None:
    import tiktok_uploader
    from tiktok_uploader import CheckStatus

    original_detect = tiktok_uploader.TikTokStudioUploader._detect_check_status
    original_replace = tiktok_uploader.TikTokStudioUploader.click_replace_for_next_video

    def detect_restricted_first(self, page):
        try:
            body = page.inner_text("body") or ""
        except Exception:
            return original_detect(self, page)

        hit = _first_hit(body) or _section_warning(body)
        if hit:
            logger.warning(
                "内容检查不通过（命中「%s」）：%s | 不点发布，改点「替换」换视频",
                hit,
                _snippet(body) or "(没取到提示原文)",
            )
            return CheckStatus.FAIL

        status = original_detect(self, page)
        # 原版在「判不出状态但发布按钮可点」时会当成通过。真出问题时这条日志
        # 能留下页面上的实际措辞，方便往关键词表里补。
        if status is CheckStatus.PASS and not any(k in body for k in _PASS_KEYS):
            pid = id(page)
            if pid not in _logged_pages:
                _logged_pages.add(pid)
                logger.info("内容检查无明确结论，按可发布处理；页面提示：%s", _snippet(body) or "(无)")
        return status

    def click_replace_modal_aware(self):
        """先找页面右上方的「替换」；详情弹窗开着时点弹窗里的「替换视频」。"""
        try:
            page = self.ensure_page()
        except Exception:
            return original_replace(self)

        modal = _visible_modal(page)
        if modal is not None:
            for sel in _MODAL_REPLACE_SEL:
                try:
                    btn = modal.locator(sel)
                    if btn.count() and btn.first.is_visible():
                        _click_guarding_file_chooser(
                            page, lambda b=btn: b.first.click(timeout=5000)
                        )
                        logger.info("已点击弹窗里的「替换视频」")
                        return True
                except Exception:
                    continue
            # 弹窗里没有替换入口就先关掉，否则会挡住页面上的替换按钮
            for sel in _MODAL_CLOSE_SEL:
                try:
                    btn = modal.locator(sel)
                    if btn.count() and btn.first.is_visible():
                        btn.first.click(timeout=3000)
                        logger.info("详情弹窗没有替换入口，已关闭弹窗")
                        break
                except Exception:
                    continue
            else:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass

        # 页面右上方那个「替换」：按位置找，不依赖固定选择器
        try:
            found = page.evaluate(_FIND_REPLACE_JS)
        except Exception as e:
            found = None
            logger.debug("定位替换按钮失败: %s", e)
        if found:
            try:
                loc = page.locator('[data-ttp-replace="1"]').first
                _click_guarding_file_chooser(page, lambda: loc.click(timeout=5000))
                logger.info(
                    "已点击页面右上方的「%s」(x=%s y=%s)",
                    found.get("text") or "替换",
                    found.get("x"),
                    found.get("y"),
                )
                return True
            except Exception as e:
                logger.warning("点击右上方「替换」失败，改用原版选择器: %s", e)

        ok = _click_guarding_file_chooser(page, lambda: original_replace(self))
        logger.info("点击页面「替换」：%s", "成功" if ok else "没找到替换按钮")
        return ok

    # 注意：不要在这里重写 run_with_replace_loop。
    # fix_post_ai_check 里的那份才是主循环，它负责把翻译后的小黄车名注入
    # PublishJob（get_cart_display_for_product），审核不通过时还会
    # _destroy_failed_video（删文件 + 清映射 + 清 Excel + 写删除日志），
    # 外面还套了 fix_incident_log 的异常记录。整个替换掉会把这些一并弄丢。
    # 它调的正是下面这个 click_replace_for_next_video，改这里就够了。
    tiktok_uploader.TikTokStudioUploader._detect_check_status = detect_restricted_first  # type: ignore[method-assign]
    tiktok_uploader.TikTokStudioUploader.click_replace_for_next_video = click_replace_modal_aware  # type: ignore[method-assign]
    logger.info("已应用补丁：内容受限/同质化判为审核不通过 → 删视频 → 点右上方「替换」")
