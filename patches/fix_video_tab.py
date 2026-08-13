"""上传前切到「视频」页签：不依赖界面语言，按结果校验。

原逻辑只认中文 `视频` 页签（get_by_role(name="视频") / :has-text("视频")），
泰文、英文界面的 Studio 匹配不到，页面就停在「照片」页签，
于是页面上只有 accept=image/* 的上传框，守卫直接抛
「拒绝使用照片上传框 accept=image/*」。

这里改成：
1. 先看页面上有没有能收视频的 input，有就不用切；
2. 没有就按多语种词表找页签候选（中/英/泰/日/韩/俄/越/印尼…），
   逐个点，每点一次重新检查是否出现了视频 input——按结果判断成功，
   而不是按点没点到判断；
3. 还不行就退回点第一个 role=tab（视频页签通常排第一）；
4. 仍然没有才报错，并把页面上实际存在的 accept 一并打出来。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("fix_video_tab")

_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv", ".flv", ".3gp", ".m4v", ".mpeg")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp")

# 页签文字：小写后比对，允许前缀匹配（Video / Videos / 视频上传）
_VIDEO_WORDS = (
    "视频", "影片", "上传视频",
    "video", "videos", "upload video",
    "วิดีโอ", "อัปโหลดวิดีโอ",
    "動画", "동영상",
    "видео",
    "vídeo", "vidéo",
)
_PHOTO_WORDS = (
    "照片", "图片", "圖片", "图文",
    "photo", "photos", "image", "images", "picture", "pictures",
    "รูปภาพ", "ภาพถ่าย", "รูป",
    "写真", "画像", "사진",
    "фото", "изображени",
    "foto", "imagen",
)


def _accept_takes_video(accept: str) -> bool:
    a = (accept or "").lower()
    if "video" in a:
        return True
    return any(e in a for e in _VIDEO_EXTS)


def _accept_image_only(accept: str) -> bool:
    a = (accept or "").lower().strip()
    if not a or _accept_takes_video(a):
        return False
    return "image" in a or any(e in a for e in _IMAGE_EXTS)


def _list_accepts(page) -> list[str]:
    try:
        return list(
            page.evaluate(
                "() => Array.from(document.querySelectorAll('input[type=file]'))"
                ".map(e => e.getAttribute('accept') || '')"
            )
            or []
        )
    except Exception:
        return []


def pick_video_file_input_fixed(page):
    """只返回能收视频的 input；只有照片框时返回 None，让上层去切页签。"""
    inputs = page.locator('input[type="file"]')
    try:
        count = inputs.count()
    except Exception:
        return None, 0, "none"
    if count == 0:
        return None, 0, "none"

    fallback = None
    for i in range(count):
        inp = inputs.nth(i)
        try:
            accept = (inp.get_attribute("accept") or "").lower()
        except Exception:
            accept = ""
        if _accept_takes_video(accept):
            return inp, i, accept or "video"
        if fallback is None and not _accept_image_only(accept):
            fallback = (inp, i, accept or "any")

    if fallback is not None:
        return fallback
    return None, 0, "image-only"


_MARK_TABS_JS = """
(words) => {
  const [videoWords, photoWords] = words;
  document.querySelectorAll('[data-ttpvtab]').forEach(e => e.removeAttribute('data-ttpvtab'));
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const hits = [];
  const nodes = document.querySelectorAll(
    '[role="tab"],[role="radio"],button,label,li,a,div,span'
  );
  for (const el of nodes) {
    const t = norm(el.innerText || el.textContent);
    if (!t || t.length > 16) continue;
    if (photoWords.some(w => t.includes(w))) continue;
    if (!videoWords.some(w => t === w || t.startsWith(w))) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 14 || r.width > 460 || r.height > 140) continue;
    if (r.bottom <= 0 || r.top > window.innerHeight * 0.75) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) continue;
    hits.push(el);
  }
  // 只留最内层：父节点文字相同会重复命中
  const inner = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
  inner.forEach((el, i) => el.setAttribute('data-ttpvtab', String(i)));
  return inner.length;
}
"""


def _click_marked(page, index: int) -> bool:
    sel = f'[data-ttpvtab="{index}"]'
    try:
        page.locator(sel).first.click(timeout=2000)
        return True
    except Exception:
        pass
    try:
        return bool(
            page.evaluate(
                "(s) => { const e = document.querySelector(s);"
                " if (!e) return false; e.click(); return true; }",
                sel,
            )
        )
    except Exception:
        return False


def switch_to_video_tab(page) -> bool:
    """确保页面处在能上传视频的页签。返回是否已就绪。"""
    target, _idx, _accept = pick_video_file_input_fixed(page)
    if target is not None:
        return True

    try:
        found = int(page.evaluate(_MARK_TABS_JS, [list(_VIDEO_WORDS), list(_PHOTO_WORDS)]) or 0)
    except Exception as e:
        logger.debug("扫描视频页签失败: %s", e)
        found = 0

    for i in range(found):
        if not _click_marked(page, i):
            continue
        try:
            page.wait_for_timeout(600)
        except Exception:
            pass
        target, _idx, _accept = pick_video_file_input_fixed(page)
        if target is not None:
            logger.info("已切到视频页签（候选 %s/%s）", i + 1, found)
            return True

    # 兜底：视频页签一般是第一个 tab
    try:
        tabs = page.locator('[role="tab"]')
        if tabs.count():
            tabs.first.click(timeout=2000)
            page.wait_for_timeout(600)
            if pick_video_file_input_fixed(page)[0] is not None:
                logger.info("已切到视频页签（退回点第一个 tab）")
                return True
    except Exception as e:
        logger.debug("退回点第一个 tab 失败: %s", e)

    return False


def upload_video_via_video_input_fixed(self, page, video_path) -> None:
    from pathlib import Path

    import fix_discard_draft as base

    video_path = Path(video_path)
    if not video_path.exists():
        raise RuntimeError(f"视频不存在: {video_path}")

    page = base.ensure_upload_stage(self, page)

    if not switch_to_video_tab(page):
        # 可能是 input 还没渲染：点一下上传区再看
        for text in ("选择视频", "Select video", "เลือกวิดีโอ", "选择要上传的视频", "Upload"):
            try:
                page.get_by_text(text, exact=False).first.click(timeout=1500)
                page.wait_for_timeout(600)
            except Exception:
                continue
            if switch_to_video_tab(page):
                break

    target, idx, accept = pick_video_file_input_fixed(page)
    if target is None:
        accepts = _list_accepts(page)
        try:
            self._screenshot(page, "no_video_input")
        except Exception:
            pass
        raise RuntimeError(
            "页面上找不到视频上传框，可能停在「照片」页签且未能切回。"
            f" 当前 input[type=file] 的 accept={accepts or '（一个都没有）'}"
        )

    logger.info("使用文件框 input[%s] accept=%s 上传: %s", idx, accept, video_path.name)
    target.set_input_files(str(video_path))
    logger.info("已选择视频文件: %s", video_path.name)
    self.page = page


def apply_video_tab_patches() -> None:
    import fix_discard_draft as base

    base._pick_video_file_input = pick_video_file_input_fixed  # type: ignore[attr-defined]
    base._upload_video_via_video_input = upload_video_via_video_input_fixed  # type: ignore[attr-defined]
    logger.info("已应用补丁：上传前按结果切「视频」页签（不依赖界面语言）")
