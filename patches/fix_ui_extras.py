"""UI 增强：语种选项、代理端口、强制快速终止、主窗可关。"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("publisher")

_LANG_CHOICES = []  # 保留占位，语种已改为始终跟随 Excel 品名
_MODE_CHOICES = []


def _find_scrollable(widget):
    name = widget.__class__.__name__
    if "Scrollable" in name:
        return widget
    try:
        for ch in widget.winfo_children():
            found = _find_scrollable(ch)
            if found is not None:
                return found
    except Exception:
        pass
    return None


def _norm_proxy_port(raw: str) -> str:
    import re

    s = (raw or "").strip()
    if not s:
        return ""
    # 中文/全角冒号、空格、重复冒号 → 正常 host:port
    s = (
        s.replace("：", ":")
        .replace("﹕", ":")
        .replace(";", ":")
        .replace(" ", "")
    )
    s = re.sub(r":{2,}", ":", s)
    # 只填端口
    if s.isdigit():
        return f"http://127.0.0.1:{s}"
    # :7890
    if re.match(r"^:\d+$", s):
        return f"http://127.0.0.1{s}"
    # 127.0.0.1:7890
    if re.match(r"^[\w\.-]+:\d+$", s):
        return f"http://{s}"
    # 已是 URL，但可能被写成 http://127.0.0.1::7890
    if "://" in s:
        s = re.sub(r"(://[^/:]+):+(\d+)", r"\1:\2", s)
        return s
    # 7890 混入其它字符时尽量抽出端口
    m = re.search(r"(\d{2,5})$", s)
    if m and s.replace(m.group(1), "").strip(":/.") in ("", "127.0.0.1", "localhost"):
        return f"http://127.0.0.1:{m.group(1)}"
    return f"http://{s}"


def re_host_port(s: str) -> bool:
    import re

    s = (s or "").replace("：", ":").replace("﹕", ":").strip()
    return bool(re.match(r"^[\w\.-]+:\d+$", s))


def _apply_proxy_env(port_or_url: str) -> tuple[str, str]:
    url = _norm_proxy_port(port_or_url)
    if not url:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
        return "", ""
    os.environ["HTTP_PROXY"] = url
    os.environ["HTTPS_PROXY"] = url
    os.environ["http_proxy"] = url
    os.environ["https_proxy"] = url
    return url, url


def apply_ui_extras_patches() -> None:
    import customtkinter as ctk
    from ui import app as ui_app
    from ui import services as ui_services
    from ui import workers as ui_workers
    import publisher
    import cancel as cancel_mod

    App = ui_app.App
    original_build_settings = App._build_settings
    original_load = App.load_settings_form
    original_save = App._save_settings
    original_stop = App._stop_job
    original_init = App.__init__
    original_save_svc = ui_services.save_settings_from_ui
    original_request_stop = ui_workers.JobRunner.request_stop
    original_interruptible = publisher.PublisherService._interruptible_sleep

    def __init_fixed(self, *a, **k):
        original_init(self, *a, **k)

        def _on_close():
            try:
                if getattr(self, "runner", None) and self.runner.busy:
                    logger.warning("[终止] 关闭窗口 → 强制停止后台任务")
                    try:
                        self.runner.cancel_event.set()
                    except Exception:
                        pass
                    try:
                        self.runner.request_stop()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self.destroy()
            except Exception:
                try:
                    self.quit()
                except Exception:
                    pass

        try:
            self.protocol("WM_DELETE_WINDOW", _on_close)
            # 允许用户随时关窗（进度/主界面都可关）
            self.resizable(True, True)
        except Exception as e:
            logger.debug("绑定关窗协议失败: %s", e)

    def _build_settings_fixed(self):
        original_build_settings(self)
        settings = (getattr(self, "_frames", {}) or {}).get("settings")
        if settings is None:
            return
        scroll = _find_scrollable(settings)
        if scroll is None:
            scroll = settings

        if not hasattr(self, "_set_vars") or self._set_vars is None:
            self._set_vars = {}

        def _next_row() -> int:
            max_r = -1
            try:
                for child in scroll.winfo_children():
                    info = child.grid_info() or {}
                    if not info:
                        continue
                    max_r = max(max_r, int(info.get("row", 0)))
            except Exception:
                pass
            return max_r + 1

        fonts = getattr(ui_app, "FONTS", {})
        colors = getattr(ui_app, "COLORS", {})

        # 把「保存设置」按钮挪到最后，新字段插在它前面
        save_btn = None
        save_info = None
        try:
            for child in list(scroll.winfo_children()):
                try:
                    txt = str(child.cget("text") or "")
                except Exception:
                    txt = ""
                if "保存设置" in txt or txt == "保存":
                    save_btn = child
                    save_info = dict(child.grid_info() or {})
                    break
        except Exception:
            pass

        if save_info and save_info.get("row") is not None:
            r = int(save_info["row"])
        else:
            r = _next_row()

        def _row() -> int:
            nonlocal r
            cur = r
            r += 1
            return cur

        # 分隔标题（跨列）
        rr = _row()
        ctk.CTkLabel(
            scroll,
            text="—— 品名语种 / 小黄车 / 本机代理端口 ——",
            font=fonts.get("body"),
            text_color=colors.get("muted", "#888"),
            anchor="w",
        ).grid(row=rr, column=0, columnspan=3, sticky="w", padx=8, pady=(16, 4))

        # 说明：语种始终跟随 Excel 品名，不再提供「固定语言」干扰自动化
        rr = _row()
        ctk.CTkLabel(
            scroll,
            text="标题/话题/小黄车语言 = 自动跟随 Excel 品名语种（无需手动选）",
            font=fonts.get("small"),
            text_color=colors.get("muted", "#888"),
            anchor="w",
        ).grid(row=rr, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))

        # 小黄车底稿（仅中文品名时使用；其它语种自动换成对应词表）
        rr = _row()
        ctk.CTkLabel(scroll, text="小黄车底稿(中文)", font=fonts.get("body"), anchor="w").grid(
            row=rr, column=0, sticky="w", pady=6
        )
        cart_var = ctk.StringVar(value="小黄车下单")
        cart_entry = ctk.CTkEntry(scroll, textvariable=cart_var, height=34)
        cart_entry.grid(row=rr, column=1, sticky="ew", padx=8, pady=6)
        self._set_vars["product_display_name"] = cart_var
        ctk.CTkLabel(
            scroll,
            text="泰/英等品名会自动换成对应语种文案",
            font=fonts.get("small"),
            text_color=colors.get("muted", "#888"),
            anchor="w",
        ).grid(row=rr, column=2, sticky="w")

        # 代理端口
        rr = _row()
        ctk.CTkLabel(scroll, text="本机代理端口", font=fonts.get("body"), anchor="w").grid(
            row=rr, column=0, sticky="w", pady=6
        )
        port_var = ctk.StringVar(value="7890")
        port_entry = ctk.CTkEntry(scroll, textvariable=port_var, height=34)
        port_entry.grid(row=rr, column=1, sticky="ew", padx=8, pady=6)
        self._set_vars["proxy_port"] = port_var
        ctk.CTkLabel(
            scroll,
            text="如 7890，留空=不走代理",
            font=fonts.get("small"),
            text_color=colors.get("muted", "#888"),
            anchor="w",
        ).grid(row=rr, column=2, sticky="w")

        # 保存按钮移到最底部
        if save_btn is not None:
            kw = {
                "row": _row(),
                "column": int(save_info.get("column", 0) or 0),
                "sticky": save_info.get("sticky") or "w",
            }
            if save_info.get("columnspan"):
                kw["columnspan"] = int(save_info["columnspan"])
            if save_info.get("pady"):
                kw["pady"] = save_info["pady"]
            else:
                kw["pady"] = (12, 8)
            if save_info.get("padx"):
                kw["padx"] = save_info["padx"]
            try:
                save_btn.grid(**kw)
            except Exception:
                save_btn.grid(row=kw["row"], column=0, sticky="w", pady=(12, 8))

        try:
            scroll.grid_columnconfigure(1, weight=1)
        except Exception:
            pass

    def _parse_choice(val: str) -> str:
        s = (val or "").strip()
        if "|" in s:
            return s.split("|", 1)[0].strip()
        return s

    def load_settings_form_fixed(self):
        original_load(self)
        try:
            from ui.services import load_config_raw

            cfg = load_config_raw()
        except Exception:
            try:
                import config_loader

                cfg = config_loader.load_config()
            except Exception:
                return
        content = cfg.get("content") or {}
        pub = cfg.get("publish") or {}
        if "product_display_name" in getattr(self, "_set_vars", {}):
            self._set_vars["product_display_name"].set(
                pub.get("product_display_name") or "小黄车下单"
            )
        # 代理端口：从 HTTP_PROXY 解析
        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
        port = ""
        if proxy:
            import re

            m = re.search(r":(\d+)(?:/|$)", proxy)
            if m:
                port = m.group(1)
            elif proxy.isdigit():
                port = proxy
        if "proxy_port" in getattr(self, "_set_vars", {}):
            self._set_vars["proxy_port"].set(port or "")
        # 同步完整代理输入框
        if proxy and "http_proxy" in getattr(self, "_set_vars", {}):
            try:
                self._set_vars["http_proxy"].set(proxy)
                self._set_vars["https_proxy"].set(
                    os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or proxy
                )
            except Exception:
                pass

    def save_settings_from_ui_fixed(payload: dict):
        cfg_payload = dict(payload or {})
        cart = str(cfg_payload.get("product_display_name") or "小黄车下单").strip() or "小黄车下单"
        port_raw = str(cfg_payload.get("proxy_port") or "").strip()

        # 端口优先：覆盖 http/https proxy 字段
        if "proxy_port" in cfg_payload:
            http_url, https_url = _apply_proxy_env(port_raw)
            cfg_payload["http_proxy"] = http_url
            cfg_payload["https_proxy"] = https_url

        original_save_svc(cfg_payload)

        # 语种始终跟随 Excel 品名；只保存小黄车中文底稿
        try:
            cfg = ui_services.load_config_raw()
            content = cfg.setdefault("content", {})
            content["language_mode"] = "auto_product"
            pub = cfg.setdefault("publish", {})
            pub["product_display_name"] = cart
            ui_services.save_config_raw(cfg)
        except Exception as e:
            logger.warning("写入小黄车配置失败: %s", e)

        if "proxy_port" in cfg_payload:
            logger.info(
                "本机代理已更新: HTTP_PROXY=%s",
                os.environ.get("HTTP_PROXY") or "(已清除)",
            )

    def _save_settings_fixed(self):
        # 把扩展字段塞进 payload
        def _get(key, default=""):
            var = (getattr(self, "_set_vars", {}) or {}).get(key)
            if var is None:
                return default
            try:
                return var.get()
            except Exception:
                return default

        def _num(key):
            raw = str(_get(key, "")).strip()
            if raw == "":
                return None
            try:
                return int(float(raw))
            except Exception:
                return None

        try:
            payload = {
                "bitbrowser_api": _get("bitbrowser_api"),
                "openai_api_key": _get("openai_api_key"),
                "openai_base_url": _get("openai_base_url"),
                "openai_model": _get("openai_model"),
                "http_proxy": _get("http_proxy"),
                "https_proxy": _get("https_proxy"),
                "dry_run": bool(self._set_dry.get()) if hasattr(self, "_set_dry") else False,
                "parallel_ips": bool(self._set_parallel.get())
                if hasattr(self, "_set_parallel")
                else False,
                "posts_per_account": _num("posts_per_account"),
                "rounds_per_day": _num("rounds_per_day"),
                "per_ip_group": _num("per_ip_group"),
                "max_parallel_ips": _num("max_parallel_ips"),
                "account_cooldown_seconds": _num("account_cooldown_seconds"),
                "ip_group_cooldown_seconds": _num("ip_group_cooldown_seconds"),
                "cooldown_jitter_seconds": _num("cooldown_jitter_seconds"),
                "history_days": _num("history_days"),
                "content_check_timeout": _num("content_check_timeout"),
                "round1_hour": _num("round1_hour"),
                "round1_minute": _num("round1_minute"),
                "round2_hour": _num("round2_hour"),
                "round2_minute": _num("round2_minute"),
                "product_display_name": _get("product_display_name"),
                "proxy_port": _get("proxy_port"),
            }

            save_settings_from_ui_fixed(payload)
            from tkinter import messagebox

            messagebox.showinfo("已保存", "设置已写入 config.yaml 与 .env（含语种/代理端口）")
            try:
                self.refresh_overview()
            except Exception:
                pass
        except Exception as e:
            from tkinter import messagebox

            messagebox.showerror("保存失败", str(e))
            logger.exception("保存设置失败")

    def request_stop_fixed(self):
        if not self.busy:
            self.log_q.put("[终止] 当前没有运行中的任务")
            return
        self.cancel_event.set()
        self.log_q.put("[终止] 已强制停止（不等待安全退出）")

    def _stop_job_fixed(self):
        from tkinter import messagebox

        if not self.runner.busy:
            messagebox.showinfo("终止", "当前没有运行中的后台任务")
            return
        # 不弹确认，立即取消；不要提前把 UI 标成空闲（否则可双开任务）
        try:
            self.runner.request_stop()
        except Exception:
            try:
                self.runner.cancel_event.set()
            except Exception:
                pass
        logger.warning("[终止] 用户强制终止")
        try:
            self.show_page("logs")
        except Exception:
            pass

    def interruptible_sleep_fixed(self, seconds: float):
        """快速响应终止：50ms 切片。"""
        import time

        deadline = time.time() + max(0.0, float(seconds or 0))
        while True:
            self._check_cancel()
            remain = deadline - time.time()
            if remain <= 0:
                return
            time.sleep(min(0.05, remain))

    # 缩短内容检查轮询对取消的响应：wrap _wait_content_check
    try:
        import tiktok_uploader

        original_wait_check = tiktok_uploader.TikTokStudioUploader._wait_content_check

        def wait_check_fixed(self, page):
            old_interval = getattr(self, "content_check_poll_interval", 10)
            try:
                self.content_check_poll_interval = min(2, int(old_interval or 10))
                return original_wait_check(self, page)
            finally:
                self.content_check_poll_interval = old_interval

        tiktok_uploader.TikTokStudioUploader._wait_content_check = wait_check_fixed  # type: ignore
    except Exception as e:
        logger.debug("内容检查快速取消补丁跳过: %s", e)

    # 代理/SOCKS 打不开上传页：不写 6 小时冷却，便于换代理后立刻重试
    try:
        from fix_discard_draft import _is_proxy_net_error

        original_mark = publisher.PublisherService._mark_cooldowns
        current_rfa = publisher.PublisherService.run_for_account

        def mark_cooldowns_fixed(self, *a, **k):
            import sys

            exc = sys.exc_info()[1]
            if exc is not None and _is_proxy_net_error(exc):
                logger.warning(
                    "代理/网络失败，跳过写入账号与 IP 组冷却（可马上换代理重试）: %s",
                    str(exc).split("\n", 1)[0][:160],
                )
                return None
            if getattr(self, "_skip_next_cooldown", False):
                self._skip_next_cooldown = False
                logger.warning("已跳过本次冷却写入（代理/网络失败标记）")
                return None
            return original_mark(self, *a, **k)

        def run_for_account_proxy_fixed(self, account, count=None, **kwargs):
            try:
                return current_rfa(self, account, count=count, **kwargs)
            except Exception as e:
                if _is_proxy_net_error(e):
                    self._skip_next_cooldown = True
                    label = (
                        (account or {}).get("name")
                        or (account or {}).get("username")
                        or (account or {}).get("profile_id")
                        or "?"
                    )
                    logger.error(
                        "账号 %s 代理/网络打不开 TikTok，本轮跳过（不写长冷却）: %s",
                        label,
                        str(e).split("\n", 1)[0][:180],
                    )
                raise

        publisher.PublisherService._mark_cooldowns = mark_cooldowns_fixed  # type: ignore
        publisher.PublisherService.run_for_account = run_for_account_proxy_fixed  # type: ignore
        logger.info("已应用补丁：代理/SOCKS 失败跳过账号且不写长冷却")
    except Exception as e:
        logger.warning("代理失败跳过冷却补丁未生效: %s", e)

    App.__init__ = __init_fixed  # type: ignore[method-assign]
    App._build_settings = _build_settings_fixed  # type: ignore[method-assign]
    App.load_settings_form = load_settings_form_fixed  # type: ignore[method-assign]
    App._save_settings = _save_settings_fixed  # type: ignore[method-assign]
    App._stop_job = _stop_job_fixed  # type: ignore[method-assign]
    ui_services.save_settings_from_ui = save_settings_from_ui_fixed  # type: ignore[assignment]
    ui_workers.JobRunner.request_stop = request_stop_fixed  # type: ignore[method-assign]
    publisher.PublisherService._interruptible_sleep = interruptible_sleep_fixed  # type: ignore[method-assign]

    # 原版 submit 已是 daemon，不再全局替换 threading.Thread
    logger.info(
        "已应用补丁：设置页语种/代理端口、强制快速终止、主窗可直接关闭"
    )
