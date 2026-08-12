"""突发失败：记录后继续下一个账号/任务；UI 增加「异常记录」页。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("publisher")


def _video_name(video) -> str:
    try:
        return Path(video).name
    except Exception:
        return str(video or "")


def apply_incident_log_patches() -> None:
    """须在 fix_post_ai_check.apply_post_ai_check_patches() 之后调用。"""
    import publisher
    import tiktok_uploader
    from incident_log import clear_account_context, record_incident, set_account_context

    fail_value = getattr(tiktok_uploader.CheckStatus.FAIL, "value", "fail")

    # ---- 1) publish：内容审核 FAIL 逐条记录 ----
    if not getattr(tiktok_uploader.TikTokStudioUploader, "_incident_publish_wrapped", False):
        original_publish = tiktok_uploader.TikTokStudioUploader.publish

        def publish_fixed(self, job):
            result = original_publish(self, job)
            try:
                if result is not None and not getattr(result, "ok", False):
                    status = str(getattr(result, "status", "") or "error")
                    if status == fail_value:
                        record_incident(
                            product_id=str(getattr(job, "product_id", "") or ""),
                            product_name=str(getattr(job, "product_display_name", "") or ""),
                            video=_video_name(getattr(job, "video_path", None)),
                            status=status,
                            message=str(getattr(result, "message", "") or ""),
                            dry_run=bool(getattr(self, "dry_run", False)),
                            stage="content_check",
                        )
            except Exception as e:
                logger.debug("单次发布失败记录跳过: %s", e)
            return result

        tiktok_uploader.TikTokStudioUploader.publish = publish_fixed  # type: ignore[method-assign]
        tiktok_uploader.TikTokStudioUploader._incident_publish_wrapped = True  # type: ignore[attr-defined]

    # ---- 2) replace_loop：最终失败（非 content_check）记一条 ----
    base_loop = getattr(publisher, "_incident_base_replace_loop", None)
    if base_loop is None:
        base_loop = tiktok_uploader.run_with_replace_loop
        publisher._incident_base_replace_loop = base_loop  # type: ignore[attr-defined]

    def replace_loop_fixed(
        uploader,
        product_id: str,
        video_paths: list,
        caption_factory,
        product_display_name: str | None = None,
        max_attempts: int = 3,
    ):
        last = base_loop(
            uploader,
            product_id,
            video_paths,
            caption_factory,
            product_display_name=product_display_name,
            max_attempts=max_attempts,
        )
        try:
            dry = bool(getattr(uploader, "dry_run", False))
        except Exception:
            dry = None
        try:
            if last is not None and not getattr(last, "ok", False):
                status = str(getattr(last, "status", "") or "error")
                # 审核 FAIL 已在 publish 里记过，避免重复
                if status != fail_value:
                    video = getattr(last, "video", "") or ""
                    if not video:
                        try:
                            paths = list(video_paths or [])
                            if paths:
                                video = _video_name(paths[-1])
                        except Exception:
                            video = ""
                    record_incident(
                        product_id=str(product_id or getattr(last, "product_id", "") or ""),
                        product_name=str(product_display_name or ""),
                        video=_video_name(video) if video else str(video),
                        status=status,
                        message=str(getattr(last, "message", "") or ""),
                        dry_run=dry,
                        stage="publish",
                    )
        except Exception as e:
            logger.debug("replace_loop 失败记录跳过: %s", e)
        return last

    tiktok_uploader.run_with_replace_loop = replace_loop_fixed  # type: ignore[assignment]
    publisher.run_with_replace_loop = replace_loop_fixed  # type: ignore[assignment]

    # ---- 3) run_for_account：上下文 + 账号级异常记录（仍抛出，上层继续下一号）----
    if getattr(publisher.PublisherService.run_for_account, "_incident_rfa_wrapped", False):
        logger.info("突发失败记录补丁已存在，跳过重复包装")
        return

    current_rfa = publisher.PublisherService.run_for_account

    def run_for_account_incident(self, account, count=None, **kwargs):
        set_account_context(account)
        try:
            dry = None
            try:
                dry = bool((getattr(self, "cfg", {}) or {}).get("publish", {}).get("dry_run"))
            except Exception:
                pass
            try:
                return current_rfa(self, account, count=count, **kwargs)
            except Exception as e:
                msg = str(e).split("\n", 1)[0][:300]
                name = type(e).__name__
                if "Cancel" in name or "终止" in msg or "已终止" in msg:
                    raise
                record_incident(
                    status="exception",
                    message=msg,
                    dry_run=dry,
                    stage="account",
                    extra={"error_type": name},
                )
                raise
        finally:
            clear_account_context()

    run_for_account_incident._incident_rfa_wrapped = True  # type: ignore[attr-defined]
    publisher.PublisherService.run_for_account = run_for_account_incident  # type: ignore[method-assign]
    logger.info("已应用补丁：突发失败记录（只记日志，不暂停账号）")


def _build_incidents_page(app, ctk, ui_app) -> None:
    fonts = getattr(ui_app, "FONTS", {})
    colors = getattr(ui_app, "COLORS", {})

    page = app._page_frame("incidents")
    app._header(page, "异常记录", "单个全自动流程失败时写入 logs/incidents；不中断其它账号")

    body = ctk.CTkScrollableFrame(page, fg_color="transparent")
    body.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 24))
    try:
        page.grid_rowconfigure(1, weight=1)
        page.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
    except Exception:
        pass

    tip = ctk.CTkLabel(
        body,
        text="失败只记录并继续下一个；含账号 / IP / 视频 / 商品ID / 品名。文件：logs/incidents.jsonl 与 incidents.txt",
        font=fonts.get("small"),
        text_color=colors.get("muted", "#888"),
        anchor="w",
        justify="left",
        wraplength=820,
    )
    tip.grid(row=0, column=0, sticky="w", padx=6, pady=(0, 8))

    bar = ctk.CTkFrame(body, fg_color="transparent")
    bar.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 10))

    list_host = ctk.CTkFrame(body, fg_color=colors.get("surface", "#1a2220"), corner_radius=12)
    list_host.grid(row=2, column=0, sticky="nsew", padx=6, pady=6)
    try:
        body.grid_rowconfigure(2, weight=1)
        list_host.grid_columnconfigure(0, weight=1)
    except Exception:
        pass

    app._incidents_list = list_host

    def refresh():
        app.refresh_incidents()

    def open_dir():
        import os
        from incident_log import incidents_jsonl_path

        d = incidents_jsonl_path().parent
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))  # type: ignore[attr-defined]
        except Exception as e:
            from tkinter import messagebox

            messagebox.showerror("打开失败", str(e))

    def open_txt():
        import os
        from incident_log import incidents_txt_path

        p = incidents_txt_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.is_file():
            p.write_text("", encoding="utf-8")
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
        except Exception as e:
            from tkinter import messagebox

            messagebox.showerror("打开失败", str(e))

    ctk.CTkButton(
        bar,
        text="刷新",
        width=90,
        fg_color=colors.get("accent", "#2F6F5E"),
        hover_color=colors.get("accent_hover", "#3D8B75"),
        text_color="#0B1210",
        command=refresh,
    ).pack(side="left")
    ctk.CTkButton(
        bar,
        text="打开日志目录",
        width=120,
        fg_color=colors.get("surface2", "#24302C"),
        border_width=1,
        border_color=colors.get("border", "#3A4A45"),
        command=open_dir,
    ).pack(side="left", padx=(10, 0))
    ctk.CTkButton(
        bar,
        text="打开 incidents.txt",
        width=140,
        fg_color=colors.get("surface2", "#24302C"),
        border_width=1,
        border_color=colors.get("border", "#3A4A45"),
        command=open_txt,
    ).pack(side="left", padx=(10, 0))


def _refresh_incidents(app, ctk, ui_app) -> None:
    from incident_log import read_incidents

    host = getattr(app, "_incidents_list", None)
    if host is None:
        return
    fonts = getattr(ui_app, "FONTS", {})
    colors = getattr(ui_app, "COLORS", {})
    try:
        for ch in list(host.winfo_children()):
            ch.destroy()
    except Exception:
        pass

    rows = read_incidents(300)
    if not rows:
        ctk.CTkLabel(
            host,
            text="暂无突发失败记录。单账号流程失败后会自动出现在这里。",
            font=fonts.get("body"),
            text_color=colors.get("muted", "#888"),
            anchor="w",
        ).pack(anchor="w", padx=16, pady=16)
        return

    for rec in rows:
        card = ctk.CTkFrame(host, fg_color=colors.get("surface2", "#24302C"), corner_radius=8)
        card.pack(fill="x", padx=10, pady=6)
        title = (
            f"{rec.get('ts') or ''}  ·  {rec.get('account') or '-'}  ·  "
            f"IP {rec.get('ip_group') or '-'}  ·  {rec.get('status') or ''}"
        )
        ctk.CTkLabel(
            card,
            text=title,
            font=fonts.get("body"),
            text_color=colors.get("text", "#E8EEEC"),
            anchor="w",
        ).pack(anchor="w", padx=12, pady=(10, 2))
        detail = (
            f"商品ID: {rec.get('product_id') or '-'}  |  品名: {rec.get('product_name') or '-'}  |  "
            f"视频: {rec.get('video') or '-'}  |  阶段: {rec.get('stage') or '-'}"
        )
        if rec.get("proxy_hint"):
            detail += f"  |  代理: {rec.get('proxy_hint')}"
        ctk.CTkLabel(
            card,
            text=detail,
            font=fonts.get("small"),
            text_color=colors.get("muted", "#888"),
            anchor="w",
            justify="left",
            wraplength=800,
        ).pack(anchor="w", padx=12, pady=(0, 2))
        ctk.CTkLabel(
            card,
            text=f"原因: {rec.get('message') or '-'}",
            font=fonts.get("small"),
            text_color=colors.get("warn") or "#C45C63",
            anchor="w",
            justify="left",
            wraplength=800,
        ).pack(anchor="w", padx=12, pady=(0, 10))


def apply_incident_ui_patches() -> None:
    import customtkinter as ctk
    from ui import app as ui_app

    App = ui_app.App

    items = list(getattr(ui_app, "NAV_ITEMS", ()) or ())
    if not any(k == "incidents" for k, _ in items):
        out = []
        for k, v in items:
            if k == "logs":
                out.append(("incidents", "异常记录"))
            out.append((k, v))
        if not any(k == "incidents" for k, _ in out):
            out.append(("incidents", "异常记录"))
        ui_app.NAV_ITEMS = tuple(out)

    original_build_pages = App._build_pages
    original_show = App.show_page

    def build_pages_fixed(self):
        original_build_pages(self)
        try:
            _build_incidents_page(self, ctk, ui_app)
        except Exception as e:
            logger.warning("异常记录页构建失败: %s", e)

    def refresh_incidents(self):
        try:
            _refresh_incidents(self, ctk, ui_app)
        except Exception as e:
            logger.warning("刷新异常记录失败: %s", e)

    def show_page_fixed(self, key):
        original_show(self, key)
        if key == "incidents":
            self.refresh_incidents()

    App._build_pages = build_pages_fixed  # type: ignore[method-assign]
    App.show_page = show_page_fixed  # type: ignore[method-assign]
    App.refresh_incidents = refresh_incidents  # type: ignore[attr-defined]

    # 总览快捷入口（包在现有 __init__ 补丁外层）
    _orig_init = App.__init__

    def __init_fixed(self, *a, **k):
        _orig_init(self, *a, **k)
        try:
            frames = getattr(self, "_frames", {}) or {}
            ov = frames.get("overview")
            if ov is None:
                return
            from fix_ui_extras import _iter_descendants, _widget_text

            for w in _iter_descendants(ov):
                if _widget_text(w) == "刷新状态":
                    parent = w.master
                    btn = ctk.CTkButton(
                        parent,
                        text="异常记录",
                        width=100,
                        fg_color=getattr(ui_app, "COLORS", {}).get("surface2", "#24302C"),
                        border_width=1,
                        border_color=getattr(ui_app, "COLORS", {}).get("border", "#3A4A45"),
                        command=lambda: self.show_page("incidents"),
                    )
                    btn.pack(side="left", padx=(10, 0))
                    break
        except Exception as e:
            logger.debug("总览异常记录入口未加上: %s", e)

    App.__init__ = __init_fixed  # type: ignore[method-assign]
    logger.info("已应用补丁：异常记录 UI 页")
