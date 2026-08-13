"""并发退化为单号串行的三处修复。

1. 保存设置会把「多 IP 并行」静默写成 false。
   总览「保存快捷设置」最终调的是设置页的 _save_settings，而那里读的是设置页
   控件 self._set_parallel。新机器上用户往往没进过设置页，控件不存在，
   payload 里就写死 parallel_ips=False，于是总览开关明明是开的，配置却是关的。
2. 同步窗口时比特 detail 被 429 限流，拿不到 lastIp/代理配置的号会全部落进
   同一个分组键，IP 组数塌成 1，并发度跟着变 1。补一轮串行重试。
3. 每轮开跑前打印并发画像，串行时直接说清是哪一条原因。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger("fix_parallel_group")


def _progress(msg: str) -> None:
    print(f"[并发] {msg}", flush=True)
    logger.info(msg)


class _BoolFlag:
    """设置页控件的替身，只在控件还没建出来时顶上。

    没有它，hasattr(app, '_set_parallel') 为假 → 保存时写死 False。
    """

    def __init__(self, value: bool) -> None:
        self._v = bool(value)

    def get(self) -> int:
        return 1 if self._v else 0

    def select(self) -> None:
        self._v = True

    def deselect(self) -> None:
        self._v = False


def _cfg_bool(section: str, key: str, default: bool) -> bool:
    try:
        from ui import services as ui_services

        cfg = ui_services.load_config_raw() or {}
        val = (cfg.get(section) or {}).get(key)
    except Exception:
        return default
    return default if val is None else bool(val)


def _ensure_flag_widgets(app) -> None:
    """补齐 _set_parallel / _set_dry，取值优先总览开关，其次当前配置。"""
    ov = getattr(app, "_ov_quick", None) or {}
    for attr, ov_key, section, key, default in (
        ("_set_parallel", "parallel", "poll", "parallel_ips", True),
        ("_set_dry", "dry", "publish", "dry_run", False),
    ):
        if hasattr(app, attr):
            continue
        val = None
        if ov_key in ov:
            try:
                val = bool(int(ov[ov_key].get()))
            except Exception:
                val = None
        if val is None:
            val = _cfg_bool(section, key, default)
        setattr(app, attr, _BoolFlag(val))
        logger.info("设置页控件 %s 不存在，按当前值 %s 兜底，避免保存时被写成 false", attr, val)


def _group_stats() -> tuple[int, dict[str, int]]:
    """读 accounts.yaml 统计启用账号数与 IP 组分布。"""
    import yaml

    try:
        import config_loader

        root = Path(getattr(config_loader, "ROOT", ".") or ".")
    except Exception:
        root = Path(".")
    data = yaml.safe_load((root / "data" / "accounts.yaml").read_text("utf-8")) or {}
    groups: dict[str, int] = {}
    total = 0
    for row in data.get("accounts") or []:
        if not (row or {}).get("enabled", True):
            continue
        total += 1
        name = str((row or {}).get("proxy_group") or "(未分组)").strip() or "(未分组)"
        groups[name] = groups.get(name, 0) + 1
    return total, groups


def apply_parallel_group_patches() -> None:
    _patch_save_settings()
    _patch_profile_enrich()
    _patch_sync_report()
    _patch_round_report()
    logger.info("已应用补丁：并发开关保存 / 分组重试 / 并发画像")


def _patch_save_settings() -> None:
    try:
        from ui import app as ui_app

        App = ui_app.App
        original = App._save_settings

        def save_settings_keep_flags(self, *a, **k):
            try:
                _ensure_flag_widgets(self)
            except Exception as e:
                logger.warning("补齐并行开关失败: %s", e)
            return original(self, *a, **k)

        App._save_settings = save_settings_keep_flags  # type: ignore[method-assign]
    except Exception as e:
        logger.exception("接管 _save_settings 失败: %s", e)


def _patch_profile_enrich() -> None:
    """detail 被限流失败的窗口，串行重试一次，避免全部塌成一组。"""
    try:
        import bitbrowser

        original = bitbrowser.BitBrowser.list_all_profiles_enriched

        def enriched_with_retry(self):
            rows = original(self) or []
            try:
                bad = [r for r in rows if not bitbrowser._has_enough_proxy_info(r)]
                if not bad:
                    return rows
                _progress(f"{len(bad)}/{len(rows)} 个窗口没读到出口 IP/代理，串行重试 detail…")
                fixed = 0
                for row in bad:
                    pid = row.get("id") or row.get("browserId")
                    if not pid:
                        continue
                    for attempt in range(3):
                        try:
                            detail = self.detail(pid)
                            if isinstance(detail, dict) and detail:
                                row.update(detail)
                            break
                        except Exception as e:
                            # 比特本地 API 并发高了会 429，退避重试
                            time.sleep(0.5 * (attempt + 1))
                            logger.debug("detail 重试 %s 失败: %s", pid, e)
                    if bitbrowser._has_enough_proxy_info(row):
                        key, hint = bitbrowser.make_proxy_group_key(row)
                        row["_proxy_key"] = key
                        row["_proxy_hint"] = hint
                        fixed += 1
                    time.sleep(0.15)
                _progress(f"重试后补回 {fixed}/{len(bad)} 个窗口的代理信息")
            except Exception as e:
                logger.warning("补齐窗口代理信息失败: %s", e)
            return rows

        bitbrowser.BitBrowser.list_all_profiles_enriched = enriched_with_retry  # type: ignore[method-assign]
    except Exception as e:
        logger.exception("接管 list_all_profiles_enriched 失败: %s", e)


def _patch_sync_report() -> None:
    """同步完打印分组结果；塌成一组时说清后果和改法。"""
    try:
        import accounts

        original = accounts.sync_accounts_from_bitbrowser

        def sync_reported(*a, **k):
            res = original(*a, **k)
            try:
                groups = (res or {}).get("groups")
                total = int((res or {}).get("total") or 0)
                if isinstance(groups, dict) and groups:
                    _progress(
                        "分组结果：" + "、".join(f"{g}({c}个)" for g, c in groups.items())
                    )
                    count = len(groups)
                else:
                    count = int(groups or 0)
                if count <= 1 and total > 1:
                    _progress(
                        "注意：所有账号被分进了同一个 IP 组，发布只能串行。"
                        "多半是比特没返回 lastIp / 代理配置（窗口从没开过，或本地 API 被限流）。"
                        "可以先手动开一次窗口再同步，或直接改 data/accounts.yaml 里的 proxy_group 拆开。"
                    )
            except Exception as e:
                logger.debug("同步分组统计失败: %s", e)
            return res

        accounts.sync_accounts_from_bitbrowser = sync_reported  # type: ignore[assignment]
    except Exception as e:
        logger.exception("接管 sync_accounts_from_bitbrowser 失败: %s", e)


def _patch_round_report() -> None:
    """每轮开跑前把并发情况说清楚，省得只能靠猜。"""
    try:
        import publisher

        original = publisher.PublisherService.run_round

        def run_round_reported(self, *a, **k):
            try:
                total, groups = _group_stats()
                parallel = bool(getattr(self, "parallel_ips", False))
                cap = int(getattr(self, "max_parallel_ips", 0) or 0)
                gcount = len(groups)
                if not parallel:
                    effective = 1
                elif cap > 0:
                    effective = min(cap, gcount)
                else:
                    effective = gcount
                _progress(
                    f"多 IP 并行={'开' if parallel else '关'} | 启用账号={total} | "
                    f"IP 组={gcount}（{'、'.join(f'{g}:{c}' for g, c in groups.items()) or '无'}）| "
                    f"并行上限={cap or '不限'} → 本轮同时跑 {effective} 组"
                )
                if effective <= 1 and total > 1:
                    if not parallel:
                        _progress(
                            "并发=1 的原因：config.yaml 的 poll.parallel_ips 是 false。"
                            "到总览「快捷设置」勾上「多 IP 并行」再点保存。"
                        )
                    elif gcount <= 1:
                        _progress(
                            "并发=1 的原因：所有账号在同一个 IP 组。同组共用出口 IP 必须串行，"
                            "要并发就去「账号」页重新同步分组，或手改 data/accounts.yaml 的 proxy_group。"
                        )
                    elif cap == 1:
                        _progress("并发=1 的原因：并行上限被设成了 1，改成 2 即可两组同时跑。")
            except Exception as e:
                logger.debug("并发画像失败: %s", e)
            return original(self, *a, **k)

        publisher.PublisherService.run_round = run_round_reported  # type: ignore[method-assign]
    except Exception as e:
        logger.exception("接管 run_round 失败: %s", e)
