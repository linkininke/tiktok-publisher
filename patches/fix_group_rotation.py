"""IP 组公平轮换：在冷却里睡觉的组不再占着并发名额。

原版 run_round 把每个 IP 组当成一个任务丢进 ThreadPoolExecutor(并发上限)，组任务
内部串行跑完自己的账号才返回。账号之间要等 IP 组冷却（默认 500s），而这段等待发生
在 run_for_account 内部，于是「正在等冷却的组」一直占着并发名额；第 3、4 组明明冷却
早就到点，也只能等第 1、2 组把整组跑完才轮得到。

这里改成工作守恒（work-conserving）调度：线程不再绑定某一个组，而是从「全部组」的
共享队列里挑一个此刻就绪的组，跑它的一个账号，跑完把组放回队列，再挑下一个就绪的组。
防风控的约束一条都没放松：

* 同一个 IP 组同时只有一个账号在跑；
* 账号冷却 / IP 组冷却仍由 run_for_account 内部等待，这里只负责优先挑不用等的；
* 同时在跑的组数依旧由 run_round 的线程池上限（poll.max_parallel_ips）决定。

只有所有组都在冷却时才会真的睡，且睡的是「全局最短剩余时间」而不是某一组的。
出问题可以把 config.yaml 的 poll.fair_group_rotation 设成 false 退回原版行为。

另外每轮开跑前做一次体检（poll.auto_parallel 可关）：

* 预估本轮时长，并指出瓶颈是「最大组的冷却链」还是「机器并发不够」；
* 组大小不均时告警——单轮时长只由最大的组决定，跟总账号数无关。100 个号里只要有
  一组塞了 15 个，整轮就会被它从 41 分钟拖到 141 分钟，而其余组早就闲着；
* poll.max_parallel_ips 填 0 时，按「本机能开几个窗口」和「实际组数」自动填一个。

并发上限以手填的为准：机器估算只是按核数和内存套的公式，没算硬盘、上行带宽和比特
自身开销，实测不少机器跑不到估出来的数。所以 poll.max_parallel_ips 一旦填了正数就
照着跑，自动校准不会再改它（比估算高只告警）；只有填 0 才交给自动。
"""

from __future__ import annotations

import logging
import statistics
import threading
import time

logger = logging.getLogger("publisher")

# 剩余不到这个秒数就当已就绪，免得为了几百毫秒空转一轮
READY_SLACK_SECONDS = 1.0
# 有账号待跑但组都被别的线程占着时的重试间隔
BUSY_POLL_SECONDS = 2.0
# 单次等待上限：睡太久就没法及时响应取消，也发现不了刚就绪的组
MAX_SLEEP_SECONDS = 15.0
# 全组冷却中时的等待日志间隔，避免刷屏
WAIT_LOG_INTERVAL = 30.0

# 首轮还没有实测数据时的单账号占用估值（实测中位 63s，留点余量）
DEFAULT_SECONDS_PER_ACCOUNT = 75.0
# 单账号耗时超过这个值不计入均值，避免个别卡死把估算带偏
DURATION_OUTLIER_SECONDS = 600.0
# 机器估算的软上限：再多也没意义，反而抢带宽
MACHINE_CAP_LIMIT = 8

# run_round 执行期间指向当前 PublisherService，给分组函数登记用
_CURRENT = threading.local()
_MODULE_LOCK = threading.Lock()
_MACHINE_CAP: tuple[int, str] | None = None


def _cooling_left(svc, profile_id, ip_group) -> tuple[float, str]:
    """还要等多久才能发这个号（不阻塞）。返回 (秒, 原因)。"""
    try:
        res = svc._account_cooling_left(profile_id, ip_group)
    except Exception as e:  # 查不出来就当就绪，等待交给 run_for_account 内部兜底
        logger.debug("查冷却失败 profile=%s group=%s: %s", profile_id, ip_group, e)
        return 0.0, "unknown"
    seconds, reason = 0.0, "ready"
    if isinstance(res, (tuple, list)):
        if res:
            seconds = res[0]
        if len(res) > 1:
            reason = str(res[1] or "ready")
    else:
        seconds = res
    try:
        seconds = max(0.0, float(seconds or 0.0))
    except (TypeError, ValueError):
        seconds = 0.0
    return seconds, reason


