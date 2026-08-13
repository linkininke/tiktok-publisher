"""让设置页的「第1/2轮 时:分」真正到点自动开跑。

原来这四个框只写进 config.yaml 的 scheduler.round1/round2，然后仅用于
「下次轮询」的显示——没有任何定时器会读它去发起任务，run_daily_poll 只按
「每天轮数」一轮接一轮连着跑。

这里补上：
1. 设置页加一个「定时轮询」开关（写 scheduler.enabled，开关即存，不用点保存）；
2. 后台守护线程每 20 秒看一次表，到点用和手动「多号真实发布」完全相同的
   路径提交一轮任务（同一个 _run_job，所以进度、日志、终止按钮都照旧可用）；
3. 上一轮还没跑完就跳过这次，不排队堆积；
4. 开关旁边显示下次触发时间。

轮次数量受「每天轮数」约束：填 1 就只跑第 1 轮。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger("fix_scheduler")

_TICK_SECONDS = 20
# 到点后这段时间内都还认：万一那一刻正好在忙或程序刚启动，不至于整轮错过
_CATCH_UP_SECONDS = 300


def _load_cfg() -> dict:
    try:
        from ui.services import load_config_raw

        return load_config_raw() or {}
    except Exception:
        try:
            from pathlib import Path

            import config_loader

            root = Path(getattr(config_loader, "ROOT", ".") or ".")
            return config_loader.load_config(root / "config.yaml") or {}
        except Exception as e:
            logger.warning("读取配置失败: %s", e)
            return {}


def _save_enabled(on: bool) -> None:
    try:
        from ui import services as ui_services

        cfg = ui_services.load_config_raw()
        cfg.setdefault("scheduler", {})["enabled"] = bool(on)
        ui_services.save_config_raw(cfg)
    except Exception as e:
        logger.warning("保存定时轮询开关失败: %s", e)


def _as_hm(node, default_h: int, default_m: int) -> tuple[int, int]:
    if isinstance(node, dict):
        h, m = node.get("hour", default_h), node.get("minute", default_m)
    else:
        h, m = default_h, default_m
    try:
        h = int(h)
        m = int(m)
    except Exception:
        h, m = default_h, default_m
    return max(0, min(23, h)), max(0, min(59, m))


def read_targets(cfg: dict) -> list[tuple[str, int, int]]:
    """按「每天轮数」取出要跑的轮次时间。"""
    sch = cfg.get("scheduler") or {}
    poll = cfg.get("poll") or {}
    try:
        rounds = int(poll.get("rounds_per_day") or 1)
    except Exception:
        rounds = 1
    rounds = max(0, min(2, rounds))

    out: list[tuple[str, int, int]] = []
    if rounds >= 1:
        h, m = _as_hm(sch.get("round1"), 10, 0)
        out.append(("第1轮", h, m))
    if rounds >= 2:
        h, m = _as_hm(sch.get("round2"), 18, 0)
        out.append(("第2轮", h, m))
    return out


def next_run_at(now: datetime, targets: list[tuple[str, int, int]]) -> tuple[str, datetime] | None:
    best: tuple[str, datetime] | None = None
    for label, h, m in targets:
        for day in (0, 1):
            at = (now + timedelta(days=day)).replace(
                hour=h, minute=m, second=0, microsecond=0
            )
            if at >= now.replace(second=0, microsecond=0) and (best is None or at < best[1]):
                best = (label, at)
                break
    return best


def due_target(
    now: datetime, targets: list[tuple[str, int, int]], fired: set
) -> tuple[str, datetime] | None:
    for label, h, m in targets:
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        key = (at.date(), label)
        if key in fired:
            continue
        gap = (now - at).total_seconds()
        if 0 <= gap < _CATCH_UP_SECONDS:
            return label, at
    return None


def _launch_round(app, label: str) -> None:
    """走和手动「多号真实发布」一模一样的路径，只是不弹确认框。"""

    def job():
        from publisher import PublisherService

        print(f"[定时轮询] {label} 到点，开始真实发布", flush=True)
        app._guard_network()
        svc = PublisherService(cancel_event=app.runner.cancel_event)
        svc.cfg.setdefault("publish", {})["dry_run"] = False
        summary = svc.run_round(rounds=1)
        print(f"[定时轮询] {label} 结束", flush=True)
        return summary

    app._run_job(f"定时轮询·{label}", job)


def apply_scheduler_patches() -> None:
    import customtkinter as ctk
    from ui import app as ui_app

    App = ui_app.App
    if getattr(App, "_scheduler_patched", False):
        return

    original_init = App.__init__
    original_build = App._build_settings

    def build_settings_with_switch(self):
        original_build(self)
        settings = (getattr(self, "_frames", {}) or {}).get("settings")
        if settings is None:
            return
        try:
            from fix_ui_extras import _find_scrollable

            scroll = _find_scrollable(settings) or settings
        except Exception:
            scroll = settings

        # 插在「保存设置」上面，保存按钮顺延一行
        save_btn = None
        save_info = None
        for child in list(scroll.winfo_children()):
            try:
                txt = str(child.cget("text") or "")
            except Exception:
                txt = ""
            if "保存设置" in txt or txt == "保存":
                save_btn = child
                save_info = dict(child.grid_info() or {})
                break

        if save_info and save_info.get("row") is not None:
            row = int(save_info["row"])
        else:
            row = 0
            for child in scroll.winfo_children():
                info = child.grid_info() or {}
                if info:
                    row = max(row, int(info.get("row", 0)) + 1)

        fonts = getattr(ui_app, "FONTS", {})
        colors = getattr(ui_app, "COLORS", {})
        enabled = bool((_load_cfg().get("scheduler") or {}).get("enabled"))

        ctk.CTkLabel(scroll, text="定时轮询", font=fonts.get("body"), anchor="w").grid(
            row=row, column=0, sticky="w", pady=6
        )
        var = ctk.BooleanVar(value=enabled)
        hint = ctk.CTkLabel(
            scroll,
            text="",
            font=fonts.get("small"),
            text_color=colors.get("muted", "#888"),
            anchor="w",
        )

        def on_toggle():
            on = bool(var.get())
            _save_enabled(on)
            logger.info("定时轮询已%s", "开启" if on else "关闭")
            _refresh_hint()

        ctk.CTkSwitch(
            scroll,
            text="到点自动按上面的时间开跑一轮真实发布",
            variable=var,
            command=on_toggle,
        ).grid(row=row, column=1, sticky="w", padx=8, pady=6)
        hint.grid(row=row, column=2, sticky="w")

        def _refresh_hint():
            try:
                if not var.get():
                    hint.configure(text="未开启")
                    return
                nxt = next_run_at(datetime.now(), read_targets(_load_cfg()))
                hint.configure(
                    text=f"下次 {nxt[1]:%m-%d %H:%M}（{nxt[0]}）" if nxt else "每天轮数为 0"
                )
            except Exception:
                pass

        self._sched_var = var
        self._sched_hint = _refresh_hint
        _refresh_hint()

        if save_btn is not None and save_info:
            kw = {
                "row": row + 1,
                "column": int(save_info.get("column", 0) or 0),
                "sticky": save_info.get("sticky") or "w",
            }
            if save_info.get("columnspan"):
                kw["columnspan"] = int(save_info["columnspan"])
            if save_info.get("pady") is not None:
                kw["pady"] = save_info["pady"]
            if save_info.get("padx") is not None:
                kw["padx"] = save_info["padx"]
            try:
                save_btn.grid(**kw)
            except Exception as e:
                logger.debug("保存按钮顺延失败: %s", e)

    def start_loop(app) -> None:
        fired: set = set()
        stop = threading.Event()
        app._sched_stop = stop

        def loop():
            logger.info("定时轮询守护线程已启动（每 %ds 看一次表）", _TICK_SECONDS)
            while not stop.wait(_TICK_SECONDS):
                try:
                    cfg = _load_cfg()
                    if not (cfg.get("scheduler") or {}).get("enabled"):
                        continue
                    targets = read_targets(cfg)
                    hit = due_target(datetime.now(), targets, fired)
                    if not hit:
                        continue
                    label, at = hit
                    fired.add((at.date(), label))
                    if len(fired) > 8:
                        fired.clear()
                        fired.add((at.date(), label))
                    if app.runner.busy():
                        logger.warning("%s 到点，但上一轮还在跑，跳过这次", label)
                        print(f"[定时轮询] {label} 到点，但任务还在跑，跳过", flush=True)
                        continue
                    logger.info("%s 到点（%s），提交任务", label, at.strftime("%H:%M"))
                    app.after(0, lambda l=label: _launch_round(app, l))
                except Exception as e:
                    logger.warning("定时轮询检查出错: %s", e)

        threading.Thread(target=loop, name="poll-scheduler", daemon=True).start()

    def init_with_scheduler(self, *a, **k):
        original_init(self, *a, **k)
        try:
            start_loop(self)
        except Exception as e:
            logger.warning("启动定时轮询失败: %s", e)

    App._build_settings = build_settings_with_switch  # type: ignore[method-assign]
    App.__init__ = init_with_scheduler  # type: ignore[method-assign]
    App._scheduler_patched = True  # type: ignore[attr-defined]
    logger.info("已应用补丁：第1/2轮时间接上真实定时器（设置页「定时轮询」开关）")
