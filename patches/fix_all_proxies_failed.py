"""全部比特窗口代理失败时：立刻停任务，并弹出换节点提示。"""

from __future__ import annotations

import logging

logger = logging.getLogger("publisher")

ALL_PROXIES_FAILED_HINT = (
    "请尝试更换加速器/梯子的节点，如若失败，请继续切换新的节点重试"
)


class AllProxiesFailedError(RuntimeError):
    """本轮全部账号窗口代理不可用。"""

    def __init__(self, message: str | None = None):
        super().__init__(message or ALL_PROXIES_FAILED_HINT)


def is_all_proxies_failed_message(msg: str) -> bool:
    s = str(msg or "")
    return ALL_PROXIES_FAILED_HINT in s or "全部账号的窗口代理都不可用" in s


def _tracker(svc) -> dict:
    t = getattr(svc, "_proxy_fail_tracker", None)
    if not isinstance(t, dict):
        t = {
            "total_scheduled": 0,
            "attempted": 0,
            "proxy_fails": 0,
            "had_success": False,
            "abort": False,
        }
        svc._proxy_fail_tracker = t
    return t


def _should_abort(t: dict) -> bool:
    if t.get("had_success"):
        return False
    if t.get("abort"):
        return True
    proxy_fails = int(t.get("proxy_fails") or 0)
    attempted = int(t.get("attempted") or 0)
    total = int(t.get("total_scheduled") or 0)
    if proxy_fails <= 0:
        return False
    # 本轮调度的号都已因代理失败
    if total > 0 and proxy_fails >= total:
        return True
    # 或已尝试的全部失败，且尝试数已覆盖调度数
    if attempted > 0 and proxy_fails >= attempted and total > 0 and attempted >= total:
        return True
    return False


def _abort_now(svc, cause: BaseException | None = None) -> None:
    t = _tracker(svc)
    t["abort"] = True
    try:
        svc.cancel_event.set()
    except Exception:
        pass
    logger.error("全部比特浏览器代理失败，立刻停止：%s", ALL_PROXIES_FAILED_HINT)
    if cause is not None:
        raise AllProxiesFailedError() from cause
    raise AllProxiesFailedError()


def apply_all_proxies_failed_patches() -> None:
    import publisher
    from fix_discard_draft import _is_proxy_net_error

    original_filter = publisher.PublisherService._filter_by_proxy_health
    original_round = publisher.PublisherService.run_round
    original_ip_group = publisher.PublisherService._run_ip_group
    current_rfa = publisher.PublisherService.run_for_account

    def filter_fixed(self, accounts):
        try:
            out = original_filter(self, accounts)
        except RuntimeError as e:
            msg = str(e)
            if ("全部账号" in msg and "不可用" in msg) or is_all_proxies_failed_message(msg):
                _tracker(self)["abort"] = True
                try:
                    self.cancel_event.set()
                except Exception:
                    pass
                raise AllProxiesFailedError() from e
            raise
        t = _tracker(self)
        t["total_scheduled"] = len(out or [])
        t["attempted"] = 0
        t["proxy_fails"] = 0
        t["had_success"] = False
        t["abort"] = False
        return out

    def rfa_fixed(self, account, count=None, **kwargs):
        t = _tracker(self)
        if t.get("abort"):
            raise AllProxiesFailedError()
        try:
            result = current_rfa(self, account, count=count, **kwargs)
            t["attempted"] = int(t.get("attempted") or 0) + 1
            t["had_success"] = True
            return result
        except AllProxiesFailedError:
            t["abort"] = True
            try:
                self.cancel_event.set()
            except Exception:
                pass
            raise
        except Exception as e:
            if _is_proxy_net_error(e):
                t["attempted"] = int(t.get("attempted") or 0) + 1
                t["proxy_fails"] = int(t.get("proxy_fails") or 0) + 1
                label = (
                    (account or {}).get("name")
                    or (account or {}).get("username")
                    or (account or {}).get("profile_id")
                    or "?"
                )
                logger.error(
                    "账号 %s 代理失败（%s/%s）: %s",
                    label,
                    t["proxy_fails"],
                    t.get("total_scheduled") or "?",
                    str(e).split("\n", 1)[0][:160],
                )
                if _should_abort(t):
                    _abort_now(self, e)
            # 登录页跳过：不计代理失败，也不中断整轮
            raise

    def run_ip_group_fixed(self, *a, **k):
        try:
            return original_ip_group(self, *a, **k)
        except AllProxiesFailedError:
            raise
        finally:
            t = getattr(self, "_proxy_fail_tracker", None)
            if isinstance(t, dict) and _should_abort(t):
                t["abort"] = True
                try:
                    self.cancel_event.set()
                except Exception:
                    pass

    def run_round_fixed(self, *a, **k):
        self._proxy_fail_tracker = {
            "total_scheduled": 0,
            "attempted": 0,
            "proxy_fails": 0,
            "had_success": False,
            "abort": False,
        }
        try:
            result = original_round(self, *a, **k)
        except AllProxiesFailedError:
            try:
                self.cancel_event.set()
            except Exception:
                pass
            raise
        t = _tracker(self)
        if _should_abort(t):
            _abort_now(self)
        # 从 summary 再兜一层（组内吞掉异常时）
        try:
            rows = list(result or [])
        except Exception:
            rows = []
        if rows and not t.get("had_success"):
            proxy_like = 0
            for row in rows:
                msg = ""
                try:
                    if isinstance(row, dict):
                        msg = str(row.get("message") or "")
                        ok = bool(row.get("ok"))
                    else:
                        msg = str(getattr(row, "message", "") or "")
                        ok = bool(getattr(row, "ok", False))
                except Exception:
                    ok = False
                    msg = ""
                if ok:
                    t["had_success"] = True
                    break
                if _is_proxy_net_error(Exception(msg)) or "PROXY_NETWORK_FAIL" in msg:
                    proxy_like += 1
            if (
                not t.get("had_success")
                and proxy_like > 0
                and proxy_like >= len(rows)
            ):
                _abort_now(self)
        return result

    publisher.PublisherService._filter_by_proxy_health = filter_fixed  # type: ignore[method-assign]
    publisher.PublisherService.run_for_account = rfa_fixed  # type: ignore[method-assign]
    publisher.PublisherService._run_ip_group = run_ip_group_fixed  # type: ignore[method-assign]
    publisher.PublisherService.run_round = run_round_fixed  # type: ignore[method-assign]

    # UI：任务失败弹窗改为明确的代理修复提示
    try:
        from ui import app as ui_app

        App = ui_app.App
        original_poll = App._poll_worker

        def poll_worker_fixed(self):
            import tkinter.messagebox as mb

            orig_error = mb.showerror
            orig_warning = mb.showwarning

            def showerror(title, message=None, **kwargs):
                text = message if message is not None else title
                if is_all_proxies_failed_message(str(text)):
                    return orig_warning(
                        "代理修复提示",
                        ALL_PROXIES_FAILED_HINT,
                        **kwargs,
                    )
                return orig_error(title, message, **kwargs)

            mb.showerror = showerror  # type: ignore[assignment]
            try:
                return original_poll(self)
            finally:
                mb.showerror = orig_error  # type: ignore[assignment]

        App._poll_worker = poll_worker_fixed  # type: ignore[method-assign]
    except Exception as e:
        logger.warning("代理失败弹窗 UI 补丁未生效: %s", e)

    logger.info("已应用补丁：全部代理失败立刻停止并提示更换节点")