class _RoundPool:
    """一轮里所有 IP 组的共享待跑队列。"""

    def __init__(self, round_i: int) -> None:
        self.round_i = int(round_i)
        self.lock = threading.RLock()
        # gname -> {queue: [(slot, account)], total, busy, last, order, arrived, started}
        self.groups: dict[str, dict] = {}
        self.claimed: set[str] = set()  # 已派发的 profile_id，防重复发布
        self._order = 0
        self._announced = False
        self._last_wait_log = 0.0

    def ensure_group(self, gname: str, members, arrived: bool) -> None:
        """登记一个组。arrived=True 表示这是组任务自己带来的权威名单。"""
        rows = [dict(m) for m in (members or []) if isinstance(m, dict)]
        with self.lock:
            g = self.groups.get(gname)
            if g is None:
                self._order += 1
                self.groups[gname] = {
                    "queue": [(i + 1, m) for i, m in enumerate(rows)],
                    "ids": {str(m.get("profile_id") or "") for m in rows},
                    "total": len(rows),
                    "busy": False,
                    "last": 0.0,
                    "order": self._order,
                    "arrived": bool(arrived),
                    "started": False,
                }
                return
            if not arrived or g["arrived"]:
                return
            g["arrived"] = True
            # 比的是原始名单，不是剩余队列——队列可能已经跑掉一部分了
            new = {str(m.get("profile_id") or "") for m in rows}
            if g["ids"] == new:
                return
            if g["started"]:
                # 已经开跑了就别换名单，宁可少跑也不能重复发
                logger.warning("[轮换] 组 %s 的名单与预登记不一致，沿用预登记名单", gname)
                return
            g["queue"] = [(i + 1, m) for i, m in enumerate(rows)]
            g["ids"] = new
            g["total"] = len(rows)

    def announce(self) -> tuple[int, int] | None:
        """第一个进来的线程负责打开场日志。"""
        with self.lock:
            if self._announced:
                return None
            self._announced = True
            return len(self.groups), sum(len(g["queue"]) for g in self.groups.values())

    def claim(self, svc) -> tuple[str, dict]:
        """挑一个此刻能跑的账号。

        返回 ("job", {...}) / ("wait", {...}) / ("done", {})。
        """
        with self.lock:
            pending = [(n, g) for n, g in self.groups.items() if g["queue"]]
            if not pending:
                return "done", {}
            free = [(n, g) for n, g in pending if not g["busy"]]
            if not free:
                return "wait", {"seconds": BUSY_POLL_SECONDS, "busy": True}

            ready: list[tuple] = []
            soonest: tuple[float, str, str] | None = None
            for gname, g in free:
                best_idx, best_left, best_reason = -1, None, "ready"
                for idx, (_slot, acct) in enumerate(g["queue"]):
                    left, reason = _cooling_left(svc, acct.get("profile_id"), gname)
                    if best_left is None or left < best_left:
                        best_idx, best_left, best_reason = idx, left, reason
                    if left <= READY_SLACK_SECONDS:
                        break
                if best_left is None:
                    continue
                if best_left <= READY_SLACK_SECONDS:
                    # 就绪的组里挑最久没跑过的，天然形成组间轮换
                    ready.append((g["last"], g["order"], gname, best_idx))
                elif soonest is None or best_left < soonest[0]:
                    soonest = (best_left, gname, best_reason)

            if not ready:
                if soonest is None:
                    return "wait", {"seconds": BUSY_POLL_SECONDS, "busy": True}
                seconds, gname, reason = soonest
                return "wait", {
                    "seconds": min(seconds, MAX_SLEEP_SECONDS),
                    "next_group": gname,
                    "next_left": seconds,
                    "reason": reason,
                    "pending": sum(len(g["queue"]) for _n, g in pending),
                }

            ready.sort()
            _last, _order, gname, idx = ready[0]
            g = self.groups[gname]
            slot, acct = g["queue"].pop(idx)
            pid = str(acct.get("profile_id") or "")
            if pid and pid in self.claimed:
                logger.warning("[轮换] 账号 %s 已派发过，跳过以防重复发布", pid)
                return "wait", {"seconds": 0.1, "busy": True}
            if pid:
                self.claimed.add(pid)
            first = not g["started"]
            g["started"] = True
            g["busy"] = True
            g["last"] = time.time()
            return "job", {
                "group": gname,
                "slot": slot,
                "total": g["total"],
                "account": acct,
                "first": first,
                "pending": sum(len(x["queue"]) for x in self.groups.values()),
            }

    def release(self, gname: str) -> bool:
        """归还组。返回 True 表示这个组已经跑完了。"""
        with self.lock:
            g = self.groups.get(gname)
            if not g:
                return False
            g["busy"] = False
            g["last"] = time.time()
            return not g["queue"]

    def log_wait(self, info: dict) -> None:
        if info.get("busy"):
            return
        with self.lock:
            now = time.time()
            if now - self._last_wait_log < WAIT_LOG_INTERVAL:
                return
            self._last_wait_log = now
        logger.info(
            "[轮换] 所有 IP 组都在冷却，最快的是 %s（还剩 %.0f 秒，%s）| 待跑 %s 个账号",
            info.get("next_group") or "?",
            float(info.get("next_left") or 0.0),
            "账号冷却" if info.get("reason") == "account" else "IP 组冷却",
            info.get("pending") or 0,
        )


