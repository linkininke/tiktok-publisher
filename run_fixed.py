"""带草稿弹窗修复的启动入口（不改原 exe，热补丁后启动 UI）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def _enable_dpi_awareness() -> None:
    """高分屏/125%/150% 缩放时减少坐标偏移。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # Per-monitor v2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _setup_paths(root: Path) -> None:
    # 注意：多次 insert(0) 会反转顺序。必须保证 patches 最终在最前，
    # 否则会优先加载 _tools/runtime_site 里的旧 .pyc。
    runtime = root / "_tools" / "runtime_site"
    internal = root / "_internal"
    patches = root / "patches"
    for p in (str(root), str(internal), str(runtime), str(patches)):
        while p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)


def _setup_logging(root: Path) -> None:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "publisher.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(
        isinstance(h, logging.FileHandler)
        and Path(getattr(h, "baseFilename", "")) == log_file
        for h in root_logger.handlers
    ):
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root_logger.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root_logger.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root_logger.addHandler(sh)


def _setup_tcl_tk(root: Path) -> None:
    """嵌入式 Python 无 PyInstaller _MEIPASS，需显式指向 _internal 的 Tcl/Tk。"""
    import os

    tcl = root / "_internal" / "_tcl_data"
    tk = root / "_internal" / "_tk_data"
    if tcl.is_dir():
        os.environ.setdefault("TCL_LIBRARY", str(tcl))
    if tk.is_dir():
        os.environ.setdefault("TK_LIBRARY", str(tk))


def main() -> None:
    _enable_dpi_awareness()
    root = Path(__file__).resolve().parent
    import os

    os.chdir(root)
    _setup_tcl_tk(root)
    _setup_paths(root)
    _setup_logging(root)

    # config_loader._app_root() 在非 frozen 时会把 runtime_site 的上上级当成根（即 _tools）
    import config_loader

    config_loader.ROOT = root

    from fix_discard_draft import apply_patches
    from fix_sync_excel import apply_sync_excel_patches
    from fix_post_ai_check import apply_post_ai_check_patches
    from fix_caption_hashtags import apply_caption_hashtag_patches
    from fix_content_i18n import apply_content_i18n_patches
    from fix_modal_add_click import apply_modal_add_click_patches
    from fix_incident_log import apply_incident_log_patches, apply_incident_ui_patches
    from fix_ui_extras import apply_ui_extras_patches
    from fix_all_proxies_failed import apply_all_proxies_failed_patches
    from fix_precheck_hang import apply_precheck_hang_patches
    from fix_proxy_probe import apply_proxy_probe_patches
    from fix_publish_verify import apply_publish_verify_patches

    apply_patches()
    apply_sync_excel_patches()
    apply_post_ai_check_patches()
    apply_caption_hashtag_patches()
    apply_content_i18n_patches()
    # 必须在 discard_draft / caption_hashtags 之后：包住 _click_modal_primary 与 AI「显示更多」
    apply_modal_add_click_patches()
    apply_incident_log_patches()
    # UI 补丁：先 extras 再 incidents（incidents 再包一层 __init__ / 导航）
    apply_ui_extras_patches()
    apply_incident_ui_patches()
    # 最后包一层：全部代理失败 → 立刻停 + 弹窗
    apply_all_proxies_failed_patches()
    # 网络预检后代理体检进度/超时，避免界面像卡住
    apply_precheck_hang_patches()
    # 必须在 precheck_hang 之后：代理体检换成真实连通性探测
    apply_proxy_probe_patches()
    # 放最后包住 _click_post：确认发布真的成功，未确认则保留视频
    apply_publish_verify_patches()

    from ui.app import main as ui_main

    ui_main()


if __name__ == "__main__":
    main()
