"""UI 增强：常用操作前置、并行上限、代理端口、强制快速终止、主窗可关。"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("publisher")

_LANG_CHOICES = []  # 保留占位，语种已改为始终跟随 Excel 品名
_MODE_CHOICES = []

# 本机机场默认端口（用户当前环境）
_DEFAULT_PROXY_PORT = "6850"


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


def _widget_text(widget) -> str:
    try:
        return str(widget.cget("text") or "")
    except Exception:
        return ""


def _iter_descendants(widget):
    yield widget
    try:
        kids = list(widget.winfo_children())
    except Exception:
        kids = []
    for ch in kids:
        yield from _iter_descendants(ch)


def _shift_grid_from_row(parent, from_row: int, delta: int) -> None:
    """把 parent 里 row>=from_row 的子控件整体下移 delta。"""
    if delta == 0:
        return
    items = []
    try:
        children = list(parent.winfo_children())
    except Exception:
        return
    for child in children:
        try:
            info = dict(child.grid_info() or {})
        except Exception:
            continue
        if not info or info.get("row") in (None, ""):
            continue
        try:
            r = int(info["row"])
        except Exception:
            continue
        if r >= from_row:
            items.append((child, info, r))
    # 先从大到小挪，避免覆盖
    items.sort(key=lambda x: x[2], reverse=(delta > 0))
    for child, info, r in items:
        kw = {
            "row": r + delta,
            "column": int(info.get("column", 0) or 0),
            "sticky": info.get("sticky") or "",
        }
        if info.get("columnspan"):
            kw["columnspan"] = int(info["columnspan"])
        if info.get("rowspan"):
            kw["rowspan"] = int(info["rowspan"])
        if info.get("padx") not in (None, ""):
            kw["padx"] = info["padx"]
        if info.get("pady") not in (None, ""):
            kw["pady"] = info["pady"]
        try:
            child.grid(**kw)
        except Exception:
            try:
                child.grid(row=r + delta, column=int(info.get("column", 0) or 0))
            except Exception:
                pass


def _find_labeled_row(scroll, label_text: str):
    """找到设置页某标签所在行，返回 (row, label_widget)。"""
    for child in _iter_descendants(scroll):
        if label_text in _widget_text(child):
            try:
                info = child.grid_info() or {}
                if info.get("row") is not None:
                    return int(info["row"]), child
            except Exception:
                continue
    return None, None


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


def _ensure_set_vars(app) -> dict:
    if not hasattr(app, "_set_vars") or app._set_vars is None:
        app._set_vars = {}
    return app._set_vars


def _is_tk_variable(obj) -> bool:
    """区分 StringVar/IntVar 与设置页里的 CTkEntry 控件。"""
    if obj is None:
        return False
    # 原版 _set_vars[key] = CTkEntry；补丁扩展字段才是 StringVar
    return hasattr(obj, "trace_add") and callable(getattr(obj, "set", None))


def _get_form_value(obj, default: str = "") -> str:
    if obj is None:
        return default
    try:
        return str(obj.get()).strip()
    except Exception:
        return default


def _set_form_value(obj, value) -> None:
    """写入 StringVar 或 CTkEntry，避免对 Entry 调用 .set()。"""
    if obj is None:
        return
    text = "" if value is None else str(value)
    if _is_tk_variable(obj):
        try:
            obj.set(text)
            return
        except Exception:
            pass
    # CTkEntry / Entry
    try:
        obj.delete(0, "end")
        if text:
            obj.insert(0, text)
        return
    except Exception:
        pass
    try:
        obj.set(text)
    except Exception:
        logger.debug("写入表单值失败 key=%r", type(obj).__name__)


def _probe_machine_perf() -> dict:
    """探测本机 CPU / 内存（Windows 优先，无第三方依赖）。"""
    import os
    import subprocess

    cores = int(os.cpu_count() or 4)
    ram_gb = 0.0
    # 物理核心（若能读到）
    physical = 0
    try:
        out = subprocess.check_output(
            ["wmic", "cpu", "get", "NumberOfCores,NumberOfLogicalProcessors", "/value"],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in out.splitlines():
            if line.startswith("NumberOfCores="):
                physical = int(line.split("=", 1)[1].strip() or 0)
            elif line.startswith("NumberOfLogicalProcessors="):
                cores = int(line.split("=", 1)[1].strip() or cores)
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value"],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in out.splitlines():
            if line.startswith("TotalPhysicalMemory="):
                raw = int(line.split("=", 1)[1].strip() or 0)
                ram_gb = raw / (1024**3)
                break
    except Exception:
        pass
    if ram_gb <= 0:
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            ram_gb = stat.ullTotalPhys / (1024**3)
        except Exception:
            ram_gb = 8.0
    return {
        "logical_cores": max(1, cores),
        "physical_cores": max(0, physical),
        "ram_gb": float(ram_gb),
    }


def _count_ip_groups() -> int:
    try:
        from ui.services import list_accounts_ui

        rows = list_accounts_ui() or []
        groups = {
            str((r or {}).get("proxy_group") or "").strip()
            for r in rows
            if (r or {}).get("enabled", True)
        }
        groups.discard("")
        return len(groups)
    except Exception:
        try:
            import yaml
            from pathlib import Path
            import config_loader

            root = Path(getattr(config_loader, "ROOT", Path(".")))
            acc = root / "data" / "accounts.yaml"
            if not acc.is_file():
                return 0
            data = yaml.safe_load(acc.read_text(encoding="utf-8")) or {}
            groups = set()
            for a in data.get("accounts") or []:
                if not (a or {}).get("enabled", True):
                    continue
                g = str((a or {}).get("proxy_group") or "").strip()
                if g:
                    groups.add(g)
            return len(groups)
        except Exception:
            return 0


def recommend_parallel_ips() -> dict:
    """按电脑性能推荐并行 IP 数。

    经验：每条并行 IP ≈ 开 1 个比特 Chromium，约吃 1.5～2 逻辑核 + 1.8GB 内存。
    预留 2 核 + 4GB 给系统和本软件。
    """
    perf = _probe_machine_perf()
    cores = int(perf["logical_cores"])
    ram_gb = float(perf["ram_gb"])
    ip_groups = _count_ip_groups()

    # CPU：留 2 核，每窗口约 2 核
    cpu_rec = max(1, (cores - 2) // 2)
    # 内存：留 4GB，每窗口约 1.8GB
    ram_headroom = max(0.0, ram_gb - 4.0)
    ram_rec = max(1, int(ram_headroom // 1.8)) if ram_headroom >= 1.8 else 1

    machine_rec = max(1, min(cpu_rec, ram_rec, 8))  # 软上限 8，避免误推太高
    if ip_groups > 0:
        final = max(1, min(machine_rec, ip_groups))
    else:
        final = machine_rec

    bottleneck = "CPU" if cpu_rec <= ram_rec else "内存"
    return {
        "recommend": final,
        "cpu_rec": cpu_rec,
        "ram_rec": ram_rec,
        "logical_cores": cores,
        "physical_cores": perf.get("physical_cores") or 0,
        "ram_gb": round(ram_gb, 1),
        "ip_groups": ip_groups,
        "bottleneck": bottleneck,
    }


def _apply_parallel_recommend(app, *, ask: bool = True) -> None:
    """写入推荐并行上限，可选弹窗说明。"""
    from tkinter import messagebox

    info = recommend_parallel_ips()
    rec = int(info["recommend"])
    vars_ = _ensure_set_vars(app)
    ov = getattr(app, "_ov_quick", None) or {}

    detail = (
        f"检测到：逻辑核 {info['logical_cores']}"
        + (f"（物理核 {info['physical_cores']}）" if info.get("physical_cores") else "")
        + f" · 内存约 {info['ram_gb']} GB"
        + (f" · 当前 IP 组 {info['ip_groups']} 个" if info["ip_groups"] else " · 尚未同步账号")
        + f"\n\n按性能估算：CPU 可撑约 {info['cpu_rec']}，内存可撑约 {info['ram_rec']}"
        + f"（瓶颈偏{info['bottleneck']}）"
        + f"\n推荐并行 IP 上限 = {rec}"
        + ("\n（已按你现有 IP 组数封顶）" if info["ip_groups"] and rec == info["ip_groups"] else "")
        + "\n\n是否填入并打开「多 IP 并行」？"
    )
    if ask:
        if not messagebox.askyesno("推荐并行数", detail):
            return

    if "max_parallel_ips" not in vars_:
        import customtkinter as ctk

        vars_["max_parallel_ips"] = ctk.StringVar(value=str(rec))
    else:
        _set_form_value(vars_["max_parallel_ips"], str(rec))
    if "max_parallel" in ov:
        try:
            ov["max_parallel"].set(str(rec))
        except Exception:
            pass
    if "parallel" in ov:
        try:
            ov["parallel"].set(1)
        except Exception:
            pass
    try:
        if hasattr(app, "_set_parallel"):
            app._set_parallel.select()
    except Exception:
        pass

    logger.info(
        "推荐并行 IP=%s（CPU=%s RAM=%.1fGB groups=%s）",
        rec,
        info["logical_cores"],
        info["ram_gb"],
        info["ip_groups"],
    )
    if ask:
        messagebox.showinfo(
            "已填入推荐值",
            f"已将并行上限设为 {rec}，并开启多 IP 并行。\n请再点「保存快捷设置」写入配置。",
        )


def _sync_overview_quick_from_settings(app) -> None:
    """设置页开关/输入 → 总览快捷条。"""
    ov = getattr(app, "_ov_quick", None) or {}
    if not ov:
        return
    vars_ = getattr(app, "_set_vars", {}) or {}
    try:
        if hasattr(app, "_set_parallel") and "parallel" in ov:
            on = bool(app._set_parallel.get())
            ov["parallel"].set(1 if on else 0)
    except Exception:
        pass
    try:
        if hasattr(app, "_set_dry") and "dry" in ov:
            on = bool(app._set_dry.get())
            ov["dry"].set(1 if on else 0)
    except Exception:
        pass
    try:
        if "max_parallel_ips" in vars_ and "max_parallel" in ov:
            ov["max_parallel"].set(_get_form_value(vars_["max_parallel_ips"], "0") or "0")
    except Exception:
        pass
    try:
        if "proxy_port" in vars_ and "proxy_port" in ov:
            ov["proxy_port"].set(_get_form_value(vars_["proxy_port"], ""))
    except Exception:
        pass


def _save_overview_quick(app) -> None:
    """从总览快捷条写回设置页控件并保存。"""
    ov = getattr(app, "_ov_quick", None) or {}
    vars_ = _ensure_set_vars(app)
    try:
        if "parallel" in ov and hasattr(app, "_set_parallel"):
            if int(ov["parallel"].get()):
                app._set_parallel.select()
            else:
                app._set_parallel.deselect()
    except Exception:
        pass
    try:
        if "dry" in ov and hasattr(app, "_set_dry"):
            if int(ov["dry"].get()):
                app._set_dry.select()
            else:
                app._set_dry.deselect()
    except Exception:
        pass
    try:
        if "max_parallel" in ov:
            val = str(ov["max_parallel"].get()).strip() or "0"
            if "max_parallel_ips" not in vars_:
                import customtkinter as ctk

                vars_["max_parallel_ips"] = ctk.StringVar(value=val)
            else:
                _set_form_value(vars_["max_parallel_ips"], val)
    except Exception:
        pass
    try:
        if "proxy_port" in ov:
            val = str(ov["proxy_port"].get()).strip()
            if "proxy_port" not in vars_:
                import customtkinter as ctk

                vars_["proxy_port"] = ctk.StringVar(value=val)
            else:
                _set_form_value(vars_["proxy_port"], val)
    except Exception:
        pass
    try:
        app._save_settings()
    except Exception as e:
        logger.warning("快捷设置保存失败: %s", e)


# 常用操作按钮分两行：上行是跑任务，下行是检查和清理
_OPS_ROW_RUN = ("多号干跑", "多号真实发布", "持续运行", "终止后台任务")
_OPS_ROW_AUX = ("代理体检", "重新检测网络", "清理草稿", "清理冷却缓存", "删除视频日志")
# 两行按钮数不同，用 20 列（4 和 5 的公倍数）让每行各自等宽且左右对齐
_OPS_COLS = 20


def _arrange_quick_actions(bar, ctk) -> None:
    """把挤成一长条的常用操作按钮改成两行等宽排列。

    9 个按钮 pack 在同一行会超出窗口宽度，右边几个直接看不见。
    """
    buttons = [w for w in bar.winfo_children() if isinstance(w, ctk.CTkButton)]
    if not buttons:
        return
    by_text = {}
    for w in buttons:
        by_text.setdefault(_widget_text(w), w)

    run = [by_text[t] for t in _OPS_ROW_RUN if t in by_text]
    aux = [by_text[t] for t in _OPS_ROW_AUX if t in by_text]
    placed = set(run) | set(aux)
    aux += [w for w in buttons if w not in placed]
    if not run or not aux:
        return

    for w in buttons:
        try:
            w.pack_forget()
        except Exception:
            pass
    for c in range(_OPS_COLS):
        try:
            bar.grid_columnconfigure(c, weight=1, uniform="ops")
        except Exception:
            pass

    for row, group, height in ((0, run, 40), (1, aux, 34)):
        span = max(1, _OPS_COLS // len(group))
        for i, w in enumerate(group):
            try:
                w.configure(width=110, height=height)
            except Exception:
                pass
            try:
                w.grid(
                    row=row,
                    column=i * span,
                    columnspan=span,
                    sticky="ew",
                    padx=(0 if i == 0 else 8, 0),
                    pady=(0 if row == 0 else 10, 0),
                )
            except Exception:
                pass


def _relayout_overview(body, app, ctk, panel) -> None:
    """总览重排：常用操作和快捷设置提到最上面，状态卡片下移。

    原顺序是 6 张状态卡片占满首屏，最常点的按钮反而要滚动才看得到。
    """
    net = getattr(app, "_ov_net", None)
    ops_bar = foot_bar = tip = None
    cards = []
    for w in body.winfo_children():
        if w is panel or w is net:
            continue
        texts = [_widget_text(x) for x in _iter_descendants(w)]
        if isinstance(w, ctk.CTkLabel):
            if any(t.startswith("提示") for t in texts):
                tip = w
            continue
        if any(t == "常用操作" for t in texts):
            ops_bar = w
            continue
        if any(t == "刷新状态" for t in texts):
            foot_bar = w
            continue
        try:
            info = w.grid_info() or {}
            cards.append((int(info.get("row", 0)), int(info.get("column", 0)), w))
        except Exception:
            continue

    if ops_bar is None:
        # 认不出原布局就别乱动，至少把快捷设置挂上去
        panel.grid(row=99, column=0, columnspan=2, sticky="ew", padx=7, pady=(10, 6))
        return

    def place(widget, row, **kw):
        if widget is None:
            return
        opts = dict(column=0, columnspan=2, sticky="ew", padx=7)
        opts.update(kw)
        try:
            widget.grid(row=row, **opts)
        except Exception:
            pass

    place(ops_bar, 0, pady=(0, 12))
    place(panel, 1, pady=(0, 12))
    place(net, 2, sticky="w", padx=10, pady=(0, 10))

    cards.sort(key=lambda x: (x[0], x[1]))
    for i, (_, col, w) in enumerate(cards):
        try:
            w.grid(row=3 + i // 2, column=col, columnspan=1, sticky="nsew", padx=7, pady=7)
        except Exception:
            pass

    base = 3 + (len(cards) + 1) // 2
    place(foot_bar, base, pady=(16, 0))
    place(tip, base + 1, sticky="w", padx=10, pady=(16, 0))


def _enhance_overview_ux(app, ctk, ui_app) -> None:
    """总览：插入「快捷设置」+ 把高频按钮排到前面。"""
    frames = getattr(app, "_frames", {}) or {}
    page = frames.get("overview")
    if page is None:
        return
    body = _find_scrollable(page)
    if body is None:
        return

    fonts = getattr(ui_app, "FONTS", {})
    colors = getattr(ui_app, "COLORS", {})

    # 先建面板，具体排在第几行由本函数末尾的 _relayout_overview 统一决定
    panel = ctk.CTkFrame(body, fg_color=colors.get("surface", "#1a2220"), corner_radius=12)
    try:
        panel.grid_columnconfigure(1, weight=1)
        panel.grid_columnconfigure(3, weight=1)
    except Exception:
        pass

    ctk.CTkLabel(
        panel,
        text="快捷设置（最常用）",
        font=fonts.get("h2") or fonts.get("body"),
        text_color=colors.get("text", "#E8EEEC"),
        anchor="w",
    ).grid(row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(12, 4))

    ctk.CTkLabel(
        panel,
        text="改完点「保存快捷设置」。多 IP 并行=不同出口同时跑；上限 0=不限制。",
        font=fonts.get("small"),
        text_color=colors.get("muted", "#888"),
        anchor="w",
    ).grid(row=1, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 8))

    # 读取当前配置默认值
    parallel_on = False
    max_par = "0"
    dry_on = False
    port = _DEFAULT_PROXY_PORT
    try:
        from ui.services import load_config_raw

        cfg = load_config_raw()
        poll = cfg.get("poll") or {}
        pub = cfg.get("publish") or {}
        parallel_on = bool(poll.get("parallel_ips"))
        max_par = str(poll.get("max_parallel_ips") if poll.get("max_parallel_ips") is not None else 0)
        dry_on = bool(pub.get("dry_run"))
    except Exception:
        pass
    try:
        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
        if proxy:
            import re

            m = re.search(r":(\d+)(?:/|$)", proxy)
            if m:
                port = m.group(1)
    except Exception:
        pass

    # 总览必须用独立 StringVar：原版 _set_vars['max_parallel_ips'] 是 CTkEntry，
    # 不能当 textvariable（会触发 Entry.trace_add 报错）。
    vars_ = _ensure_set_vars(app)
    parallel_ivar = ctk.IntVar(value=1 if parallel_on else 0)
    dry_ivar = ctk.IntVar(value=1 if dry_on else 0)

    max_init = max_par
    try:
        if "max_parallel_ips" in vars_:
            cur = _get_form_value(vars_["max_parallel_ips"], "")
            if cur != "":
                max_init = cur
    except Exception:
        pass
    max_svar = ctk.StringVar(value=str(max_init))

    port_init = port
    try:
        if "proxy_port" in vars_:
            cur = _get_form_value(vars_["proxy_port"], "")
            # 设置页若已有值优先；否则用探测到的端口
            if cur != "":
                port_init = cur
    except Exception:
        pass
    port_svar = ctk.StringVar(value=str(port_init or ""))

    # row2: 多IP并行 | 并行上限
    ctk.CTkLabel(panel, text="多 IP 并行", font=fonts.get("body"), anchor="w").grid(
        row=2, column=0, sticky="w", padx=(14, 8), pady=6
    )
    sw_par = ctk.CTkSwitch(panel, text="开启", variable=parallel_ivar, width=70)
    sw_par.grid(row=2, column=1, sticky="w", padx=4, pady=6)

    ctk.CTkLabel(panel, text="并行 IP 上限", font=fonts.get("body"), anchor="w").grid(
        row=2, column=2, sticky="w", padx=(18, 8), pady=6
    )
    max_wrap = ctk.CTkFrame(panel, fg_color="transparent")
    max_wrap.grid(row=2, column=3, sticky="w", padx=4, pady=6)
    ent_max = ctk.CTkEntry(max_wrap, textvariable=max_svar, width=70, height=32)
    ent_max.pack(side="left")
    ctk.CTkButton(
        max_wrap,
        text="按性能推荐",
        width=100,
        height=32,
        fg_color=colors.get("surface2", "#24302C"),
        hover_color=colors.get("accent_hover", "#3D8B75"),
        border_width=1,
        border_color=colors.get("border", "#3A4A45"),
        command=lambda: _apply_parallel_recommend(app, ask=True),
    ).pack(side="left", padx=(8, 0))

    # row3: 干跑 | 代理端口
    ctk.CTkLabel(panel, text="干跑模式", font=fonts.get("body"), anchor="w").grid(
        row=3, column=0, sticky="w", padx=(14, 8), pady=6
    )
    sw_dry = ctk.CTkSwitch(panel, text="不点发布", variable=dry_ivar, width=90)
    sw_dry.grid(row=3, column=1, sticky="w", padx=4, pady=6)

    ctk.CTkLabel(panel, text="本机代理端口", font=fonts.get("body"), anchor="w").grid(
        row=3, column=2, sticky="w", padx=(18, 8), pady=6
    )
    ent_port = ctk.CTkEntry(panel, textvariable=port_svar, width=90, height=32)
    ent_port.grid(row=3, column=3, sticky="w", padx=4, pady=6)

    btn_row = ctk.CTkFrame(panel, fg_color="transparent")
    btn_row.grid(row=4, column=0, columnspan=4, sticky="ew", padx=14, pady=(4, 12))

    ctk.CTkButton(
        btn_row,
        text="保存快捷设置",
        width=130,
        height=34,
        fg_color=colors.get("accent", "#2F6F5E"),
        hover_color=colors.get("accent_hover", "#3D8B75"),
        command=lambda: _save_overview_quick(app),
    ).pack(side="left", padx=(0, 8))

    ctk.CTkLabel(
        btn_row,
        text=f"端口示例 {_DEFAULT_PROXY_PORT}；留空=不走代理",
        font=fonts.get("small"),
        text_color=colors.get("muted", "#888"),
        anchor="w",
    ).pack(side="left")

    app._ov_quick = {
        "parallel": parallel_ivar,
        "dry": dry_ivar,
        "max_parallel": max_svar,
        "proxy_port": port_svar,
        "panel": panel,
    }

    # 常用操作栏插入「清理草稿」（插在「终止后台任务」前）
    try:
        stop_btn = None
        for w in _iter_descendants(body):
            if not isinstance(w, ctk.CTkButton):
                continue
            if _widget_text(w) != "终止后台任务":
                continue
            # 只改「常用操作」条里的那个，避免误改日志页同名按钮
            try:
                p = w.master
                for _ in range(4):
                    if p is None:
                        break
                    sibs = []
                    try:
                        sibs = [_widget_text(x) for x in p.winfo_children()]
                    except Exception:
                        pass
                    if "代理体检" in sibs or "多号干跑" in sibs:
                        stop_btn = w
                        break
                    p = getattr(p, "master", None)
            except Exception:
                continue
            if stop_btn is not None:
                break
        if stop_btn is not None:
            qbar = stop_btn.master
            clear_btn = ctk.CTkButton(
                qbar,
                text="清理草稿",
                height=36,
                fg_color=colors.get("surface2", "#24302C"),
                hover_color=colors.get("accent_hover", "#3D8B75"),
                border_width=1,
                border_color=colors.get("border", "#3A4A45"),
                command=lambda: app._start_clear_drafts(),
            )
            clear_btn.pack(side="left", padx=(10, 0), before=stop_btn)
            app._ov_clear_drafts_btn = clear_btn

            cool_btn = ctk.CTkButton(
                qbar,
                text="清理冷却缓存",
                height=36,
                fg_color=colors.get("surface2", "#24302C"),
                hover_color=colors.get("accent_hover", "#3D8B75"),
                border_width=1,
                border_color=colors.get("border", "#3A4A45"),
                command=lambda: app._clear_cooldown_cache(),
            )
            cool_btn.pack(side="left", padx=(10, 0), before=stop_btn)
            app._ov_clear_cooldown_btn = cool_btn

            del_log_btn = ctk.CTkButton(
                qbar,
                text="删除视频日志",
                height=36,
                fg_color=colors.get("surface2", "#24302C"),
                hover_color=colors.get("accent_hover", "#3D8B75"),
                border_width=1,
                border_color=colors.get("border", "#3A4A45"),
                command=lambda: app._open_deleted_videos_log(),
            )
            del_log_btn.pack(side="left", padx=(10, 0), before=stop_btn)
            app._ov_deleted_videos_btn = del_log_btn
            _arrange_quick_actions(stop_btn.master, ctk)
    except Exception as e:
        logger.warning("总览「清理草稿」按钮未加上: %s", e)

    _relayout_overview(body, app, ctk, panel)

    # 高频按钮视觉强调：只改字体，高度交给 _arrange_quick_actions 统一，
    # 否则同一行的按钮会一高一矮。
    # 切勿对按钮内部 Label 调 height——CTkLabel.height 会把按钮撑到上百像素。
    try:
        for w in _iter_descendants(body):
            if isinstance(w, ctk.CTkButton) and _widget_text(w) == "多号真实发布":
                try:
                    w.configure(font=fonts.get("body"))
                except Exception:
                    pass
    except Exception:
        pass


def _start_clear_drafts(app) -> None:
    """总览：后台清理各账号上传页未保存草稿。"""
    from tkinter import messagebox

    if not messagebox.askyesno(
        "清理草稿",
        "将对所有启用账号逐个打开 TikTok 上传页，并放弃「未保存草稿」弹窗。\n"
        "过程中请勿关闭比特浏览器；可随时点「终止后台任务」。\n\n确定开始？",
    ):
        return

    def job():
        from tools_clear_drafts import run_clear_drafts

        cancel = None
        try:
            cancel = app.runner.cancel_event
        except Exception:
            pass
        return run_clear_drafts(cancel_event=cancel, apply_patch=False)

    def done(info):
        info = info or {}
        ok = int(info.get("ok") or 0)
        fail = int(info.get("fail") or 0)
        total = int(info.get("total") or 0)
        cancelled = bool(info.get("cancelled"))
        text = f"成功 {ok}/{total}"
        if fail:
            text += f" · 失败 {fail}"
        if cancelled:
            text += " · 已中途终止"
        errs = info.get("errors") or []
        if errs:
            sample = "；".join(
                f"{e.get('name')}: {str(e.get('error') or '')[:60]}" for e in errs[:5]
            )
            text += f"\n\n失败摘要：{sample}"
        if fail or cancelled:
            messagebox.showwarning("清理草稿", text)
        else:
            messagebox.showinfo("清理草稿", text or "没有启用中的账号")

    app._run_job("清理草稿", job, done)


def _state_file_path():
    from pathlib import Path

    try:
        import config_loader

        root = Path(getattr(config_loader, "ROOT", None) or Path(__file__).resolve().parent.parent)
        cfg = config_loader.load_config(root / "config.yaml")
        return root / cfg["paths"]["state_file"]
    except Exception:
        return Path(__file__).resolve().parent.parent / "data" / "publish_state.json"


def _deleted_videos_log_paths():
    from pathlib import Path

    try:
        import config_loader

        root = Path(getattr(config_loader, "ROOT", None) or Path(__file__).resolve().parent.parent)
        cfg = config_loader.load_config(root / "config.yaml")
        log_dir = root / cfg["paths"].get("log_dir", "logs")
    except Exception:
        root = Path(__file__).resolve().parent.parent
        log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "deleted_videos.txt", log_dir / "deleted_videos.jsonl"


def _clear_cooldown_cache(app) -> None:
    """清空 publish_state.json 里的账号/IP 组冷却，保留发布历史。"""
    import json
    import shutil
    from datetime import datetime
    from tkinter import messagebox

    state = _state_file_path()
    if not state.is_file():
        messagebox.showinfo("清理冷却缓存", f"未找到状态文件：\n{state}")
        return

    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except Exception as e:
        messagebox.showerror("清理冷却缓存", f"读取失败：{e}")
        return

    cool = data.get("cooldowns") if isinstance(data, dict) else None
    if not isinstance(cool, dict):
        cool = {}
    acc = cool.get("accounts") if isinstance(cool.get("accounts"), dict) else {}
    ips = cool.get("ip_groups") if isinstance(cool.get("ip_groups"), dict) else {}
    n_acc, n_ip = len(acc), len(ips)

    if n_acc == 0 and n_ip == 0:
        messagebox.showinfo("清理冷却缓存", "当前没有账号/IP 组冷却记录。")
        return

    if not messagebox.askyesno(
        "清理冷却缓存",
        f"将清空冷却记录（不影响发布历史/已用视频）：\n"
        f"· 账号冷却：{n_acc} 条\n"
        f"· IP 组冷却：{n_ip} 条\n\n"
        f"会先备份再清空。确定？",
    ):
        return

    try:
        bak = state.with_name(
            f"publish_state.bak_cooldown_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        shutil.copy2(state, bak)
        data.setdefault("cooldowns", {})
        data["cooldowns"]["accounts"] = {}
        data["cooldowns"]["ip_groups"] = {}
        state.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info(
            "已清理冷却缓存 accounts=%s ip_groups=%s backup=%s",
            n_acc,
            n_ip,
            bak.name,
        )
        for name in ("refresh_cooldown", "refresh_overview", "_refresh_overview"):
            try:
                fn = getattr(app, name, None)
                if callable(fn):
                    fn()
            except Exception:
                pass
        messagebox.showinfo(
            "清理冷却缓存",
            f"已清空账号冷却 {n_acc}、IP 组冷却 {n_ip}。\n备份：{bak.name}",
        )
    except Exception as e:
        messagebox.showerror("清理冷却缓存", f"写入失败：{e}")


def _open_deleted_videos_log(app) -> None:
    """预览并打开删除视频日志。"""
    import os
    from tkinter import messagebox

    txt, jsonl = _deleted_videos_log_paths()
    if not txt.is_file():
        txt.write_text(
            "# 内容审核不通过并销毁视频时会追加记录（含原因）\n",
            encoding="utf-8",
        )
    try:
        lines = [
            ln
            for ln in txt.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    except Exception:
        lines = []

    preview = "\n".join(lines[-12:]) if lines else "（暂无删除记录）"
    tip = (
        f"文件：{txt}\n"
        f"另有：{jsonl.name}\n\n"
        f"最近记录：\n{preview}\n\n"
        f"点「是」用系统默认程序打开日志文件。"
    )
    if messagebox.askyesno("删除视频日志", tip):
        try:
            os.startfile(str(txt))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("打开失败", str(e))


def _promote_settings_common(app, ctk, ui_app) -> None:
    """设置页顶部加「常用」说明，并把并行上限提示写清楚。"""
    frames = getattr(app, "_frames", {}) or {}
    settings = frames.get("settings")
    if settings is None:
        return
    scroll = _find_scrollable(settings)
    if scroll is None:
        return

    fonts = getattr(ui_app, "FONTS", {})
    colors = getattr(ui_app, "COLORS", {})

    # 顶部插入提示条
    _shift_grid_from_row(scroll, 0, 1)
    tip = ctk.CTkLabel(
        scroll,
        text="【常用】干跑 / 多 IP 并行 / 并行上限 / 代理 → 总览可改；点「按性能推荐」可按 CPU/内存估算",
        font=fonts.get("small"),
        text_color=colors.get("accent", "#2F6F5E"),
        anchor="w",
    )
    tip.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 4))

    # 强化「并行 IP 上限」旁注，并在下一行加推荐按钮
    row, lab = _find_labeled_row(scroll, "并行 IP 上限")
    if row is not None:
        try:
            for child in list(scroll.winfo_children()):
                info = child.grid_info() or {}
                if int(info.get("row", -1)) == row and int(info.get("column", -1)) == 2:
                    child.configure(text="0=不限制；也可在总览点「按性能推荐」")
                    break
        except Exception:
            pass
        try:
            _shift_grid_from_row(scroll, row + 1, 1)
            rec_row = ctk.CTkFrame(scroll, fg_color="transparent")
            rec_row.grid(row=row + 1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))
            ctk.CTkButton(
                rec_row,
                text="按电脑性能推荐并行数",
                width=180,
                height=32,
                fg_color=colors.get("surface2", "#24302C"),
                hover_color=colors.get("accent_hover", "#3D8B75"),
                command=lambda: _apply_parallel_recommend(app, ask=True),
            ).pack(side="left")
            ctk.CTkLabel(
                rec_row,
                text="按 CPU/内存估算，并受当前 IP 组数封顶",
                font=fonts.get("small"),
                text_color=colors.get("muted", "#888"),
                anchor="w",
            ).pack(side="left", padx=(10, 0))
        except Exception as e:
            logger.debug("设置页推荐按钮未加上: %s", e)


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

        # 总览：常用设置 + 按钮重排（settings 已建好，可共用变量）
        try:
            _enhance_overview_ux(self, ctk, ui_app)
        except Exception as e:
            logger.warning("总览 UX 增强失败: %s", e)
        try:
            _promote_settings_common(self, ctk, ui_app)
        except Exception as e:
            logger.warning("设置页常用前置失败: %s", e)
        try:
            # 把当前 config 同步到快捷控件
            self.load_settings_form()
        except Exception:
            pass
        try:
            _sync_overview_quick_from_settings(self)
        except Exception:
            pass

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
        port_var = ctk.StringVar(value=_DEFAULT_PROXY_PORT)
        port_entry = ctk.CTkEntry(scroll, textvariable=port_var, height=34)
        port_entry.grid(row=rr, column=1, sticky="ew", padx=8, pady=6)
        self._set_vars["proxy_port"] = port_var
        ctk.CTkLabel(
            scroll,
            text=f"如 {_DEFAULT_PROXY_PORT}，留空=不走代理",
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
                cfg = {}
        content = (cfg or {}).get("content") or {}
        pub = (cfg or {}).get("publish") or {}
        poll = (cfg or {}).get("poll") or {}
        vars_ = _ensure_set_vars(self)
        if "product_display_name" in vars_:
            vars_["product_display_name"].set(
                pub.get("product_display_name") or "小黄车下单"
            )
        # 并行上限（确保总览/设置都能读到）
        if "max_parallel_ips" not in vars_:
            import customtkinter as ctk

            vars_["max_parallel_ips"] = ctk.StringVar(
                value=str(poll.get("max_parallel_ips") if poll.get("max_parallel_ips") is not None else 0)
            )
        else:
            _set_form_value(
                vars_["max_parallel_ips"],
                str(poll.get("max_parallel_ips") if poll.get("max_parallel_ips") is not None else 0),
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
        if "proxy_port" in vars_:
            _set_form_value(vars_["proxy_port"], port or "")
        # 同步完整代理输入框
        if proxy and "http_proxy" in vars_:
            try:
                _set_form_value(vars_["http_proxy"], proxy)
                _set_form_value(
                    vars_["https_proxy"],
                    os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or proxy,
                )
            except Exception:
                pass
        try:
            _sync_overview_quick_from_settings(self)
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

            messagebox.showinfo(
                "已保存",
                "设置已写入 config.yaml 与 .env（含多IP并行/并行上限/代理端口）",
            )
            try:
                self.refresh_overview()
            except Exception:
                pass
            try:
                _sync_overview_quick_from_settings(self)
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

    # 代理/SOCKS 打不开上传页、或跳到登录页：不写长冷却，立刻换下一号
    try:
        from fix_discard_draft import (
            AccountLoginRequired,
            _is_login_required_error,
            _is_proxy_net_error,
        )

        original_mark = publisher.PublisherService._mark_cooldowns
        current_rfa = publisher.PublisherService.run_for_account

        def mark_cooldowns_fixed(self, *a, **k):
            import sys

            exc = sys.exc_info()[1]
            if exc is not None and (
                _is_proxy_net_error(exc) or _is_login_required_error(exc)
            ):
                why = (
                    "登录失效"
                    if _is_login_required_error(exc)
                    else "代理/网络失败"
                )
                logger.warning(
                    "%s，跳过写入账号与 IP 组冷却（可马上重试下一号）: %s",
                    why,
                    str(exc).split("\n", 1)[0][:160],
                )
                return None
            if getattr(self, "_skip_next_cooldown", False):
                self._skip_next_cooldown = False
                logger.warning("已跳过本次冷却写入（代理/网络/登录跳过标记）")
                return None
            return original_mark(self, *a, **k)

        def run_for_account_proxy_fixed(self, account, count=None, **kwargs):
            try:
                return current_rfa(self, account, count=count, **kwargs)
            except Exception as e:
                label = (
                    (account or {}).get("name")
                    or (account or {}).get("username")
                    or (account or {}).get("profile_id")
                    or "?"
                )
                if _is_login_required_error(e) or isinstance(e, AccountLoginRequired):
                    self._skip_next_cooldown = True
                    logger.error(
                        "账号 %s 跳转到 TikTok 登录页，本轮跳过自动化（不写长冷却）: %s",
                        label,
                        str(e).split("\n", 1)[0][:180],
                    )
                elif _is_proxy_net_error(e):
                    self._skip_next_cooldown = True
                    logger.error(
                        "账号 %s 代理/网络打不开 TikTok，本轮跳过（不写长冷却）: %s",
                        label,
                        str(e).split("\n", 1)[0][:180],
                    )
                raise

        publisher.PublisherService._mark_cooldowns = mark_cooldowns_fixed  # type: ignore
        publisher.PublisherService.run_for_account = run_for_account_proxy_fixed  # type: ignore
        logger.info("已应用补丁：代理/SOCKS/登录页失败跳过账号且不写长冷却")
    except Exception as e:
        logger.warning("代理/登录跳过冷却补丁未生效: %s", e)

    def _start_clear_drafts_method(self):
        _start_clear_drafts(self)

    def _clear_cooldown_cache_method(self):
        _clear_cooldown_cache(self)

    def _open_deleted_videos_log_method(self):
        _open_deleted_videos_log(self)

    App.__init__ = __init_fixed  # type: ignore[method-assign]
    App._build_settings = _build_settings_fixed  # type: ignore[method-assign]
    App.load_settings_form = load_settings_form_fixed  # type: ignore[method-assign]
    App._save_settings = _save_settings_fixed  # type: ignore[method-assign]
    App._stop_job = _stop_job_fixed  # type: ignore[method-assign]
    App._start_clear_drafts = _start_clear_drafts_method  # type: ignore[attr-defined]
    App._clear_cooldown_cache = _clear_cooldown_cache_method  # type: ignore[attr-defined]
    App._open_deleted_videos_log = _open_deleted_videos_log_method  # type: ignore[attr-defined]
    ui_services.save_settings_from_ui = save_settings_from_ui_fixed  # type: ignore[assignment]
    ui_workers.JobRunner.request_stop = request_stop_fixed  # type: ignore[method-assign]
    publisher.PublisherService._interruptible_sleep = interruptible_sleep_fixed  # type: ignore[method-assign]

    # 原版 submit 已是 daemon，不再全局替换 threading.Thread
    logger.info(
        "已应用补丁：总览快捷设置 + 清理草稿/冷却缓存/删除视频日志 + 按性能推荐并行IP + 强制快速终止"
    )
