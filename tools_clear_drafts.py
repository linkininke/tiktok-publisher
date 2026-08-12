"""一键清理各账号 TikTok Studio 上传页的「未保存草稿」弹窗。

用法：先开比特浏览器本地服务，再运行：
  启动_清理草稿.bat
或在修复版 UI 总览点「清理草稿」。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
import os

os.chdir(ROOT)
tcl = ROOT / "_internal" / "_tcl_data"
tk = ROOT / "_internal" / "_tk_data"
if tcl.is_dir():
    os.environ.setdefault("TCL_LIBRARY", str(tcl))
if tk.is_dir():
    os.environ.setdefault("TK_LIBRARY", str(tk))

sys.path[:0] = [
    str(ROOT / "patches"),
    str(ROOT / "_tools" / "runtime_site"),
    str(ROOT / "_internal"),
    str(ROOT),
]

logger = logging.getLogger("publisher")


def run_clear_drafts(
    *,
    cancel_event=None,
    log: Callable[[str], None] | None = None,
    apply_patch: bool = True,
) -> dict[str, Any]:
    """逐个打开启用账号的上传页并放弃未保存草稿。

    返回: {ok, fail, total, errors: [{name, error}, ...], cancelled: bool}
    """
    from fix_discard_draft import apply_patches, discard_unsaved_draft_fixed
    import yaml
    import config_loader
    from bitbrowser import BitBrowser
    from config_loader import load_config
    from tiktok_uploader import TikTokStudioUploader

    config_loader.ROOT = ROOT
    if apply_patch:
        apply_patches()

    def _log(msg: str) -> None:
        if log:
            log(msg)
        else:
            print(msg)
        logger.info("%s", msg)

    def _cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    cfg = load_config(ROOT / "config.yaml")
    api = cfg["bitbrowser"]["api"]
    accounts_path = ROOT / cfg["paths"]["accounts_file"]
    data = yaml.safe_load(accounts_path.read_text(encoding="utf-8")) or {}
    accounts = [a for a in data.get("accounts", []) if a.get("enabled", True)]
    upload_url = cfg["publish"]["upload_url"]

    bb = BitBrowser(api)
    uploader = TikTokStudioUploader(
        upload_url=upload_url,
        content_check_timeout=int(cfg["publish"].get("content_check_timeout", 180)),
        content_check_poll_interval=int(cfg["publish"].get("content_check_poll_interval", 10)),
        dry_run=True,
    )

    ok_n = 0
    fail_n = 0
    errors: list[dict[str, str]] = []
    cancelled = False

    for acc in accounts:
        if _cancelled():
            cancelled = True
            _log("已请求终止，停止后续账号清理")
            break
        pid = acc["profile_id"]
        name = acc.get("name") or pid[:8]
        _log(f"[{name}] 打开窗口并清理草稿...")
        try:
            opened = bb.open(pid)
            ws = opened.get("ws") if isinstance(opened, dict) else None
            if not ws:
                msg = f"未拿到 ws: {opened}"
                _log(f"  跳过：{msg}")
                fail_n += 1
                errors.append({"name": str(name), "error": msg})
                continue
            page = uploader.connect(ws)
            page.goto(upload_url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(2000)
            discard_unsaved_draft_fixed(uploader, page)
            _log(f"  [{name}] 完成")
            ok_n += 1
        except Exception as e:
            _log(f"  [{name}] 失败: {e}")
            fail_n += 1
            errors.append({"name": str(name), "error": str(e)})
        finally:
            try:
                uploader.disconnect(close_browser=False)
            except Exception:
                pass
            try:
                bb.close(pid)
            except Exception:
                pass

    summary = {
        "ok": ok_n,
        "fail": fail_n,
        "total": len(accounts),
        "errors": errors,
        "cancelled": cancelled,
    }
    _log(f"清理结束：成功 {ok_n}/{len(accounts)}" + (f"，失败 {fail_n}" if fail_n else ""))
    return summary


def main() -> None:
    run_clear_drafts(apply_patch=True)


if __name__ == "__main__":
    main()
