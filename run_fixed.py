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


def _bypass_proxy_for_localhost() -> None:
    """本机地址不走代理。

    比特浏览器 API 在 127.0.0.1:54345。只要设了 HTTP_PROXY，requests 就会把
    这个请求也送去代理，表现为「未获取到任何比特窗口」或健康检查报
    HTTPConnectionPool(host='127.0.0.1', port=<代理端口>)。
    """
    import os

    keep = {x.strip() for x in (os.environ.get("NO_PROXY") or "").split(",") if x.strip()}
    keep.update(("127.0.0.1", "localhost", "::1"))
    joined = ",".join(sorted(keep))
    os.environ["NO_PROXY"] = joined
    os.environ["no_proxy"] = joined


def main() -> None:
    _enable_dpi_awareness()
    root = Path(__file__).resolve().parent
    import os

    os.chdir(root)
    # 必须在任何 requests 调用之前
    _bypass_proxy_for_localhost()
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
    from fix_cart_display_fill import apply_cart_display_fill_patches
    from fix_incident_log import apply_incident_log_patches, apply_incident_ui_patches
    from fix_ui_extras import apply_ui_extras_patches
    from fix_app_icon import apply_app_icon_patches
    from fix_all_proxies_failed import apply_all_proxies_failed_patches
    from fix_precheck_hang import apply_precheck_hang_patches
    from fix_proxy_probe import apply_proxy_probe_patches
    from fix_publish_verify import apply_publish_verify_patches
    from fix_review_replace import apply_review_replace_patches
    from fix_no_proxy_precheck import apply_no_proxy_precheck_patches
    from fix_parallel_group import apply_parallel_group_patches
    from fix_group_rotation import apply_group_rotation_patches
    from fix_video_tab import apply_video_tab_patches
    from fix_scheduler import apply_scheduler_patches
    from fix_dashboard import apply_dashboard_patches

    apply_patches()
    apply_sync_excel_patches()
    apply_post_ai_check_patches()
    apply_caption_hashtag_patches()
    apply_content_i18n_patches()
    # 必须在 discard_draft / caption_hashtags 之后：包住 _click_modal_primary 与 AI「显示更多」
    apply_modal_add_click_patches()
    # 商品展示名：英文等非中文界面下原版会写错元素还报成功
    apply_cart_display_fill_patches()
    apply_incident_log_patches()
    # UI 补丁：先 extras 再 incidents（incidents 再包一层 __init__ / 导航）
    apply_ui_extras_patches()
    apply_incident_ui_patches()
    # 放在两个 UI 补丁之后：它们也包了 __init__，图标要最后设才不会被盖掉
    apply_app_icon_patches()
    # 审核不通过（同质化/重复内容）时判为 FAIL，交给替换流程
    apply_review_replace_patches()
    # 必须在 all_proxies_failed 之前：它要包住 _filter_by_proxy_health 统计调度数
    apply_no_proxy_precheck_patches()
    # 最后包一层：全部代理失败 → 立刻停 + 弹窗
    apply_all_proxies_failed_patches()
    # 网络预检后代理体检进度/超时，避免界面像卡住
    apply_precheck_hang_patches()
    # 必须在 precheck_hang 之后：代理体检换成真实连通性探测
    apply_proxy_probe_patches()
    # 放最后包住 _click_post：确认发布真的成功，未确认则保留视频
    apply_publish_verify_patches()
    # 并发相关：保存设置别把并行开关写没、分组失败重试、每轮打印并发画像
    apply_parallel_group_patches()
    # 必须在 all_proxies_failed / parallel_group 之后：它要接手 _run_ip_group 做跨组调度
    apply_group_rotation_patches()
    # 上传前切「视频」页签：泰文/英文界面下原版只认中文「视频」，会卡在照片框
    apply_video_tab_patches()
    # 放在所有 UI 补丁之后：它也包了 __init__ 和 _build_settings
    apply_scheduler_patches()
    # 数据看板：只读发布记录，必须在 incident_ui 之后（它把 NAV_ITEMS 换成了 tuple）
    apply_dashboard_patches()

    from ui.app import main as ui_main

    ui_main()


if __name__ == "__main__":
    main()
