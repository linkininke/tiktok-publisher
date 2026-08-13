"""数据看板：按天看每个账号发了几条、发了什么、哪些号还没发。

纯前端功能：只读 data/publish_state.json 和 data/accounts.yaml（都用项目现成的
加载函数），不碰 publisher / uploader 的任何流程，也不写回任何文件。

一个容易搞错的口径：state 里的 published 记录用 status 区分三种结果，而 ok 字段
把干跑也算成 True（ok=True 里既有 status=pass 也有 status=dry_run）。看板必须
把「真实发布」和「干跑」分开数，否则试跑一轮就会让今日条数虚高一倍。

「比特浏览器备注」取 accounts.yaml 里的 name —— 那就是同步比特窗口时抓下来的
备注名，所以看板不需要连比特 API，比特没开也能看。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("publisher")

NAV_KEY = "dashboard"
NAV_LABEL = "看板"

# 明细最多渲染多少行。CTk 每行都是真控件，几千行会把界面卡住
DETAIL_LIMIT = 300
# 看板可见时的自动刷新间隔（毫秒）
AUTO_REFRESH_MS = 20000

RANGES = ("今天", "昨天", "近 7 天")

KIND_OK = "ok"
KIND_DRY = "dry"
KIND_ERR = "err"
KIND_TEXT = {KIND_OK: "已发布", KIND_DRY: "干跑", KIND_ERR: "失败"}


# ---------------- 数据（与 UI 无关，便于单独验证） ----------------


def _classify(rec: dict) -> str:
    """把一条 published 记录归类。

    status=pass 才是真发出去了；dry_run 是「上传/文案/挂车都做了但没点发布」，
    不能算业绩；其余（error 或缺字段）按失败。
    """
    status = str(rec.get("status") or "").strip().lower()
    if status == "dry_run":
        return KIND_DRY
    if status == "error":
        return KIND_ERR
    return KIND_OK if rec.get("ok") else KIND_ERR


def _parse_time(val) -> datetime | None:
    s = str(val or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # 兼容早期可能写成 "2026-08-11 16:22:32" 的记录
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def range_bounds(label: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """把「今天/昨天/近 7 天」换成 [起, 止) 两个本地时间点。

    state 里的 time 是不带时区的本地时间，所以这里也全用 naive datetime 比较。
    """
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if label == "昨天":
        return today - timedelta(days=1), today
    if label == "近 7 天":
        return today - timedelta(days=6), today + timedelta(days=1)
    return today, today + timedelta(days=1)


def load_records() -> list[dict]:
    """读 state 里的 published，转成看板用的行。读不到就返回空列表。"""
    try:
        import config_loader
        from ui.services import load_config_raw

        cfg = load_config_raw() or {}
        state_file = (cfg.get("paths") or {}).get("state_file") or "data/publish_state.json"
        state = config_loader.load_state(state_file) or {}
    except Exception as e:
        logger.warning("看板读取发布记录失败: %s", e)
        return []

    rows = []
    for rec in state.get("published") or []:
        if not isinstance(rec, dict):
            continue
        dt = _parse_time(rec.get("time"))
        if dt is None:
            continue
        rows.append(
            {
                "dt": dt,
                "profile_id": str(rec.get("account") or ""),
                "product_id": str(rec.get("product_id") or ""),
                "product_name": str(rec.get("product_name") or ""),
                "video": str(rec.get("video") or ""),
                "kind": _classify(rec),
                "message": str(rec.get("message") or ""),
            }
        )
    rows.sort(key=lambda r: r["dt"], reverse=True)
    return rows


def load_accounts() -> list[dict]:
    """账号表（含比特备注名）。比特没开也能读，因为这是同步时落盘的。"""
    try:
        from ui.services import list_accounts_ui

        return [dict(a) for a in (list_accounts_ui() or []) if isinstance(a, dict)]
    except Exception as e:
        logger.warning("看板读取账号表失败: %s", e)
        return []


def build_view(label: str, now: datetime | None = None) -> dict:
    """汇总出看板要显示的一切。"""
    start, end = range_bounds(label, now)
    accounts = load_accounts()
    records = [r for r in load_records() if start <= r["dt"] < end]

    by_pid: dict[str, dict] = {}
    for a in accounts:
        pid = str(a.get("profile_id") or "")
        if not pid:
            continue
        by_pid[pid] = {
            "profile_id": pid,
            "name": str(a.get("name") or "").strip() or ("未命名 " + pid[:8]),
            "group": str(a.get("proxy_group") or "-"),
            "enabled": bool(a.get("enabled", True)),
            "known": True,
            "ok": 0,
            "dry": 0,
            "err": 0,
            "last": None,
        }

    for r in records:
        pid = r["profile_id"]
        row = by_pid.get(pid)
        if row is None:
            # 账号表里没有：比特窗口被删了，或记录早于最近一次同步
            row = by_pid[pid] = {
                "profile_id": pid,
                "name": "已移除 " + (pid[:8] or "?"),
                "group": "-",
                "enabled": False,
                "known": False,
                "ok": 0,
                "dry": 0,
                "err": 0,
                "last": None,
            }
        row[r["kind"]] += 1
        if r["kind"] == KIND_OK and (row["last"] is None or r["dt"] > row["last"]):
            row["last"] = r["dt"]

    rows = list(by_pid.values())
    # 没发的排前面——用户看这页主要就是想知道该补哪些号
    rows.sort(key=lambda x: (x["ok"] > 0, x["group"], x["name"].lower()))
    posted = [x for x in rows if x["ok"] > 0]
    # 停用的号本来就不参与轮询，算进「还没发」会误导
    pending = [x for x in rows if x["ok"] == 0 and x["enabled"]]
    disabled = [x for x in rows if x["ok"] == 0 and not x["enabled"]]

    return {
        "label": label,
        "start": start,
        "end": end,
        "accounts_total": len([a for a in accounts if a.get("enabled", True)]),
        "rows": rows,
        "posted": posted,
        "pending": pending,
        "disabled": disabled,
        "details": records,
        "total_ok": sum(x["ok"] for x in rows),
        "total_dry": sum(x["dry"] for x in rows),
        "total_err": sum(x["err"] for x in rows),
    }


def summary_text(view: dict) -> str:
    n_enabled = view["accounts_total"]
    line1 = "%s：真实发布 %d 条 · 已发账号 %d/%d · 还没发 %d 个" % (
        view["label"],
        view["total_ok"],
        len(view["posted"]),
        n_enabled,
        len(view["pending"]),
    )
    bits = []
    if view["total_dry"]:
        bits.append("干跑 %d 条（未点发布，不计入发布量）" % view["total_dry"])
    if view["total_err"]:
        bits.append("失败 %d 条" % view["total_err"])
    if view["disabled"]:
        bits.append("已停用 %d 个账号未统计在「还没发」内" % len(view["disabled"]))
    return line1 + ("\n" + " · ".join(bits) if bits else "")


# ---------------- UI ----------------


def apply_dashboard_patches() -> None:
    import customtkinter as ctk
    from ui import app as uiapp

    COLORS = uiapp.COLORS
    FONTS = uiapp.FONTS
    App = uiapp.App

    # fix_incident_log 会把 NAV_ITEMS 换成 tuple，所以这里重建后整体赋值，
    # 不能直接 .insert()——那样在它之后加载就会炸。
    items = list(getattr(uiapp, "NAV_ITEMS", ()) or ())
    if not any(k == NAV_KEY for k, _ in items):
        # 放在「总览」后面：这页是看结果的，紧跟总览最顺手
        idx = next((i for i, (k, _) in enumerate(items) if k == "overview"), -1)
        items.insert(idx + 1, (NAV_KEY, NAV_LABEL))
        uiapp.NAV_ITEMS = tuple(items)

    def _kind_color(kind: str) -> str:
        return {KIND_OK: COLORS["ok"], KIND_DRY: COLORS["warn"]}.get(kind, COLORS["danger"])

    def _row_frame(parent):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", pady=2)
        return f

    def _build_dashboard(self) -> None:
        f = self._page_frame(NAV_KEY)
        self._header(
            f,
            "数据看板",
            "按天看每个账号发了几条、发的是哪条视频，以及哪些账号还没发",
        )

        body = ctk.CTkFrame(f, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 16))
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=4)
        body.grid_rowconfigure(2, weight=1)

        bar = ctk.CTkFrame(body, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._dash_range = ctk.CTkSegmentedButton(
            bar,
            values=list(RANGES),
            font=FONTS["body"],
            selected_color=COLORS["accent"],
            selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["surface2"],
            text_color=COLORS["text"],
            command=lambda _v: self.refresh_dashboard(),
        )
        self._dash_range.set(RANGES[0])
        self._dash_range.pack(side="left")
        ctk.CTkButton(
            bar,
            text="刷新",
            width=80,
            font=FONTS["body"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color="#0B1210",
            command=self.refresh_dashboard,
        ).pack(side="left", padx=12)
        self._dash_summary = ctk.CTkLabel(
            bar,
            text="加载中…",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            justify="left",
            anchor="w",
        )
        self._dash_summary.pack(side="left", padx=12)

        ctk.CTkLabel(body, text="账号维度", font=FONTS["h2"]).grid(
            row=1, column=0, sticky="w", pady=(0, 6)
        )
        ctk.CTkLabel(body, text="视频明细", font=FONTS["h2"]).grid(
            row=1, column=1, sticky="w", padx=(12, 0), pady=(0, 6)
        )
        self._dash_acc_list = ctk.CTkScrollableFrame(
            body, fg_color=COLORS["surface"], corner_radius=12
        )
        self._dash_acc_list.grid(row=2, column=0, sticky="nsew", padx=(0, 6))
        self._dash_detail_list = ctk.CTkScrollableFrame(
            body, fg_color=COLORS["surface"], corner_radius=12
        )
        self._dash_detail_list.grid(row=2, column=1, sticky="nsew", padx=(6, 0))

        uiapp._tip(
            body,
            "说明：只有真实发布（点了发布并确认落地）才计入条数；干跑单独列出，不算发布量。"
            "账号名取比特窗口备注，来自「账号」页最近一次同步，比特没开也能查看。",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _dash_section(self, parent, title: str, color: str) -> None:
        ctk.CTkLabel(
            parent, text=title, font=FONTS["small"], text_color=color, anchor="w"
        ).pack(fill="x", pady=(10, 2))

    def _dash_account_row(self, parent, row: dict) -> None:
        line = _row_frame(parent)
        dot = COLORS["ok"] if row["ok"] else (COLORS["warn"] if row["dry"] else COLORS["muted"])
        ctk.CTkLabel(line, text="●", font=FONTS["small"], text_color=dot, width=16).pack(side="left")
        tag = "" if row["enabled"] else "（已停用）"
        ctk.CTkLabel(
            line,
            text="%s%s" % (row["name"], tag),
            font=FONTS["body"],
            text_color=COLORS["text"],
            width=170,
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            line, text=row["group"], font=FONTS["small"], text_color=COLORS["muted"], width=60, anchor="w"
        ).pack(side="left")
        counts = "发布 %d" % row["ok"]
        if row["dry"]:
            counts += " · 干跑 %d" % row["dry"]
        if row["err"]:
            counts += " · 失败 %d" % row["err"]
        ctk.CTkLabel(
            line,
            text=counts,
            font=FONTS["small"],
            text_color=COLORS["text"] if row["ok"] else COLORS["muted"],
            width=150,
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            line,
            text=row["last"].strftime("%m-%d %H:%M") if row["last"] else "—",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            anchor="w",
        ).pack(side="left")

    def _dash_detail_row(self, parent, rec: dict, name: str) -> None:
        line = _row_frame(parent)
        ctk.CTkLabel(
            line,
            text=rec["dt"].strftime("%m-%d %H:%M"),
            font=FONTS["small"],
            text_color=COLORS["muted"],
            width=86,
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            line, text=name, font=FONTS["small"], text_color=COLORS["text"], width=130, anchor="w"
        ).pack(side="left")
        ctk.CTkLabel(
            line,
            text=(rec["product_name"] or rec["product_id"] or "-")[:18],
            font=FONTS["small"],
            text_color=COLORS["muted"],
            width=150,
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            line,
            text=rec["video"] or "-",
            font=FONTS["small"],
            text_color=COLORS["text"],
            width=120,
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            line,
            text=KIND_TEXT.get(rec["kind"], "?"),
            font=FONTS["small"],
            text_color=_kind_color(rec["kind"]),
            width=60,
            anchor="w",
        ).pack(side="left")
        if rec["kind"] == KIND_ERR and rec["message"]:
            ctk.CTkLabel(
                line,
                text=rec["message"][:40],
                font=FONTS["small"],
                text_color=COLORS["danger"],
                anchor="w",
            ).pack(side="left")

    def refresh_dashboard(self) -> None:
        if not hasattr(self, "_dash_acc_list"):
            return
        for w in self._dash_acc_list.winfo_children():
            w.destroy()
        for w in self._dash_detail_list.winfo_children():
            w.destroy()

        label = RANGES[0]
        try:
            label = self._dash_range.get() or RANGES[0]
        except Exception:
            pass
        try:
            view = build_view(label)
        except Exception as e:
            logger.warning("看板汇总失败: %s", e)
            self._dash_summary.configure(text="读取发布记录失败：%s" % e)
            return

        self._dash_summary.configure(text=summary_text(view))
        names = {r["profile_id"]: r["name"] for r in view["rows"]}

        if view["pending"]:
            self._dash_section(
                self._dash_acc_list,
                "还没发（%d 个）" % len(view["pending"]),
                COLORS["warn"],
            )
            for row in view["pending"]:
                self._dash_account_row(self._dash_acc_list, row)
        if view["posted"]:
            self._dash_section(
                self._dash_acc_list,
                "已发布（%d 个）" % len(view["posted"]),
                COLORS["ok"],
            )
            for row in view["posted"]:
                self._dash_account_row(self._dash_acc_list, row)
        if view["disabled"]:
            self._dash_section(
                self._dash_acc_list,
                "已停用（%d 个，不参与轮询）" % len(view["disabled"]),
                COLORS["muted"],
            )
            for row in view["disabled"]:
                self._dash_account_row(self._dash_acc_list, row)
        if not view["rows"]:
            ctk.CTkLabel(
                self._dash_acc_list,
                text="账号表是空的，先去「账号」页同步比特窗口",
                font=FONTS["body"],
                text_color=COLORS["muted"],
            ).pack(anchor="w", pady=8)

        details = view["details"]
        for rec in details[:DETAIL_LIMIT]:
            self._dash_detail_row(
                self._dash_detail_list, rec, names.get(rec["profile_id"], rec["profile_id"][:8])
            )
        if not details:
            ctk.CTkLabel(
                self._dash_detail_list,
                text="%s 还没有任何发布记录" % view["label"],
                font=FONTS["body"],
                text_color=COLORS["muted"],
            ).pack(anchor="w", pady=8)
        elif len(details) > DETAIL_LIMIT:
            ctk.CTkLabel(
                self._dash_detail_list,
                text="仅显示最近 %d 条，共 %d 条" % (DETAIL_LIMIT, len(details)),
                font=FONTS["small"],
                text_color=COLORS["muted"],
            ).pack(anchor="w", pady=(8, 0))

    def _dash_tick(self) -> None:
        """看板可见时自动刷新，跟冷却页的定时刷新一个套路。"""
        try:
            if getattr(self, "_page", None) == NAV_KEY and self.winfo_exists():
                self.refresh_dashboard()
        except Exception:
            return
        try:
            self.after(AUTO_REFRESH_MS, lambda: _dash_tick(self))
        except Exception:
            pass

    App._build_dashboard = _build_dashboard
    App._dash_section = _dash_section
    App._dash_account_row = _dash_account_row
    App._dash_detail_row = _dash_detail_row
    App.refresh_dashboard = refresh_dashboard

    original_build_pages = App._build_pages

    def build_pages_fixed(self) -> None:
        original_build_pages(self)
        self._build_dashboard()
        try:
            self.after(AUTO_REFRESH_MS, lambda: _dash_tick(self))
        except Exception:
            pass

    original_show_page = App.show_page

    def show_page_fixed(self, key: str):
        result = original_show_page(self, key)
        if key == NAV_KEY:
            self.refresh_dashboard()
        return result

    App._build_pages = build_pages_fixed
    App.show_page = show_page_fixed
    print("[进度] 已加载数据看板页（只读发布记录，不改后端流程）")