def _machine_cap() -> tuple[int, str]:
    """本机最多同时开几个比特窗口（含说明）。结果缓存，wmic 只探一次。"""
    global _MACHINE_CAP
    if _MACHINE_CAP is not None:
        return _MACHINE_CAP
    cores, ram_gb = 4, 8.0
    try:
        from fix_ui_extras import _probe_machine_perf

        perf = _probe_machine_perf()
        cores = int(perf.get("logical_cores") or 4)
        ram_gb = float(perf.get("ram_gb") or 8.0)
    except Exception as e:
        logger.debug("探测机器性能失败，按 4 核 8G 估: %s", e)
        try:
            import os

            cores = int(os.cpu_count() or 4)
        except Exception:
            pass
    # 每个比特窗口约 2 逻辑核 + 1.8GB；给系统和本软件留 2 核 + 4GB
    cpu_cap = max(1, (cores - 2) // 2)
    ram_cap = max(1, int(max(0.0, ram_gb - 4.0) // 1.8))
    cap = max(1, min(cpu_cap, ram_cap, MACHINE_CAP_LIMIT))
    why = "%s 逻辑核 / %.0fGB 内存 → CPU 可撑 %s、内存可撑 %s" % (cores, ram_gb, cpu_cap, ram_cap)
    _MACHINE_CAP = (cap, why)
    return _MACHINE_CAP


def _seconds_per_account(svc) -> tuple[float, bool]:
    """单账号占用时长。有实测就用实测中位数，否则用默认估值。"""
    samples = [
        d
        for d in (getattr(svc, "_fair_durations", None) or [])
        if 0 < float(d) < DURATION_OUTLIER_SECONDS
    ]
    if len(samples) >= 3:
        return float(statistics.median(samples)), True
    cfg = getattr(svc, "cfg", None)
    val = (cfg.get("poll") or {}).get("est_seconds_per_account") if isinstance(cfg, dict) else None
    try:
        if val:
            return max(1.0, float(val)), False
    except (TypeError, ValueError):
        pass
    return DEFAULT_SECONDS_PER_ACCOUNT, False


def _record_duration(svc, seconds: float) -> None:
    with _svc_lock(svc):
        hist = getattr(svc, "_fair_durations", None)
        if not isinstance(hist, list):
            hist = []
            svc._fair_durations = hist
        hist.append(float(seconds))
        del hist[:-40]


def _estimate_round_minutes(counts: list[int], workers: int, t_sec: float, c_sec: float) -> tuple[float, str]:
    """预估单轮时长，并指出瓶颈。

    公式经 100 号 / 20 组仿真校验，误差 2% 以内：
      最大组的冷却链 chain = n*T + (n-1)*C
      首波排队延迟   queue = (组数-1)*T/并发
      机器总工作量   work  = 总账号*T/并发
      单轮 ≈ max(chain + queue, work)
    """
    if not counts:
        return 0.0, "无账号"
    n_max = max(counts)
    total = sum(counts)
    k = max(1, int(workers or 1))
    chain = n_max * t_sec + max(0, n_max - 1) * c_sec
    queue = max(0, len(counts) - 1) * t_sec / k
    work = total * t_sec / k
    if work >= chain + queue:
        return work / 60.0, "机器并发不够（总工作量摊不开）"
    return (chain + queue) / 60.0, "最大组的冷却链（%s 个号一条 IP）" % n_max


def _manual_cap(svc) -> int:
    """用户手填的并发上限，0 表示留给自动校准。

    只认 config 里的值：自动校准会把算出来的数写回 svc.max_parallel_ips，
    再从属性读就分不清「用户填的 2」和「上一轮自动算出来的 4」了。
    """
    cfg = getattr(svc, "cfg", None)
    poll = (cfg.get("poll") or {}) if isinstance(cfg, dict) else {}
    raw = poll.get("max_parallel_ips")
    if raw is None:  # 老配置里没这个键，只能退回属性
        raw = getattr(svc, "max_parallel_ips", 0)
    try:
        return max(0, int(float(raw or 0)))
    except (TypeError, ValueError):
        return 0


def _tune_parallel(svc, group_count: int) -> int:
    """定下本轮的并发数。返回实际生效的并发数。

    手填的值说了算。_machine_cap() 只是按核数和内存套公式，没算硬盘、上行带宽和比特
    自身开销，不少机器实际跑不到那个数，所以填了就照着跑，绝不替用户改大。
    只有留 0（=自动）时才用机器估算去填。
    """
    cfg = getattr(svc, "cfg", None)
    poll = (cfg.get("poll") or {}) if isinstance(cfg, dict) else {}
    manual = _manual_cap(svc)

    if manual > 0:
        # 上一轮自动校准可能把属性改过，这里改回手填值，run_round 才会按它开线程池
        svc.max_parallel_ips = manual
        effective = min(manual, group_count)
        cap, why = _machine_cap()
        if manual > cap:
            logger.warning(
                "[体检] 并发上限 %s（手动设置，按你设的跑）| 机器估算只建议 %s（%s）。"
                "要是单号耗时明显变长或窗口卡，调小到 %s 再试",
                manual,
                cap,
                why,
                cap,
            )
        elif effective < manual:
            logger.info(
                "[体检] 并发上限 %s（手动设置）| 只有 %s 个 IP 组，本轮同时跑 %s 组",
                manual,
                group_count,
                effective,
            )
        else:
            logger.info(
                "[体检] 并发上限 %s（手动设置）| %s 个 IP 组 → 本轮同时跑 %s 组%s",
                manual,
                group_count,
                effective,
                "；机器估算还能撑 %s，想更快可以往上调" % cap if manual < cap else "",
            )
        return effective

    if poll.get("auto_parallel") is False:
        logger.info(
            "[体检] 并发上限 不限（并行上限填 0 且已关自动校准）→ %s 个 IP 组全开。"
            "机器扛不住就在「并行 IP 上限」填个数字",
            group_count,
        )
        return group_count

    cap, why = _machine_cap()
    target = max(1, min(cap, group_count))
    svc.max_parallel_ips = target
    logger.info(
        "[体检] 并发上限 自动 → %s：本机估算可撑 %s 个窗口（%s），当前 %s 个 IP 组。"
        "想固定下来就在「并行 IP 上限」填数字，填了就不再自动改",
        target,
        cap,
        why,
        group_count,
    )
    return target


def _preflight(svc, groups: dict) -> None:
    """开跑前的分组体检：校准并发、预估时长、组大小不均就告警。"""
    counts = [len(v or []) for v in groups.values()]
    if not counts:
        return
    workers = _tune_parallel(svc, len(counts))
    t_sec, measured = _seconds_per_account(svc)
    c_sec = float(getattr(svc, "ip_group_cooldown", 0.0) or 0.0) + float(
        getattr(svc, "cooldown_jitter", 0.0) or 0.0
    ) / 2.0
    est, bottleneck = _estimate_round_minutes(counts, workers, t_sec, c_sec)
    logger.info(
        "[体检] %s 个号 / %s 个 IP 组（每组 %s~%s 个）| 并发 %s | 单号占用 %.0fs（%s）| "
        "预计单轮 ≈ %.0f 分钟 | 瓶颈：%s",
        sum(counts),
        len(counts),
        min(counts),
        max(counts),
        workers,
        t_sec,
        "实测" if measured else "估值",
        est,
        bottleneck,
    )

    # 单轮时长只看最大组，所以真正要防的是「有组特别大」
    n_max = max(counts)
    limit = max(1, int(getattr(svc, "per_ip_group", 5) or 5))
    if n_max <= limit or len(counts) <= 1:
        return
    fat = [g for g, v in groups.items() if len(v or []) == n_max]
    balanced = -(-sum(counts) // len(counts))  # 均衡后每组的个数（上取整）
    est_balanced, _ = _estimate_round_minutes(
        [balanced] * len(counts), workers, t_sec, c_sec
    )
    logger.warning(
        "[体检] 组 %s 有 %s 个号，超过每组上限 %s。单轮时长只由最大的组决定，"
        "它会把本轮拖到 %.0f 分钟；均衡到每组 %s 个可降到 %.0f 分钟（其余组现在多半在闲等）。"
        "多半是比特没返回 lastIp/代理配置，去「账号」页重新同步，或改 data/accounts.yaml 的 proxy_group 拆开",
        "、".join(fat[:3]) + ("…" if len(fat) > 3 else ""),
        n_max,
        limit,
        est,
        balanced,
        est_balanced,
    )


def _svc_lock(svc) -> threading.Lock:
    with _MODULE_LOCK:
        lk = getattr(svc, "_fair_lock", None)
        if lk is None:
            lk = threading.Lock()
            svc._fair_lock = lk
        return lk


def _get_pool(svc, round_i) -> _RoundPool | None:
    """取本轮的共享队列。run_round 包装没生效时返回 None，退回原版。"""
    gen = int(getattr(svc, "_fair_gen", 0) or 0)
    if gen <= 0:
        return None
    key = (gen, int(round_i or 0))
    with _svc_lock(svc):
        pools = getattr(svc, "_fair_pools", None)
        if not isinstance(pools, dict):
            pools = {}
            svc._fair_pools = pools
        pool = pools.get(key)
        if pool is None:
            pool = _RoundPool(round_i)
            pools[key] = pool
            for stale in list(pools)[:-4]:
                pools.pop(stale, None)
        return pool


def _stashed_groups(svc) -> dict:
    """run_round 本次算出的完整分组（由分组函数包装登记）。"""
    if int(getattr(svc, "_fair_groups_gen", -1)) != int(getattr(svc, "_fair_gen", 0) or 0):
        return {}
    groups = getattr(svc, "_fair_groups", None)
    return groups if isinstance(groups, dict) else {}


def _enabled(svc) -> bool:
    cfg = getattr(svc, "cfg", None)
    val = None
    if isinstance(cfg, dict):
        val = (cfg.get("poll") or {}).get("fair_group_rotation")
    return True if val is None else bool(val)


def _abort_if_all_proxies_failed(svc) -> None:
    """接手了 _run_ip_group，原来挂在它 finally 上的全代理失败检查得补回来。"""
    try:
        from fix_all_proxies_failed import _should_abort
    except Exception:
        return
    tracker = getattr(svc, "_proxy_fail_tracker", None)
    if not isinstance(tracker, dict) or not _should_abort(tracker):
        return
    tracker["abort"] = True
    try:
        svc.cancel_event.set()
    except Exception:
        pass


def _run_one_account(svc, group_name: str, job: dict, round_i) -> list[dict]:
    """跑一个账号。逻辑与原版 _run_ip_group 的循环体一致。"""
    import publisher

    slot, total = job["slot"], job["total"]
    account = dict(job["account"])
    account["_poll_slot"] = slot
    account["_poll_group"] = group_name
    label = account.get("name") or account["profile_id"]
    logger.info(
        "---- [%s] 组内 %s/%s | 账号=%s | 等待独立冷却后发布 ----",
        group_name,
        slot,
        total,
        label,
    )
    started = time.time()
    cancelled = False
    try:
        results = svc.run_for_account(account)
    except publisher.CancelledError:
        cancelled = True
        raise
    except Exception as e:
        logger.exception("[%s] 账号 %s 执行失败: %s", group_name, label, e)
        results = [
            publisher.PublishResult(
                ok=False,
                status="error",
                message=str(e),
                video="",
                product_id="",
            )
        ]
        dry_run = bool((svc.cfg.get("publish") or {}).get("dry_run", False))
        if not dry_run:
            svc._mark_cooldowns(account["profile_id"], group_name, label=label)
    finally:
        # 喂给下一轮的时长预估：调度只派就绪的号，所以这里几乎不含冷却等待。
        # 用户中途终止那次不算，否则会把估值带偏。
        if not cancelled:
            _record_duration(svc, time.time() - started)
    rows = []
    for r in results:
        rows.append(
            {
                "round": round_i,
                "account": label,
                "profile_id": account["profile_id"],
                "proxy_group": account.get("proxy_group") or group_name,
                "poll_slot": slot,
                **r.__dict__,
            }
        )
    return rows


def apply_group_rotation_patches() -> None:
    import publisher

    prev_ip_group = publisher.PublisherService._run_ip_group
    prev_round = publisher.PublisherService.run_round
    prev_group_fn = publisher.group_accounts_by_proxy

    def group_accounts_registered(*a, **k):
        groups = prev_group_fn(*a, **k)
        svc = getattr(_CURRENT, "svc", None)
        if svc is not None and isinstance(groups, dict):
            try:
                svc._fair_groups = {
                    str(g): [dict(m) for m in (members or []) if isinstance(m, dict)]
                    for g, members in groups.items()
                }
                svc._fair_groups_gen = int(getattr(svc, "_fair_gen", 0) or 0)
            except Exception as e:
                logger.debug("登记 IP 分组失败: %s", e)
            # 必须在这里做：run_round 紧接着就用 max_parallel_ips 算线程池大小
            try:
                _preflight(svc, groups)
            except Exception as e:
                logger.warning("开跑前体检失败（不影响发布）: %s", e)
        return groups

    def run_round_fair(self, *a, **k):
        # 每次 run_round 都是新一代：run_forever 会反复用 round_i=1 调这里
        self._fair_gen = int(getattr(self, "_fair_gen", 0) or 0) + 1
        outer = getattr(_CURRENT, "svc", None)
        _CURRENT.svc = self
        try:
            return prev_round(self, *a, **k)
        finally:
            _CURRENT.svc = outer

    def run_ip_group_fair(self, group_name, members, round_i):
        pool = _get_pool(self, round_i) if _enabled(self) else None
        if pool is None:
            return prev_ip_group(self, group_name, members, round_i)

        # run_round 提交任务前分组就算好了，所以第一个跑起来的线程就能看到所有组
        for gname, gmembers in _stashed_groups(self).items():
            pool.ensure_group(gname, gmembers, arrived=False)
        pool.ensure_group(group_name, list(members or []), arrived=True)

        opened = pool.announce()
        if opened:
            logger.info(
                "######## 公平轮换：%s 个 IP 组 / %s 个账号 | 冷却到点即上，空闲名额不再干等 ########",
                opened[0],
                opened[1],
            )

        summary: list[dict] = []
        while True:
            self._check_cancel()
            kind, info = pool.claim(self)
            if kind == "done":
                break
            if kind == "wait":
                pool.log_wait(info)
                self._interruptible_sleep(float(info.get("seconds") or 1.0))
                continue
            gname = info["group"]
            try:
                if info["first"]:
                    logger.info(
                        ">>>> IP 组 %s 开始 | 组内 %s 个账号（串行，按独立冷却）<<<<",
                        gname,
                        info["total"],
                    )
                logger.info(
                    "[轮换] 选中组 %s（冷却已到）| 组内 %s/%s | 全局待跑 %s 个账号",
                    gname,
                    info["slot"],
                    info["total"],
                    info["pending"],
                )
                summary.extend(_run_one_account(self, gname, info, round_i))
            finally:
                drained = pool.release(gname)
                _abort_if_all_proxies_failed(self)
                if drained:
                    logger.info("<<<< IP 组 %s 结束 >>>>", gname)
        return summary

    publisher.group_accounts_by_proxy = group_accounts_registered  # type: ignore[assignment]
    publisher.PublisherService.run_round = run_round_fair  # type: ignore[method-assign]
    publisher.PublisherService._run_ip_group = run_ip_group_fair  # type: ignore[method-assign]

    logger.info("已应用补丁：IP 组公平轮换（冷却中的组不再占用并发名额）")
