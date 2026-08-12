"""修复挂车最后一步「添加」点到主页面按钮、把弹窗重新打开的问题。

_click_modal_primary 找按钮时，只有检测到可见弹窗才会限制在弹窗内搜，
否则回退到整个 document.body。挂车最后一步如果弹窗已经自己关掉，
这个回退就会找到主页面那个用来「打开」挂车弹窗的「添加」按钮（宽约 280）
并点下去，把弹窗又开了一次：

    已点击弹窗主按钮「添加」(mouse 473,574 size=280x36)   ← 主页面按钮
    已点「添加」，挂车完成                                 ← 假成功
    已点击「显示更多」×7 ...                               ← 弹窗盖住，全部无效
    未能展开「显示更多」，看不到 AI 生成的内容开关          ← 本轮失败

正常情况下弹窗主按钮是 108x40、位于弹窗右下角。

三道防护：
1. 最后一步「添加」时若确实没有弹窗，就不点（这一步本来就已经完成了），
   绝不去点主页面的「添加」把弹窗重新拉起来。
2. 点完弹窗仍开着，说明没点中主按钮：按几何位置补点底栏按钮。
   这里绝不能按 Esc 关弹窗——那等于取消挂车，商品没挂上却被当成成功，
   结果是视频照发但没有小黄车。补点两次仍失败就直接报错，本轮不发。
3. 展开「显示更多」之前如果发现有弹窗挡着，先收起来，
   避免一个点歪就让整条链路连环失败。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("publisher")

# 与 fix_discard_draft.js_find 里的 roots 判定保持一致，
# 否则两边对「有没有弹窗」的看法不一样，防护就会失效
_MODAL_COUNT_JS = """() => {
  const sel = '[role="dialog"], [class*="TUXModal"], [class*="Modal"], [class*="Dialog"],'
    + ' [class*="drawer"], [class*="Drawer"], [class*="Popup"], [class*="popup"]';
  return [...document.querySelectorAll(sel)].filter(r => {
    const st = getComputedStyle(r);
    const b = r.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && b.width > 80 && b.height > 80;
  }).length;
}"""

# 按几何特征找弹窗底栏主按钮：只在弹窗下半部分找，尺寸像按钮，取最右下角那个。
# 比按文字找可靠——文字匹配会命中主页面上同样叫「添加」的按钮。
_FOOTER_BTN_JS = """() => {
  const sel = '[role="dialog"], [class*="TUXModal"], [class*="Modal"], [class*="Dialog"]';
  const roots = [...document.querySelectorAll(sel)].filter(r => {
    const st = getComputedStyle(r);
    const b = r.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && b.width > 80 && b.height > 80;
  });
  if (!roots.length) return null;
  const root = roots[roots.length - 1];
  const rr = root.getBoundingClientRect();
  const out = [];
  for (const el of root.querySelectorAll('button, [role="button"], div[class*="Button"]')) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (st.pointerEvents === 'none') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 70 || r.width > 240 || r.height < 28 || r.height > 56) continue;
    if (r.top < rr.top + rr.height * 0.5) continue;
    out.push({
      x: r.x + r.width / 2, y: r.y + r.height / 2,
      w: Math.round(r.width), h: Math.round(r.height),
      right: r.right, bottom: r.bottom,
      text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 20),
    });
  }
  if (!out.length) return null;
  out.sort((a, b) => (b.bottom - a.bottom) || (b.right - a.right));
  return out[0];
}"""

_CLOSE_MODAL_JS = """() => {
  const sel = '[role="dialog"], [class*="TUXModal"], [class*="Modal"], [class*="Dialog"]';
  const roots = [...document.querySelectorAll(sel)].filter(r => {
    const st = getComputedStyle(r);
    const b = r.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && b.width > 80 && b.height > 80;
  });
  if (!roots.length) return 'none';
  const root = roots[roots.length - 1];
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const btns = [...root.querySelectorAll('button, [role="button"], [class*="close"], [class*="Close"]')];
  const hit = btns.find(b => {
    const t = norm(b.innerText || b.textContent);
    const al = norm(b.getAttribute('aria-label'));
    return /^(取消|Cancel|关闭|Close|ยกเลิก)$/i.test(t)
      || /^(取消|Cancel|关闭|Close)$/i.test(al)
      || /close/i.test(b.className || '');
  });
  if (hit) { hit.click(); return 'button'; }
  return 'noButton';
}"""

_ADD_WORDS = {"添加", "add", "addtovideo", "add to video", "添加到视频", "เพิ่ม", "เพิ่มลงในวิดีโอ"}
_NEXT_WORDS = {"下一步", "next", "ถัดไป"}


def _norm_labels(labels) -> set[str]:
    out = set()
    for x in labels or ():
        if isinstance(x, str) and x.strip():
            out.add(x.strip().lower())
    return out


def _is_add_step(labels) -> bool:
    """只认挂车最后一步的「添加」，「下一步」不算。"""
    low = _norm_labels(labels)
    if low & _NEXT_WORDS:
        return False
    return bool(low & _ADD_WORDS)


def _modal_count(page) -> int:
    try:
        return int(page.evaluate(_MODAL_COUNT_JS) or 0)
    except Exception:
        return 0


def _wait_modal(page, want_visible: bool, timeout_ms: int) -> bool:
    waited = 0
    step = 200
    while True:
        has = _modal_count(page) > 0
        if has == want_visible:
            return True
        if waited >= timeout_ms:
            return False
        try:
            page.wait_for_timeout(step)
        except Exception:
            return False
        waited += step


def click_modal_footer_button(page) -> bool:
    """补点弹窗底栏的主按钮。

    通用查找是按文字匹配的，主页面上那个用来打开挂车弹窗的「添加」
    文字一模一样，会被误选（宽 280，弹窗真正的主按钮是 108x40）。
    这里改按几何特征找：弹窗下半部分、尺寸像按钮、最靠右下的那个。
    """
    try:
        btn = page.evaluate(_FOOTER_BTN_JS)
    except Exception as e:
        logger.debug("查找弹窗底栏按钮失败: %s", e)
        return False
    if not btn:
        logger.warning("弹窗里没找到底栏主按钮")
        return False
    try:
        page.mouse.click(float(btn["x"]), float(btn["y"]))
        logger.info(
            "补点弹窗底栏主按钮「%s」(mouse %.0f,%.0f size=%sx%s)",
            btn.get("text"),
            float(btn["x"]),
            float(btn["y"]),
            btn.get("w"),
            btn.get("h"),
        )
    except Exception as e:
        logger.warning("补点弹窗底栏按钮失败: %s", e)
        return False
    return _wait_modal(page, False, 4000)


def close_covering_modal(page) -> bool:
    """把挡住页面的弹窗收起来，返回是否已经没有弹窗。"""
    for attempt in range(3):
        if _modal_count(page) == 0:
            return True
        try:
            if attempt == 1:
                page.evaluate(_CLOSE_MODAL_JS)
            else:
                page.keyboard.press("Escape")
        except Exception as e:
            logger.debug("收起弹窗失败: %s", e)
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass
    return _modal_count(page) == 0


def apply_modal_add_click_patches() -> None:
    import tiktok_uploader

    cls = tiktok_uploader.TikTokStudioUploader
    if getattr(cls, "_modal_add_click_patched", False):
        return

    original_primary = cls._click_modal_primary
    original_ai = cls._open_ai_more_if_any

    def click_modal_primary_guarded(self, page, labels, wait_ms: int = 12000):
        if not _is_add_step(labels):
            return original_primary(self, page, labels, wait_ms)

        # 弹窗可能还在渲染，给一点时间；确实没有才判定这一步已经结束
        if not _wait_modal(page, True, 2000):
            logger.warning(
                "挂车最后一步：没有可见弹窗，跳过点「添加」"
                "（此时页面上能匹配到的只有主页面那个「添加」，点了会把弹窗重新打开）"
            )
            return True

        ok = original_primary(self, page, labels, wait_ms)
        if not ok or _wait_modal(page, False, 4000):
            return ok

        # 弹窗还开着 = 刚才那一下没点中真正的主按钮。
        # 这里绝不能按 Esc 关弹窗——那等于取消挂车，商品挂不上还会被当成成功。
        logger.warning("点完「添加」弹窗仍开着，说明没点中主按钮，改按底栏位置补点")
        for attempt in range(2):
            if click_modal_footer_button(page):
                logger.info("补点成功，弹窗已关闭，挂车完成")
                return True
            logger.warning("第 %d 次补点后弹窗仍开着", attempt + 1)
        raise RuntimeError("挂车失败：弹窗「添加」点不掉，商品没挂上，本次不发布")

    def open_ai_more_guarded(self, page):
        if _modal_count(page) > 0:
            logger.warning("展开「显示更多」前发现弹窗挡住页面，先收起")
            close_covering_modal(page)
        return original_ai(self, page)

    cls._click_modal_primary = click_modal_primary_guarded  # type: ignore[method-assign]
    cls._open_ai_more_if_any = open_ai_more_guarded  # type: ignore[method-assign]
    cls._modal_add_click_patched = True  # type: ignore[attr-defined]
    logger.info(
        "已应用补丁：挂车「添加」只在弹窗内点；没点中改按底栏位置补点（不再 Esc 取消挂车）"
    )
