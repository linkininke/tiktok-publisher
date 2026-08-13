"""给主窗口和任务栏挂上 assets/app.ico。

CustomTkinter 会在 __init__ 里设自己的默认图标，构造完立刻调 iconbitmap 常被它覆盖，
所以这里在 __init__ 之后再用 after 延迟设一次。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("publisher")


def icon_path() -> Path | None:
    roots = []
    try:
        import config_loader

        r = getattr(config_loader, "ROOT", None)
        if r:
            roots.append(Path(r))
    except Exception:
        pass
    roots.append(Path(__file__).resolve().parent.parent)
    for root in roots:
        p = root / "assets" / "app.ico"
        if p.exists():
            return p
    return None


def apply_window_icon(win) -> bool:
    p = icon_path()
    if p is None:
        logger.warning("没找到 assets/app.ico，窗口图标沿用默认")
        return False
    try:
        win.iconbitmap(str(p))
        # 有些 Tk 版本 iconbitmap 只影响标题栏，再补一次 iconphoto 兜住任务栏
        try:
            import tkinter

            win._ttp_icon_img = tkinter.PhotoImage(file=str(p.with_name("app_icon.png")))
            win.iconphoto(True, win._ttp_icon_img)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("设置窗口图标失败: %s", e)
        return False


def apply_app_icon_patches() -> None:
    from ui.app import App

    if getattr(App, "_app_icon_patched", False):
        return
    original_init = App.__init__

    def init_with_icon(self, *a, **k):
        out = original_init(self, *a, **k)
        apply_window_icon(self)
        try:
            # CustomTkinter 稍后会再设一次它的默认图标，延迟覆盖回来
            self.after(300, lambda: apply_window_icon(self))
        except Exception:
            pass
        return out

    App.__init__ = init_with_icon  # type: ignore[method-assign]
    App._app_icon_patched = True  # type: ignore[attr-defined]
    logger.info("已应用补丁：主窗口/任务栏图标使用 assets/app.ico")
