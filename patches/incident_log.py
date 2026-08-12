"""突发失败记录：单账号全自动流程失败时落盘，不中断后续账号。

写入：
  logs/incidents.jsonl  — 结构化（UI 读取）
  logs/incidents.txt    — 人类可读摘要
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("publisher")

_LOCK = threading.Lock()
_CTX = threading.local()


def _root() -> Path:
    try:
        import config_loader

        r = getattr(config_loader, "ROOT", None)
        if r:
            return Path(r)
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent


def incidents_jsonl_path() -> Path:
    return _root() / "logs" / "incidents.jsonl"


def incidents_txt_path() -> Path:
    return _root() / "logs" / "incidents.txt"


def set_account_context(account: dict | None) -> None:
    """在 run_for_account 入口设置当前账号上下文，供嵌套失败记录使用。"""
    acc = account or {}
    _CTX.account = {
        "account": acc.get("name") or acc.get("username") or "",
        "profile_id": acc.get("profile_id") or "",
        "ip_group": acc.get("proxy_group") or acc.get("_poll_group") or "",
        "proxy_hint": acc.get("proxy_hint") or acc.get("lastIp") or acc.get("last_ip") or "",
    }


def clear_account_context() -> None:
    _CTX.account = None


def get_account_context() -> dict[str, str]:
    return dict(getattr(_CTX, "account", None) or {})


def _lookup_product_name(product_id: str) -> str:
    if not product_id:
        return ""
    try:
        from ui.services import load_mapping_rows

        for row in load_mapping_rows() or []:
            if str(row.get("product_id") or "") == str(product_id):
                return str(row.get("product_name") or "")
    except Exception:
        pass
    try:
        import config_loader

        root = _root()
        cfg = config_loader.load_config(root / "config.yaml")
        map_file = root / cfg["paths"]["mapping_file"]
        if not map_file.is_file():
            return ""
        text = map_file.read_text(encoding="utf-8")
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get("products") or data.get("rows") or []
        for row in rows:
            if str(row.get("product_id") or row.get("id") or "") == str(product_id):
                return str(row.get("product_name") or row.get("name") or "")
    except Exception:
        pass
    return ""


def record_incident(
    *,
    account: str = "",
    profile_id: str = "",
    ip_group: str = "",
    proxy_hint: str = "",
    product_id: str = "",
    product_name: str = "",
    video: str = "",
    status: str = "",
    message: str = "",
    dry_run: bool | None = None,
    stage: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """追加一条突发失败记录；永不抛出（避免影响主流程）。"""
    ctx = get_account_context()
    pid = str(product_id or "")
    # 优先 Excel 映射品名；没有再退回传入值（可能是小黄车展示名）
    pname = _lookup_product_name(pid) or str(product_name or "")
    rec: dict[str, Any] = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "account": str(account or ctx.get("account") or ""),
        "profile_id": str(profile_id or ctx.get("profile_id") or ""),
        "ip_group": str(ip_group or ctx.get("ip_group") or ""),
        "proxy_hint": str(proxy_hint or ctx.get("proxy_hint") or ""),
        "product_id": pid,
        "product_name": pname,
        "video": str(video or ""),
        "status": str(status or "error"),
        "message": str(message or "")[:500],
        "stage": str(stage or ""),
    }
    if dry_run is not None:
        rec["dry_run"] = bool(dry_run)
    if extra:
        for k, v in extra.items():
            if k not in rec and v is not None:
                rec[k] = v

    try:
        log_dir = _root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        jpath = incidents_jsonl_path()
        tpath = incidents_txt_path()
        line = json.dumps(rec, ensure_ascii=False)
        human = (
            f"[{rec['ts']}] 账号={rec['account'] or '-'} IP={rec['ip_group'] or '-'} "
            f"代理={rec['proxy_hint'] or '-'} 商品ID={rec['product_id'] or '-'} "
            f"品名={rec['product_name'] or '-'} 视频={rec['video'] or '-'} "
            f"状态={rec['status']} 阶段={rec['stage'] or '-'} "
            f"原因={rec['message']}"
        )
        with _LOCK:
            with jpath.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            with tpath.open("a", encoding="utf-8") as f:
                f.write(human + "\n")
        logger.warning("[突发失败] %s", human)
    except Exception as e:
        logger.debug("写入突发失败日志失败: %s", e)
    return rec


def read_incidents(limit: int = 200) -> list[dict[str, Any]]:
    """读取最近 limit 条（新→旧）。"""
    path = incidents_jsonl_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        s = (line or "").strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
        if len(out) >= max(1, int(limit or 200)):
            break
    return out
