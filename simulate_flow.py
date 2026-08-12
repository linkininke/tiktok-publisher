"""模拟单账号发布前半段，追踪 page 身份，定位「旧页填完再开新页」问题。"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [
    str(ROOT / "patches"),
    str(ROOT / "_tools" / "runtime_site"),
    str(ROOT / "_internal"),
    str(ROOT),
]

import os

os.chdir(ROOT)
os.environ.setdefault("TCL_LIBRARY", str(ROOT / "_internal" / "_tcl_data"))
os.environ.setdefault("TK_LIBRARY", str(ROOT / "_internal" / "_tk_data"))
os.environ.setdefault("PYTHONUTF8", "1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(ROOT / "logs" / "simulate_flow.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("simulate")


def page_tag(page) -> str:
    if page is None:
        return "None"
    try:
        return f"id={id(page)} url={page.url!r}"
    except Exception as e:
        return f"id={id(page)} DEAD({e})"


def main() -> None:
    import config_loader
    from fix_discard_draft import apply_patches, UPLOAD_URL
    from bitbrowser import BitBrowser
    from tiktok_uploader import TikTokStudioUploader

    config_loader.ROOT = ROOT
    apply_patches()

    cfg = config_loader.load_config(ROOT / "config.yaml")
    # 用最近失败账号，避免读 accounts 密文；profile 以 BitBrowser 为准
    import json

    st = json.loads((ROOT / "data" / "publish_state.json").read_text(encoding="utf-8"))
    profile_id = None
    for row in reversed(st.get("published") or []):
        if row.get("account"):
            profile_id = row["account"]
            break
    if not profile_id:
        raise SystemExit("no account in publish_state")

    api = cfg["bitbrowser"]["api"]
    upload_url = (cfg.get("publish") or {}).get("upload_url") or UPLOAD_URL
    bb = BitBrowser(api)
    log.info("=== SIM start profile=%s upload_url=%s", profile_id[:8], upload_url)

    opened = bb.open(profile_id)
    ws = opened.get("ws") if isinstance(opened, dict) else None
    if not ws:
        http = (opened or {}).get("http")
        ws = http if str(http).startswith("http") else f"http://{http}"
    log.info("BitBrowser opened ws=%s", ws)

    pub = cfg.get("publish") or {}
    uploader = TikTokStudioUploader(
        upload_url=upload_url,
        content_check_timeout=60,
        content_check_poll_interval=5,
        dry_run=True,
        viewport=(
            int(pub.get("viewport_width") or 1440),
            int(pub.get("viewport_height") or 900),
        ),
        force_viewport=bool(pub.get("force_viewport", True)),
    )

    # wrap key methods with tracing
    real_connect = uploader.connect
    real_open = uploader.open_upload_page
    real_goto = uploader._goto_upload
    real_ensure = uploader.ensure_page

    def traced_connect(ws_endpoint):
        log.info("[1] connect BEFORE page=%s", page_tag(getattr(uploader, "page", None)))
        ret = real_connect(ws_endpoint)
        log.info("[1] connect AFTER  page=%s ret=%s", page_tag(uploader.page), page_tag(ret))
        log.info(
            "[1] pages=%s",
            [page_tag(p) for p in uploader.page.context.pages],
        )
        return ret

    def traced_open(force=True):
        log.info("[2] open_upload BEFORE page=%s", page_tag(getattr(uploader, "page", None)))
        ret = real_open(force=force)
        log.info("[2] open_upload AFTER  page=%s ret=%s", page_tag(uploader.page), page_tag(ret))
        log.info(
            "[2] pages=%s",
            [page_tag(p) for p in uploader.page.context.pages],
        )
        return ret

    def traced_goto(page):
        log.info("[3] _goto_upload BEFORE arg=%s self=%s", page_tag(page), page_tag(uploader.page))
        ret = real_goto(page)
        log.info("[3] _goto_upload AFTER  self=%s ret=%s", page_tag(uploader.page), page_tag(ret))
        # 模拟 publish()：ensure_page 后再用 page 填表
        p2 = uploader.ensure_page()
        log.info("[4] publish-like ensure_page -> %s", page_tag(p2))
        if p2 is not uploader.page:
            log.error("BUG: ensure_page 返回的不是 self.page")
        if id(p2) != id(ret) and ret is not None:
            log.warning("WARN: ensure_page 与 _goto_upload 返回值不是同一 page 对象")
        # 模拟「填信息」：在当前页 body 写入标记，看之后是否丢页
        try:
            p2.evaluate(
                """() => {
                  window.__SIM_MARK__ = 'filled-on-' + location.href;
                  const el = document.createElement('div');
                  el.id = 'sim-mark';
                  el.textContent = window.__SIM_MARK__;
                  el.style.cssText = 'position:fixed;top:0;left:0;z-index:99999;background:red;color:#fff;padding:8px';
                  document.body.appendChild(el);
                }"""
            )
            mark = p2.evaluate("() => window.__SIM_MARK__")
            log.info("[5] simulated fill on page mark=%s", mark)
        except Exception as e:
            log.error("[5] simulated fill FAILED: %s", e)

        # 再调用一次 _goto_upload，模拟多步骤/二次进入是否会换页丢掉填写
        log.info("[6] second _goto_upload (like next step/retry)")
        before_pages = [id(p) for p in uploader.page.context.pages]
        ret2 = real_goto(uploader.page)
        after_pages = [id(p) for p in uploader.page.context.pages]
        log.info("[6] after second goto self=%s", page_tag(uploader.page))
        try:
            mark2 = uploader.page.evaluate("() => window.__SIM_MARK__")
            log.info("[6] mark still present? %s", mark2)
            if not mark2:
                log.error("ROOT CAUSE CANDIDATE: 二次 _goto_upload 后填写标记丢失（换页/重载）")
        except Exception as e:
            log.error("ROOT CAUSE CANDIDATE: 二次 _goto_upload 后原页失效: %s", e)
        log.info("[6] page id set before=%s after=%s", before_pages, after_pages)
        return ret

    uploader.connect = traced_connect  # type: ignore
    uploader.open_upload_page = traced_open  # type: ignore
    uploader._goto_upload = traced_goto  # type: ignore

    try:
        uploader.connect(ws)
        try:
            uploader.open_upload_page()
        except Exception as e:
            log.error("open_upload_page error: %s", e)
        # 模拟 publish 开头
        page = uploader.ensure_page()
        uploader._goto_upload(page)
        log.info("=== SIM done ===")
        time.sleep(2)
    finally:
        try:
            uploader.disconnect(close_browser=False)
        except Exception:
            pass
        try:
            bb.close(profile_id)
        except Exception:
            pass


if __name__ == "__main__":
    main()
