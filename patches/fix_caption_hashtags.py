"""话题标签：在原版填写/发布流程上轻量守卫，不重写上传自动化。

打开界面、选视频、填文案、挂车仍走原版 publish / _fill_caption。

并发关键问题：
1) 禁止在 publish 期间改写类方法（会串号冲掉另一窗口文案）
2) 挂车 / 显示更多 / AI 开关后，Studio 常清空描述框；
   点「发布」前若只做「缺失才补」，并发时容易漏补 → 发布出去没有标题/话题。
   因此：挂车完成后、点发布前，强制用本账号文案再覆盖一次。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("publisher")

_TAG_RE = re.compile(r"#([^\s#]+)")


def _split_title_tags(caption: str) -> tuple[str, list[str]]:
    text = (caption or "").replace("\r\n", "\n").strip()
    if not text:
        return "", []
    tags: list[str] = []
    for m in _TAG_RE.finditer(text):
        raw = m.group(0).strip()
        if raw and raw not in tags:
            tags.append(raw if raw.startswith("#") else f"#{raw}")
    lines: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        tokens = s.split()
        if tokens and all(t.startswith("#") for t in tokens):
            continue
        cleaned = _TAG_RE.sub("", s)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;|")
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip(), tags


def _read_caption_text(page) -> str:
    try:
        return (
            page.evaluate(
                """() => {
                  const el = document.querySelector('[data-e2e="caption_container"] [contenteditable="true"]')
                    || [...document.querySelectorAll('[contenteditable="true"]')]
                         .find(e => e.offsetParent !== null);
                  return el ? (el.innerText || '').trim() : '';
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def _caption_missing_tags(page, tags: list[str]) -> list[str]:
    actual = _read_caption_text(page).lower()
    missing = []
    for t in tags:
        key = t.lstrip("#").lower()
        if not key:
            continue
        if key not in actual and f"#{key}" not in actual:
            missing.append(t)
    return missing


_CAPTION_SELECTORS = (
    '[data-e2e="caption_container"] [contenteditable="true"]',
    '[contenteditable="true"]',
)


def _caption_locator(page):
    for sel in _CAPTION_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                return loc
        except Exception:
            continue
    return None


def _type_caption_real(page, text: str) -> bool:
    """用真实键盘输入写描述。

    原版走 `el.innerText = text` + 合成 InputEvent 直接改 DOM，
    但 Studio 的描述框是富文本编辑器，自己维护一份数据模型，
    不认这种非可信事件。结果就是框里看得见字、提交上去却是空的，
    发出来显示「暂无描述」。

    这里改用 CDP 的 insertText：整段一次性插入，走浏览器真实输入管线，
    编辑器模型会同步更新；同时避免逐字键入把泰文的组合字符打散。
    """
    loc = _caption_locator(page)
    if loc is None:
        logger.warning("没找到描述框，无法真实键入")
        return False
    if not hasattr(page.keyboard, "insert_text"):
        logger.warning("当前 Playwright 不支持 insert_text")
        return False
    try:
        loc.click(timeout=3000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(120)
        # 按行插入：换行用 Enter。话题行放最后，Enter 都发生在它之前，
        # 不会被话题联想面板抢走当成「选中某个话题」。
        for i, line in enumerate(text.split("\n")):
            if i:
                page.keyboard.press("Enter")
            if line:
                page.keyboard.insert_text(line)
                page.wait_for_timeout(60)
        page.wait_for_timeout(250)
        # 收尾把话题联想面板关掉，否则它会挡住后面的点击
        page.keyboard.press("Escape")
        page.wait_for_timeout(120)
    except Exception as e:
        logger.warning("真实键入描述异常: %s", e)
        return False

    actual = _read_caption_text(page).replace("\n", " ")
    head = text.replace("\n", " ").strip()[:12]
    if head and head in actual:
        return True
    logger.warning(
        "真实键入后校验不通过 期望前12=%s 实际前60=%s", head, actual[:60]
    )
    return False


def _active_caption(uploader) -> str:
    return str(getattr(uploader, "_publish_caption", "") or "")


def _dump_caption_dom(page) -> str:
    """把描述框的 DOM 结构打出来。

    话题到底有没有被编辑器识别，光看 innerText 是看不出来的（纯文本和实体
    读出来一模一样）。只有看 innerHTML 才能确定 Studio 是怎么渲染 #xxx 的。
    """
    try:
        return (
            page.evaluate(
                """() => {
                  const el = document.querySelector('[data-e2e="caption_container"] [contenteditable="true"]')
                    || [...document.querySelectorAll('[contenteditable="true"]')]
                         .find(e => e.offsetParent !== null);
                  return el ? (el.innerHTML || '') : '(未找到描述框)';
                }"""
            )
            or ""
        )
    except Exception as e:
        return f"(读取失败: {e})"


def apply_caption_hashtag_patches() -> None:
    import tiktok_uploader

    cls = tiktok_uploader.TikTokStudioUploader
    # v4 逐字 type() 会打散泰文；v5 回退 JS 写入但发出去是「暂无描述」；
    # v6 改用 CDP insertText 整段插入，编辑器模型能收到
    already = getattr(cls, "_caption_hashtag_patched_v6", False)
    if already:
        return

    original_fill = cls._fill_caption
    # 若已打过 v2，publish/ai/wait/post 可能已是守卫；再包一层会重复。
    # 以「当前类上的方法」为底座，保证挂到最新链路上。
    original_publish = cls.publish
    original_ai = cls._open_ai_more_if_any
    original_wait = cls._wait_content_check
    original_post = cls._click_post
    original_attach = cls._attach_product

    def fill_caption_fixed(self, page, caption: str):
        """优先真实键入，失败才回退原版 JS 写入。

        原版 JS 写入会让描述在页面上看着是好的、发出去却是「暂无描述」，
        所以它只能当兜底：真实键入至少有机会成功，回退了也不比现在更差。
        逐字 type() 不用，实测会把泰文的组合字符打散。
        """
        text = (caption or "").strip()
        if not text:
            raise RuntimeError("文案为空，拒绝填写")
        try:
            self._discard_unsaved_draft(page)
        except Exception as e:
            logger.debug("键入前清草稿跳过: %s", e)

        if _type_caption_real(page, text):
            logger.info(
                "已填写描述/标题（真实键入，编辑器已收到）: %s",
                text.replace("\n", " ")[:60],
            )
            return None

        logger.warning("真实键入失败，回退原版 JS 写入（有发出去没描述的风险）")
        return original_fill(self, page, caption)

    def ensure_with_original(
        self, page, caption: str, *, force: bool = False, where: str = ""
    ) -> None:
        if page is None or not (caption or "").strip():
            if force:
                logger.warning(
                    "发布前强制回填跳过：本账号文案为空 where=%s", where or "-"
                )
            return
        title, tags = _split_title_tags(caption)
        try:
            missing = _caption_missing_tags(page, tags) if tags else []
            actual = _read_caption_text(page)
        except Exception as e:
            if not force:
                return
            missing = list(tags)
            actual = ""
            logger.warning("读取描述失败，将强制回填 where=%s: %s", where or "-", e)

        flat = actual.replace("\n", " ")
        title_ok = (not title) or (title[:12] in flat)
        tags_ok = not (tags and missing)
        # force 的本意是「并发时描述被清空要能补回来」，不是每次都重写一遍。
        # 内容已经完整还重写，只会反复清空重打、把文案越弄越残。
        if title_ok and tags_ok and actual.strip():
            logger.info(
                "描述完整，跳过回填 where=%s force=%s actual前60=%s",
                where or "-",
                force,
                flat[:60],
            )
            return
        need = force or not tags_ok or not title_ok
        if not need and actual.strip():
            logger.info(
                "描述仍在，跳过回填 where=%s actual前60=%s",
                where or "-",
                flat[:60],
            )
            return

        logger.warning(
            "回填描述/话题 where=%s force=%s missing=%s actual前60=%s",
            where or "-",
            force,
            missing,
            (actual or "").replace("\n", " ")[:60],
        )
        try:
            fill_caption_fixed(self, page, caption)
            after = _read_caption_text(page)
            logger.info(
                "回填后描述前80=%s",
                (after or "").replace("\n", " ")[:80],
            )
        except Exception as e:
            logger.warning("补写文案失败 where=%s: %s", where or "-", e)

    def open_ai_guard(self, page):
        original_ai(self, page)
        try:
            # AI/显示更多后常见丢文案：缺则补；不强制以免打断开关状态
            ensure_with_original(
                self, page, _active_caption(self), force=False, where="ai后"
            )
        except Exception as e:
            logger.debug("AI后话题守卫跳过: %s", e)

    def wait_check_guard(self, page):
        try:
            ensure_with_original(
                self, page, _active_caption(self), force=False, where="内容检测前"
            )
        except Exception:
            pass
        return original_wait(self, page)

    def click_post_guard(self, page):
        # 关键：点发布前强制覆盖一次，避免挂车后空描述被发出去
        try:
            ensure_with_original(
                self, page, _active_caption(self), force=True, where="点发布前"
            )
        except Exception as e:
            logger.warning("点发布前强制回填异常: %s", e)
        try:
            logger.info(
                "即将点发布，当前描述前80=%s",
                _read_caption_text(page).replace("\n", " ")[:80],
            )
            logger.info("点发布前描述框DOM=%s", _dump_caption_dom(page)[:600])
        except Exception:
            pass
        return original_post(self, page)

    def attach_product_fixed(self, page, product_id, product_display_name=None):
        out = original_attach(self, page, product_id, product_display_name)
        # 挂车弹窗关闭后 Studio 经常清空描述：立刻强制写回
        try:
            ensure_with_original(
                self,
                getattr(self, "page", None) or page,
                _active_caption(self),
                force=True,
                where="挂车后",
            )
        except Exception as e:
            logger.warning("挂车后回填文案失败: %s", e)
        return out

    def publish_fixed(self, job):
        """文案挂在实例上，供 AI/挂车/点发布守卫读取；不改类方法。"""
        prev = getattr(self, "_publish_caption", None)
        self._publish_caption = getattr(job, "caption", "") or ""
        try:
            return original_publish(self, job)
        finally:
            if prev is None:
                try:
                    delattr(self, "_publish_caption")
                except Exception:
                    self._publish_caption = ""
            else:
                self._publish_caption = prev

    cls._fill_caption = fill_caption_fixed  # type: ignore[method-assign]
    cls._open_ai_more_if_any = open_ai_guard  # type: ignore[method-assign]
    cls._wait_content_check = wait_check_guard  # type: ignore[method-assign]
    cls._click_post = click_post_guard  # type: ignore[method-assign]
    cls._attach_product = attach_product_fixed  # type: ignore[method-assign]
    cls.publish = publish_fixed  # type: ignore[method-assign]
    cls._caption_hashtag_patched_v2 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v3 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v4 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v5 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v6 = True  # type: ignore[attr-defined]
    logger.info(
        "已应用补丁：描述改为真实键入（修「暂无描述」）+ 完整就不重写 + 点发布前打印DOM"
    )
