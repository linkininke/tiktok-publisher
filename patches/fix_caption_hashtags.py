"""话题标签：在原版填写/发布流程上轻量守卫，不重写上传自动化。

打开界面、选视频、填文案、挂车仍走原版 publish / _fill_caption。
仅在 AI/检测/点发布前，发现话题丢失时再补一次原版填写。
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


def apply_caption_hashtag_patches() -> None:
    import tiktok_uploader

    cls = tiktok_uploader.TikTokStudioUploader
    original_fill = cls._fill_caption
    original_publish = cls.publish

    def fill_caption_fixed(self, page, caption: str):
        # 恢复原版填写（昨天成功路径：已填写描述/标题（已覆盖旧草稿））
        return original_fill(self, page, caption)

    def ensure_with_original(self, page, caption: str) -> None:
        if page is None or not (caption or "").strip():
            return
        title, tags = _split_title_tags(caption)
        try:
            missing = _caption_missing_tags(page, tags) if tags else []
            actual = _read_caption_text(page)
        except Exception:
            return
        need = bool(tags and missing) or (
            bool(title) and title[:12] not in actual.replace("\n", " ")
        )
        if not need and actual.strip():
            return
        logger.warning("发布前发现文案/话题缺失 missing=%s，用原版填写补一次", missing)
        try:
            original_fill(self, page, caption)
        except Exception as e:
            logger.warning("补写文案失败: %s", e)

    def publish_fixed(self, job):
        """原版 publish；仅在本次调用期间挂钩子，结束后还原。"""
        caption = getattr(job, "caption", "") or ""
        cur_ai = cls._open_ai_more_if_any
        cur_wait = cls._wait_content_check
        cur_post = cls._click_post

        def open_ai_guard(page_self, page):
            cur_ai(page_self, page)
            try:
                ensure_with_original(page_self, page, caption)
            except Exception as e:
                logger.debug("AI后话题守卫跳过: %s", e)

        def wait_check_guard(page_self, page):
            try:
                ensure_with_original(page_self, page, caption)
            except Exception:
                pass
            return cur_wait(page_self, page)

        def click_post_guard(page_self, page):
            try:
                ensure_with_original(page_self, page, caption)
            except Exception:
                pass
            return cur_post(page_self, page)

        cls._open_ai_more_if_any = open_ai_guard  # type: ignore[method-assign]
        cls._wait_content_check = wait_check_guard  # type: ignore[method-assign]
        cls._click_post = click_post_guard  # type: ignore[method-assign]
        try:
            return original_publish(self, job)
        finally:
            cls._open_ai_more_if_any = cur_ai  # type: ignore[method-assign]
            cls._wait_content_check = cur_wait  # type: ignore[method-assign]
            cls._click_post = cur_post  # type: ignore[method-assign]

    cls._fill_caption = fill_caption_fixed  # type: ignore[method-assign]
    cls.publish = publish_fixed  # type: ignore[method-assign]
    logger.info("已应用补丁：恢复原版上传/填文案流程；发布前仅轻量守卫话题")
