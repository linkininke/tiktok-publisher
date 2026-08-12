"""修复：真实发布在「网络预检 HTTP 200」后假死。

根因链：
1) _guard_network 在后台线程里调用 Tk after()，可能拖死后续
2) PublisherService.__init__ 里 setup_logging / DeepSeek 客户端可能卡住
3) 卡点只用了 print 的预检行，后面无进度

策略：重写 _guard_network + 包住 _start_poll 任务，每一步强制 print+写 progress 文件。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from pathlib import Path

logger = logging.getLogger("publisher")

_PRECHECK_BUDGET_SEC = 45
_DETAIL_TIMEOUT = 8
_CHECKAGENT_TIMEOUT = 10


def _root() -> Path:
    try:
        import config_loader

        r = getattr(config_loader, "ROOT", None)
        if r:
            return Path(r)
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent


def _progress(msg: str) -> None:
    line = f"[进度] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        logger.info("%s", msg)
    except Exception:
        pass
    try:
        p = _root() / "logs" / "progress.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + " " + line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def apply_precheck_hang_patches() -> None:
    import bitbrowser
    import config_loader
    import content_gen
    import proxy_health
    import publisher

    _progress("fix_precheck_hang 补丁已加载")

    # ---- setup_logging：禁止重复打开日志文件 ----
    original_setup = config_loader.setup_logging

    def setup_logging_safe(log_dir=None):
        root_logger = logging.getLogger()
        if root_logger.handlers:
            try:
                p = Path(log_dir) if log_dir else (_root() / "logs")
                p.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return None
        return original_setup(log_dir)

    config_loader.setup_logging = setup_logging_safe  # type: ignore[assignment]
    if hasattr(publisher, "setup_logging"):
        publisher.setup_logging = setup_logging_safe  # type: ignore[attr-defined]

    # ---- DeepSeek 懒加载 ----
    CG = content_gen.ContentGenerator
    original_generate = CG.generate

    def cg_init_lazy(self, *a, **k):
        _progress("读取 DeepSeek 配置（懒加载，初始化不连网）")
        try:
            from dotenv import load_dotenv

            load_dotenv(_root() / ".env", override=True)
            api_key = (a[0] if len(a) > 0 else k.get("api_key")) or os.getenv("OPENAI_API_KEY", "")
            base_url = (a[1] if len(a) > 1 else k.get("base_url")) or os.getenv(
                "OPENAI_BASE_URL", "https://api.deepseek.com"
            )
            model = (a[2] if len(a) > 2 else k.get("model")) or os.getenv(
                "OPENAI_MODEL", "deepseek-v4-flash"
            )
            hashtag_count = a[3] if len(a) > 3 else k.get("hashtag_count", 5)
            self.api_key = (api_key or "").strip()
            self.base_url = (base_url or "").strip().rstrip("/")
            self.model = (model or "").strip()
            self.hashtag_count = hashtag_count
            self._client = None
            _progress(f"DeepSeek 配置完成 model={self.model or '-'}")
        except Exception as e:
            _progress(f"DeepSeek 懒加载失败，使用空客户端: {e}")
            self.api_key = ""
            self.base_url = "https://api.deepseek.com"
            self.model = "deepseek-v4-flash"
            self.hashtag_count = 5
            self._client = None

    def _ensure_client(self):
        if getattr(self, "_client", None) is not None:
            return
        key = (getattr(self, "api_key", None) or "").strip()
        if not key or key in ("sk-xxx", "your-key", "changeme"):
            return
        _progress("创建 DeepSeek 客户端（临时直连，不走本机代理）…")
        import httpx
        from openai import OpenAI

        saved = {
            n: os.environ.pop(n, None)
            for n in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        }
        try:
            timeout = httpx.Timeout(10.0, connect=8.0, read=45.0, write=10.0)
            self._client = OpenAI(
                api_key=key,
                base_url=getattr(self, "base_url", None) or "https://api.deepseek.com",
                timeout=timeout,
                max_retries=1,
            )
            _progress("DeepSeek 客户端 OK")
        finally:
            for n, v in saved.items():
                if v is not None:
                    os.environ[n] = v

    def generate_fixed(self, *a, **k):
        try:
            _ensure_client(self)
        except Exception as e:
            logger.warning("DeepSeek 客户端失败，将用兜底文案: %s", e)
            self._client = None
        return original_generate(self, *a, **k)

    CG.__init__ = cg_init_lazy  # type: ignore[method-assign]
    CG.generate = generate_fixed  # type: ignore[method-assign]

    # ---- PublisherService.__init__ ----
    original_ps_init = publisher.PublisherService.__init__

    def ps_init_logged(self, *a, **k):
        _progress("PublisherService 初始化开始")
        try:
            out = original_ps_init(self, *a, **k)
            _progress("PublisherService 初始化完成")
            return out
        except Exception as e:
            _progress(f"PublisherService 初始化异常: {e}")
            raise

    publisher.PublisherService.__init__ = ps_init_logged  # type: ignore[method-assign]

    # ---- BitBrowser 短超时（仅 detail）----
    original_check_agent = bitbrowser.BitBrowser.check_agent

    def _post_fast(self, path: str, payload=None):
        import requests

        p = str(path or "")
        t = _DETAIL_TIMEOUT if ("detail" in p or "checkagent" in p) else 60
        url = f"{str(self.api).rstrip('/')}{path}"
        resp = requests.post(url, json=payload or {}, timeout=t)
        resp.raise_for_status()
        data = resp.json()
        ok = data.get("success", False)
        code = data.get("code")
        if not ok and code not in (None, 0, "0", 200, "200"):
            raise RuntimeError(f"BitBrowser API 失败: {data}")
        return data.get("data") if isinstance(data, dict) and "data" in data else data

    def check_agent_fast(self, proxy, timeout=_CHECKAGENT_TIMEOUT):
        return original_check_agent(
            self, proxy, timeout=min(int(timeout or _CHECKAGENT_TIMEOUT), _CHECKAGENT_TIMEOUT)
        )

    bitbrowser.BitBrowser._post = _post_fast  # type: ignore[method-assign]
    bitbrowser.BitBrowser.check_agent = check_agent_fast  # type: ignore[method-assign]

    original_health = bitbrowser.BitBrowser.health

    def health_logged(self):
        _progress("连接比特浏览器 API…")
        ok = original_health(self)
        _progress("比特 API 连通" if ok else "比特 API 未连通")
        return ok

    bitbrowser.BitBrowser.health = health_logged  # type: ignore[method-assign]

    # ---- 代理体检 ----
    original_check_accounts = proxy_health.check_accounts_proxy

    def check_accounts_proxy_fast(accounts, api="http://127.0.0.1:54345", *, max_workers=8):
        accounts = list(accounts or [])
        if not accounts:
            return []
        total = len(accounts)
        _progress(f"代理体检开始 {total} 个号，限时 {_PRECHECK_BUDGET_SEC}s")
        bb = bitbrowser.BitBrowser(api)
        results: dict[str, dict] = {}
        done = 0
        deadline = time.time() + _PRECHECK_BUDGET_SEC
        workers = max(1, min(int(max_workers or 8), 6, total))
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(proxy_health.check_account_proxy, bb, a): a for a in accounts}
                pending = set(futs)
                while pending:
                    left = deadline - time.time()
                    if left <= 0:
                        _progress(f"代理体检超时，已完成 {done}/{total}，其余放行")
                        for fut in pending:
                            fut.cancel()
                            acc = futs[fut]
                            pid = acc.get("profile_id")
                            results[pid] = {
                                "profile_id": pid,
                                "name": acc.get("name") or pid,
                                "status": getattr(proxy_health, "UNKNOWN", "unknown"),
                                "message": "体检超时，已跳过",
                            }
                        break
                    try:
                        for fut in as_completed(list(pending), timeout=min(3.0, left)):
                            pending.discard(fut)
                            acc = futs[fut]
                            pid = acc.get("profile_id")
                            try:
                                row = fut.result()
                            except Exception as e:
                                row = {
                                    "profile_id": pid,
                                    "name": acc.get("name") or pid,
                                    "status": getattr(proxy_health, "UNKNOWN", "unknown"),
                                    "message": f"检测异常：{e}",
                                }
                            results[pid] = row
                            done += 1
                            if done == 1 or done == total or done % 3 == 0:
                                _progress(
                                    f"代理体检 {done}/{total} | {row.get('name') or pid} -> {row.get('status')}"
                                )
                    except FuturesTimeout:
                        continue
        except Exception as e:
            _progress(f"代理体检异常，回退原版: {e}")
            return original_check_accounts(accounts, api, max_workers=max_workers)

        out = []
        for a in accounts:
            pid = a.get("profile_id")
            out.append(
                results.get(
                    pid,
                    {
                        "profile_id": pid,
                        "name": a.get("name") or pid,
                        "status": getattr(proxy_health, "UNKNOWN", "unknown"),
                        "message": "未检出",
                    },
                )
            )
        _progress(f"代理体检结束 {len(out)}/{total}")
        return out

    proxy_health.check_accounts_proxy = check_accounts_proxy_fast  # type: ignore[assignment]

    original_round = publisher.PublisherService.run_round
    original_forever = publisher.PublisherService.run_forever

    def run_round_logged(self, *a, **k):
        _progress("开始 run_round（拉账号/体检/分组/发布）")
        return original_round(self, *a, **k)

    def run_forever_logged(self, *a, **k):
        _progress("开始 run_forever（持续运行）")
        return original_forever(self, *a, **k)

    publisher.PublisherService.run_round = run_round_logged  # type: ignore[method-assign]
    publisher.PublisherService.run_forever = run_forever_logged  # type: ignore[method-assign]

    # ---- 关键：重写 _guard_network，避免后台线程 after() 卡死 ----
    try:
        from tkinter import messagebox
        from ui import app as ui_app

        App = ui_app.App
        original_start_poll = App._start_poll

        def guard_network_safe(self):
            """在任务线程内执行；UI 更新用 after 但包 try，失败不阻塞。"""
            _progress("执行网络预检 _probe_network …")
            result = self._probe_network()
            try:
                self.after(0, lambda r=result: self._apply_net_label(r))
            except Exception as e:
                _progress(f"更新网络标签跳过: {e}")
            ok = bool((result or {}).get("ok"))
            msg = (result or {}).get("message") or ""
            detail = (result or {}).get("detail") or ""
            hint = (result or {}).get("hint") or ""
            if ok:
                print(f"[网络预检] {msg} · {detail}", flush=True)
                _progress("网络预检通过，准备初始化发布服务")
                return
            text = f"{msg}；{detail} {hint}".strip()
            raise RuntimeError(text)

        def start_poll_fixed(self, dry: bool = False, forever: bool = False):
            # 复用原确认框逻辑
            if not dry:
                if forever:
                    if not messagebox.askyesno(
                        "持续运行",
                        "任务会一轮接一轮持续跑下去，直到你点「终止后台任务」。确定开始？",
                    ):
                        return
                else:
                    if not messagebox.askyesno(
                        "确认真实轮询",
                        "将对所有启用且已就绪的账号真实发布（冷却中的账号会跳过），确定？",
                    ):
                        return

            def job():
                from publisher import PublisherService

                _progress(
                    f"真实发布任务线程已启动 dry={dry} forever={forever}"
                )
                guard_network_safe(self)
                _progress("即将构造 PublisherService …")
                svc = PublisherService(cancel_event=self.runner.cancel_event)
                _progress("PublisherService 已构造，写入 dry_run 并启动")
                svc.cfg.setdefault("publish", {})["dry_run"] = bool(dry)
                if forever:
                    summary = svc.run_forever()
                else:
                    summary = svc.run_round(rounds=1)
                try:
                    import json

                    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                except Exception:
                    pass
                _progress("本轮任务函数返回")
                return summary

            label = (
                "多账号持续干跑"
                if dry and forever
                else "多账号持续真实轮询"
                if forever
                else "多账号干跑"
                if dry
                else "多账号真实轮询"
            )
            _progress(f"提交后台任务：{label}")
            self._run_job(label, job)

        App._guard_network = guard_network_safe  # type: ignore[method-assign]
        App._start_poll = start_poll_fixed  # type: ignore[method-assign]
        _progress("已接管 _start_poll / _guard_network（真实发布进度可见）")
    except Exception as e:
        logger.exception("接管发布任务失败: %s", e)
        _progress(f"接管发布任务失败: {e}")

    logger.info("已应用补丁：真实发布预检后假死修复（强制进度日志）")
