"""一键清理各账号 TikTok Studio 上传页的「未保存草稿」弹窗。

用法：先开比特浏览器本地服务，再运行：
  启动_清理草稿.bat
"""

from __future__ import annotations

import sys
from pathlib import Path

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

from fix_discard_draft import apply_patches, discard_unsaved_draft_fixed  # noqa: E402
import yaml  # noqa: E402
import config_loader  # noqa: E402
from bitbrowser import BitBrowser  # noqa: E402
from config_loader import load_config  # noqa: E402
from tiktok_uploader import TikTokStudioUploader  # noqa: E402

config_loader.ROOT = ROOT


def main() -> None:
    apply_patches()
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
    for acc in accounts:
        pid = acc["profile_id"]
        name = acc.get("name") or pid[:8]
        print(f"[{name}] 打开窗口并清理草稿...")
        try:
            opened = bb.open(pid)
            ws = opened.get("ws") if isinstance(opened, dict) else None
            if not ws:
                print(f"  跳过：未拿到 ws: {opened}")
                continue
            page = uploader.connect(ws)
            page.goto(upload_url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(2000)
            discard_unsaved_draft_fixed(uploader, page)
            print("  完成")
            ok_n += 1
        except Exception as e:
            print(f"  失败: {e}")
        finally:
            try:
                uploader.disconnect(close_browser=False)
            except Exception:
                pass
            try:
                bb.close(pid)
            except Exception:
                pass

    print(f"清理结束：成功 {ok_n}/{len(accounts)}")


if __name__ == "__main__":
    main()
