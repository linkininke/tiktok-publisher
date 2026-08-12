"""挂车后流程补丁：显示更多 → AI 开关 → 内容检测 → 失败销毁视频再试下一个。

对照昨晚成功路径（mouse 448,615 / 已勾选 AI）+ 原版 _click_text_button：
- 「显示更多」必须真展开（看到「收起」或 AI 文案），禁止假点击就继续
- AI 开关点可见 Switch 轨道，避开 aria-hidden 的 Switch__input
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("publisher")

_AI_KEYWORDS = (
    "AI 生成的内容",
    "AI生成的内容",
    "AI-generated content",
    "AI generated content",
    "AI-generated",
    "AI generated",
    "เนื้อหาที่สร้างโดย AI",
    "เนื้อหาที่สร้างด้วย AI",
)

_SHOW_MORE_LABELS = (
    "显示更多",
    "Show more",
    "แสดงเพิ่มเติม",
)

_COLLAPSE_LABELS = (
    "收起",
    "Show less",
    "แสดงน้อยลง",
)


def _destroy_failed_video(
    product_id: str,
    video: Path,
    *,
    reason: str = "",
    status: str = "fail",
) -> dict:
    """内容检测失败：删文件 + 映射去掉该视频 + Excel 去掉对应文本 + 写删除日志。"""
    import json
    import time
    from datetime import datetime

    video = Path(video)
    info: dict = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "product_id": str(product_id or ""),
        "video": video.name,
        "reason": (reason or "内容审核不通过").strip() or "内容审核不通过",
        "status": status or "fail",
        "stage": "content_check_replace",
        "deleted_file": False,
        "mapping_updated": False,
        "excel_updated": False,
    }

    root = Path(__file__).resolve().parent.parent
    cfg = {}
    map_file = root / "data" / "product_video_map.json"
    videos_dir = video.parent if video.parent.exists() else root
    excel = None
    try:
        import config_loader

        root = Path(getattr(config_loader, "ROOT", None) or root)
        cfg = config_loader.load_config(root / "config.yaml")
        map_file = root / cfg["paths"]["mapping_file"]
        videos_dir = Path(cfg["paths"]["videos_dir"])
        if not videos_dir.is_absolute():
            videos_dir = root / videos_dir
        excel_path = Path(cfg["paths"].get("excel_file") or "")
        if excel_path and not excel_path.is_absolute():
            excel_path = root / excel_path
        excel = excel_path if excel_path and str(excel_path) != "." else None
    except Exception as e:
        logger.debug("读取配置失败，使用默认路径: %s", e)

    # 1) 映射 + 删文件（复用原版成功消费逻辑）
    try:
        from mapping_store import consume_video_after_success

        consumed = consume_video_after_success(
            mapping_file=map_file,
            videos_dir=videos_dir,
            product_id=str(product_id),
            video_name=video.name,
            mapping_in_memory=None,
        ) or {}
        info["deleted_file"] = bool(consumed.get("deleted_file"))
        info["mapping_updated"] = True
        info["consume"] = {
            k: consumed.get(k)
            for k in ("deleted_file", "removed_from_mapping", "remaining")
            if k in consumed
        }
        logger.info(
            "内容检测失败，已销毁视频 product=%s video=%s deleted=%s",
            product_id,
            video.name,
            info["deleted_file"],
        )
    except Exception as e:
        logger.warning("映射销毁失败，尝试直接删文件: %s", e)
        try:
            target = video if video.is_file() else (videos_dir / video.name)
            if target.is_file():
                target.unlink()
                info["deleted_file"] = True
                logger.info("已直接删除失败视频文件: %s", target)
        except Exception as e2:
            logger.warning("删除失败视频文件失败: %s", e2)
        # 兜底：手工从映射 JSON 去掉该视频名
        try:
            info["mapping_updated"] = _remove_video_from_mapping_file(
                map_file, str(product_id), video.name
            )
        except Exception as e3:
            logger.warning("手工更新映射失败: %s", e3)

    # 2) Excel 去掉对应视频文本
    if excel is not None:
        try:
            from sync_excel import remove_video_from_excel, rewrite_mapping_summary

            excel_info = remove_video_from_excel(
                excel, str(product_id), video.name, videos_dir=videos_dir
            )
            info["excel_updated"] = bool(excel_info.get("ok") and excel_info.get("removed"))
            info["excel"] = excel_info
            try:
                rewrite_mapping_summary(excel, map_file)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Excel 移除失败视频文本失败: %s", e)
            info["excel_error"] = str(e).split("\n", 1)[0][:200]
    else:
        info["excel_error"] = "excel_path_missing"

    # 3) 删除日志（jsonl + 可读 txt）
    try:
        _append_deleted_video_log(root, info)
    except Exception as e:
        logger.warning("写入删除视频日志失败: %s", e)

    return info


def _remove_video_from_mapping_file(map_file: Path, product_id: str, video_name: str) -> bool:
    import json

    map_file = Path(map_file)
    if not map_file.exists():
        return False
    data = json.loads(map_file.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return False
    key = Path(video_name).name.lower()
    if not key.endswith(".mp4"):
        key = key + ".mp4"
    changed = False
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("product_id") or "") != str(product_id):
            continue
        vids = list(item.get("videos") or [])
        kept = [v for v in vids if Path(str(v)).name.lower() != key]
        if len(kept) != len(vids):
            item["videos"] = kept
            item["enabled"] = bool(kept)
            changed = True
        break
    if changed:
        map_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def _append_deleted_video_log(root: Path, info: dict) -> None:
    import json

    log_dir = root / "logs"
    try:
        import config_loader

        cfg = config_loader.load_config(root / "config.yaml")
        log_dir = root / cfg["paths"].get("log_dir", "logs")
    except Exception:
        pass
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl = log_dir / "deleted_videos.jsonl"
    txt = log_dir / "deleted_videos.txt"
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(info, ensure_ascii=False) + "\n")
    line = (
        f"[{info.get('ts')}] 商品ID={info.get('product_id')} "
        f"视频={info.get('video')} 原因={info.get('reason')} "
        f"删文件={info.get('deleted_file')} 映射={info.get('mapping_updated')} "
        f"Excel={info.get('excel_updated')}"
    )
    with txt.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    logger.info("已记录删除视频日志: %s", line)


def _text_visible(page, labels: tuple[str, ...], timeout: int = 600) -> bool:
    for label in labels:
        try:
            if page.get_by_text(label, exact=False).first.is_visible(timeout=timeout):
                return True
        except Exception:
            continue
    return False


def _settings_expanded(page) -> bool:
    """真正展开的判据：出现「收起」或 AI 开关文案可见。"""
    if _text_visible(page, _COLLAPSE_LABELS, timeout=500):
        return True
    if _text_visible(page, _AI_KEYWORDS, timeout=500):
        return True
    try:
        return bool(
            page.evaluate(
                """(keywords) => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const want = (keywords || []).map(norm);
                  for (const el of document.querySelectorAll('div,span,label,p,li,section')) {
                    const t = norm(el.innerText || el.textContent);
                    if (!t || t.length > 64) continue;
                    if (!want.some(w => t === w || t.includes(w))) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                  }
                  return false;
                }""",
                list(_AI_KEYWORDS),
            )
        )
    except Exception:
        return False


def _click_show_more_dom(page) -> bool:
    """DOM 精确点「显示更多」（与原版 _click_text_button 的 JS 兜底一致：exact + 有尺寸）。"""
    try:
        return bool(
            page.evaluate(
                """(labels) => {
                  const nodes = [...document.querySelectorAll('button, [role="button"], div, span, a')];
                  for (const label of labels) {
                    const hit = nodes.reverse().find(e => {
                      const t = (e.innerText || e.textContent || '').trim();
                      if (t !== label) return false;
                      const r = e.getBoundingClientRect();
                      return r.width > 0 && r.height > 0;
                    });
                    if (hit) {
                      try { hit.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
                      hit.click();
                      return true;
                    }
                  }
                  return false;
                }""",
                list(_SHOW_MORE_LABELS),
            )
        )
    except Exception as e:
        logger.debug("DOM 点显示更多失败: %s", e)
        return False


def _ensure_show_more(self, page) -> None:
    """对齐原版：先滚设置区 → _click_text_button → 必须验证已展开。"""
    # 先向下滚，把设置区滚进视口（原版 wheel 10 次）
    try:
        for _ in range(10):
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(180)
    except Exception:
        pass

    if _settings_expanded(page):
        logger.info("设置区已展开（看到「收起」或 AI 文案）")
        return

    clicked = False
    try:
        clicked = bool(self._click_text_button(page, _SHOW_MORE_LABELS))
    except Exception as e:
        logger.debug("_click_text_button 显示更多失败: %s", e)

    if not clicked:
        clicked = _click_show_more_dom(page)

    if clicked:
        logger.info("已点击「显示更多」")
        page.wait_for_timeout(1200)
        if _settings_expanded(page):
            logger.info("设置区已确认展开")
            return
        logger.warning("点了「显示更多」但未展开，继续重试")
    else:
        logger.info("首次未点到「显示更多」，准备滚动重试")

    # 原版二次：向上滚一点再点
    for round_i in range(1, 6):
        try:
            page.mouse.wheel(0, -500)
            page.wait_for_timeout(200)
        except Exception:
            pass
        clicked = False
        try:
            clicked = bool(self._click_text_button(page, _SHOW_MORE_LABELS))
        except Exception:
            pass
        if not clicked:
            clicked = _click_show_more_dom(page)
        if clicked:
            logger.info("已点击「显示更多」(二次#%d)", round_i)
            page.wait_for_timeout(1200)
            if _settings_expanded(page):
                logger.info("设置区已确认展开")
                return
        try:
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(200)
        except Exception:
            pass

    # Playwright exact 再试
    for label in _SHOW_MORE_LABELS:
        try:
            loc = page.get_by_text(label, exact=True)
            n = loc.count()
            for i in range(n - 1, -1, -1):
                item = loc.nth(i)
                if item.is_visible(timeout=500):
                    item.scroll_into_view_if_needed(timeout=1500)
                    item.click(timeout=3000, force=True)
                    logger.info("已点击「%s」(Playwright exact)", label)
                    page.wait_for_timeout(1200)
                    if _settings_expanded(page):
                        logger.info("设置区已确认展开")
                        return
        except Exception:
            continue

    if _settings_expanded(page):
        logger.info("设置区已展开")
        return
    logger.warning("未确认展开「显示更多」，仍尝试直接找 AI 开关")


def _ai_switch_is_on(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """(keywords) => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const want = (keywords || []).map(norm);
                  for (const el of document.querySelectorAll('div,span,label,p,li,section')) {
                    const t = norm(el.innerText || el.textContent);
                    if (!t || t.length > 64) continue;
                    if (!want.some(w => t === w || t.includes(w))) continue;
                    let row = el;
                    for (let i = 0; i < 10 && row; i++) {
                      const inp = row.querySelector(
                        'input.Switch__input, input[type="checkbox"][role="switch"], input[type="checkbox"], [role="switch"]'
                      );
                      if (inp) {
                        if (inp.tagName === 'INPUT') return !!inp.checked;
                        const a = inp.getAttribute('aria-checked');
                        if (a != null) return a === 'true';
                        const nested = inp.querySelector('input[type="checkbox"]');
                        if (nested) return !!nested.checked;
                      }
                      row = row.parentElement;
                    }
                  }
                  return false;
                }""",
                list(_AI_KEYWORDS),
            )
        )
    except Exception:
        return False


def _click_ai_switch_once(page) -> bool:
    """昨晚成功逻辑：点可见 Switch 容器中心；坐标无效则走 DOM，不点 (0,0)。"""
    try:
        info = page.evaluate(
            """(keywords) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const want = (keywords || []).map(norm);
              for (const el of document.querySelectorAll('div,span,label,p,li,section')) {
                const t = norm(el.innerText || el.textContent);
                if (!t || t.length > 64) continue;
                if (!want.some(w => t === w || t.includes(w))) continue;
                let row = el;
                for (let i = 0; i < 10 && row; i++) {
                  const input = row.querySelector('input.Switch__input, input[role="switch"], input[type="checkbox"]');
                  if (!input) { row = row.parentElement; continue; }
                  // 优先可见轨道，再 Switch 容器（昨晚成功 size≈28x16）
                  let target =
                    row.querySelector('[class*="Switch__track"]')
                    || row.querySelector('[class*="Switch-track"]')
                    || input.closest('[class*="Switch"]')
                    || input.parentElement
                    || input;
                  if (target === input || (target && target.tagName === 'INPUT')) {
                    target = input.parentElement || target;
                  }
                  try { target.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
                  const box2 = target.getBoundingClientRect();
                  if (box2.width < 8 || box2.height < 8) {
                    row = row.parentElement;
                    continue;
                  }
                  return {
                    x: box2.x + box2.width / 2,
                    y: box2.y + box2.height / 2,
                    w: box2.width,
                    h: box2.height,
                    checked: !!(input.checked),
                  };
                }
              }
              return null;
            }""",
            list(_AI_KEYWORDS),
        )
    except Exception as e:
        logger.debug("定位 AI 开关失败: %s", e)
        info = None

    if info:
        x, y = float(info.get("x") or 0), float(info.get("y") or 0)
        w, h = float(info.get("w") or 0), float(info.get("h") or 0)
        if x > 1 and y > 1 and w >= 8 and h >= 8:
            try:
                page.mouse.move(x, y)
                page.wait_for_timeout(60)
                page.mouse.down()
                page.wait_for_timeout(40)
                page.mouse.up()
                logger.info(
                    "已点击 AI 开关轨道 (mouse %.0f,%.0f size=%.0fx%.0f)",
                    x,
                    y,
                    w,
                    h,
                )
                return True
            except Exception as e:
                logger.warning("AI 开关 mouse 点击失败: %s", e)
        else:
            logger.info("AI 开关坐标无效 (%.0f,%.0f size=%.0fx%.0f)，改 DOM", x, y, w, h)

    # DOM 兜底（昨晚成功路径的第二段）
    try:
        ok = page.evaluate(
            """(keywords) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const want = (keywords || []).map(norm);
              for (const el of document.querySelectorAll('div,span,label,p,li,section')) {
                const t = norm(el.innerText || el.textContent);
                if (!t || t.length > 64) continue;
                if (!want.some(w => t === w || t.includes(w))) continue;
                let row = el;
                for (let i = 0; i < 10 && row; i++) {
                  const input = row.querySelector('input.Switch__input, input[role="switch"], input[type="checkbox"]');
                  if (!input) { row = row.parentElement; continue; }
                  const target =
                    row.querySelector('[class*="Switch__track"]')
                    || input.closest('[class*="Switch"]')
                    || input.parentElement
                    || input;
                  try { target.scrollIntoView({block:'center'}); } catch (e) {}
                  if (target && target !== input) target.click();
                  else el.click();
                  if (!input.checked) {
                    input.checked = true;
                    input.dispatchEvent(new Event('input', {bubbles:true}));
                    input.dispatchEvent(new Event('change', {bubbles:true}));
                    input.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                  }
                  return true;
                }
              }
              return false;
            }""",
            list(_AI_KEYWORDS),
        )
        if ok:
            logger.info("已点击 AI 开关 (DOM/事件兜底)")
        return bool(ok)
    except Exception as e:
        logger.warning("AI 开关 DOM 点击失败: %s", e)
        return False


def open_ai_more_fixed(self, page) -> None:
    """显示更多（必须真展开）→ 打开「AI 生成的内容」。"""
    try:
        self._dismiss_blocking_modals(page)
    except Exception:
        pass

    try:
        _ensure_show_more(self, page)
        page.wait_for_timeout(500)
    except Exception as e:
        logger.warning("展开「显示更多」异常: %s", e)

    if not _settings_expanded(page):
        # 再强制一次原版按钮点击
        try:
            if self._click_text_button(page, _SHOW_MORE_LABELS):
                logger.info("已点击「显示更多」(末次原版)")
                page.wait_for_timeout(1200)
        except Exception:
            pass

    if _ai_switch_is_on(page):
        logger.info("目标开关已是开启状态")
        logger.info("已勾选「AI 生成的内容」")
        return

    if not _text_visible(page, _AI_KEYWORDS, timeout=800) and not _settings_expanded(page):
        try:
            self._screenshot(page, "ai_show_more_fail")
        except Exception:
            pass
        raise RuntimeError("未能展开「显示更多」，看不到 AI 生成的内容开关")

    for attempt in range(1, 6):
        try:
            clicked = _click_ai_switch_once(page)
            if not clicked:
                logger.info("第%d次未定位到可点的 AI 开关", attempt)
        except Exception as e:
            logger.warning("AI 开关第%d次尝试异常: %s", attempt, e)
        page.wait_for_timeout(900)
        if _ai_switch_is_on(page):
            logger.info("开关已开启（第%d次点击生效）", attempt)
            logger.info("已勾选「AI 生成的内容」")
            return
        logger.info("AI 开关尚未开启，准备第%d次重试", attempt + 1)

    try:
        self._screenshot(page, "ai_switch_fail")
    except Exception:
        pass
    raise RuntimeError("未能打开「AI生成的内容」选项")


def run_with_replace_loop_fixed(
    uploader,
    product_id: str,
    video_paths: list,
    caption_factory: Callable[[Any], str],
    product_display_name: str | None = None,
    max_attempts: int = 3,
):
    """审核失败：销毁当前视频 → 点替换/重开上传 → 下一支视频。"""
    import tiktok_uploader

    PublishResult = tiktok_uploader.PublishResult
    PublishJob = tiktok_uploader.PublishJob
    CheckStatus = tiktok_uploader.CheckStatus

    last = PublishResult(False, "error", "无可用视频", "", product_id)
    paths = list(video_paths or [])[: max(1, int(max_attempts or 1))]
    for i, video in enumerate(paths):
        video = Path(video)
        caption = caption_factory(video)
        display = product_display_name
        try:
            from fix_content_i18n import get_cart_display_for_product

            display = get_cart_display_for_product(str(product_id), product_display_name)
        except Exception:
            pass
        job = PublishJob(
            product_id=product_id,
            video_path=video,
            caption=caption,
            product_display_name=display,
        )
        if i == 0:
            last = uploader.publish(job)
        else:
            replaced = False
            try:
                replaced = bool(uploader.click_replace_for_next_video())
            except Exception as e:
                logger.warning("点击替换异常: %s", e)
            if not replaced:
                logger.warning("未点到替换，重新打开上传页")
                try:
                    uploader.open_upload_page(force=True)
                except Exception as e:
                    logger.warning("重新打开上传页失败: %s", e)
            last = uploader.publish(job)

        if last.ok:
            return last

        fail_value = getattr(CheckStatus.FAIL, "value", "fail")
        if getattr(last, "status", None) != fail_value:
            return last

        logger.info("视频 %s 审核不通过，销毁后尝试下一个", video.name)
        reason = str(getattr(last, "message", "") or "") or "内容审核不通过"
        status = str(getattr(last, "status", "") or "fail")
        _destroy_failed_video(
            product_id,
            video,
            reason=reason,
            status=status,
        )

    return last


def apply_post_ai_check_patches() -> None:
    import publisher
    import tiktok_uploader

    tiktok_uploader.TikTokStudioUploader._open_ai_more_if_any = open_ai_more_fixed  # type: ignore[method-assign]
    tiktok_uploader.run_with_replace_loop = run_with_replace_loop_fixed  # type: ignore[assignment]
    publisher.run_with_replace_loop = run_with_replace_loop_fixed  # type: ignore[assignment]
    logger.info("已应用补丁：显示更多必须真展开 + AI开关点可见轨道（昨晚成功逻辑）")
