"""代理体检改成真实连通性探测。

原版走比特 /checkagent：把窗口的 host/port/账号密码丢给比特，问一次「出口 IP 是多少」。
它证明的是「这组代理参数能连上并回显 IP」，不是「这个窗口能不能打开 TikTok」：
  - 代理活着但线路到不了 TikTok → 照样判可用
  - 比特侧超时/限流 → 判不可用，配合 skip_bad_proxy_accounts 会把好号踢出本轮
  - 动态IP / 第三方客户端接管的窗口直接跳过不测 → 开窗口才发现 ERR_SOCKS_CONNECTION_FAILED

改成混合探测：
  1) 能拿到固定 host:port 的窗口 → 程序按该窗口的代理直连 www.tiktok.com:443
     （原生 socket 实现 SOCKS5 / HTTP CONNECT，再做一次 TLS 握手确认隧道真的通）
  2) 动态IP / 第三方接管 / 配置不全的窗口 → 才真开比特窗口，访问 TikTok 验证
"""

from __future__ import annotations

import base64
import logging
import socket
import ssl
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("publisher")

TARGET_HOST = "www.tiktok.com"
TARGET_PORT = 443

_SOCKET_TIMEOUT = 8
_WINDOW_GOTO_TIMEOUT_MS = 25000
_WINDOW_BUDGET_SEC = 180
_WINDOW_CONCURRENCY = 2
_PROBE_URL = "https://www.tiktok.com/robots.txt"

# 需要真开窗口才能判定
NEED_WINDOW = "need_window"

class _Inconclusive(Exception):
    """端口是通的，只是我们这边没法完成握手 —— 不能据此判定代理坏了。

    比特窗口里的代理常带账号密码，但 /browser/detail 未必把凭据吐出来。
    这种情况下浏览器能正常走代理，我们却握不了手，只能判「未知」不拦截。
    """


_SOCKS5_ERRORS = {
    1: "代理内部错误",
    2: "代理规则不允许连接",
    3: "网络不可达",
    4: "目标主机不可达",
    5: "目标拒绝连接",
    6: "TTL 超时",
    7: "代理不支持该命令",
    8: "代理不支持该地址类型",
}


def _progress(msg: str) -> None:
    try:
        from fix_precheck_hang import _progress as p

        p(msg)
        return
    except Exception:
        pass
    try:
        logger.info("%s", msg)
        print(f"[进度] {msg}", flush=True)
    except Exception:
        pass


def _connect_tcp(host: str, port: int, timeout: int) -> socket.socket:
    """域名解析出多个地址时 create_connection 会逐个各等一遍超时，体检会被拖长。
    这里优先 IPv4、最多试 2 个地址，把耗时压在 2×timeout 以内。"""
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    last = None
    for af, stype, proto, _canon, addr in infos[:2]:
        sock = socket.socket(af, stype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(addr)
            return sock
        except Exception as e:
            last = e
            try:
                sock.close()
            except Exception:
                pass
            # 超时说明这条链路就是不通，换个地址还是会等满，直接判失败
            if isinstance(e, (socket.timeout, TimeoutError)):
                break
    raise last or OSError("无法建立连接")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("代理提前断开连接")
        buf += chunk
    return buf


def _socks5_connect(host, port, user, pwd, timeout):
    sock = _connect_tcp(host, port, timeout)
    try:
        # 总是同时提供「无需认证」和「账号密码」两种方式：只报无需认证的话，
        # 要求认证的代理会直接掐断连接，看起来就像代理坏了。
        try:
            sock.sendall(b"\x05\x02\x00\x02")
            ver, method = _recv_exact(sock, 2)
            if ver != 5:
                raise _Inconclusive("对端不是标准 SOCKS5，无法直连判定")
            if method == 0xFF:
                raise _Inconclusive("代理拒绝了我们提供的认证方式，无法直连判定")
            if method == 0x02:
                if not user:
                    raise _Inconclusive(
                        "代理需要账号密码，但窗口配置里没读到凭据，无法直连判定"
                    )
                u = user.encode("utf-8")
                p = (pwd or "").encode("utf-8")
                sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                _, status = _recv_exact(sock, 2)
                if status != 0:
                    raise _Inconclusive("代理拒绝了这组账号密码，无法直连判定")
        except _Inconclusive:
            raise
        except Exception as e:
            # 端口已经连上了，握手阶段出问题不能算代理坏
            raise _Inconclusive(f"握手未完成（{e}），无法直连判定")

        target = TARGET_HOST.encode("utf-8")
        sock.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(target)])
            + target
            + struct.pack("!H", TARGET_PORT)
        )
        reply = _recv_exact(sock, 4)
        if reply[1] != 0:
            raise OSError(_SOCKS5_ERRORS.get(reply[1], f"代理返回错误码 {reply[1]}"))
        atyp = reply[3]
        if atyp == 1:
            _recv_exact(sock, 4 + 2)
        elif atyp == 3:
            n = _recv_exact(sock, 1)[0]
            _recv_exact(sock, n + 2)
        elif atyp == 4:
            _recv_exact(sock, 16 + 2)
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def _http_connect(host, port, user, pwd, timeout, *, tls_to_proxy=False):
    sock = _connect_tcp(host, port, timeout)
    try:
        if tls_to_proxy:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)

        req = (
            f"CONNECT {TARGET_HOST}:{TARGET_PORT} HTTP/1.1\r\n"
            f"Host: {TARGET_HOST}:{TARGET_PORT}\r\n"
        )
        if user:
            token = base64.b64encode(
                f"{user}:{pwd or ''}".encode("utf-8")
            ).decode("ascii")
            req += f"Proxy-Authorization: Basic {token}\r\n"
        req += "\r\n"
        sock.sendall(req.encode("utf-8"))

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise OSError("代理提前断开连接")
            head += chunk
            if len(head) > 65536:
                raise OSError("代理返回内容异常")
        first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = first.split()
        if len(parts) >= 2 and parts[1] == "407":
            raise _Inconclusive("代理要求认证，窗口配置里的凭据不可用，无法直连判定")
        if len(parts) < 2 or parts[1] != "200":
            raise OSError(f"代理拒绝 CONNECT：{first.strip()}")
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def _tls_handshake(sock) -> None:
    """CONNECT 通了不代表隧道能传数据，再握一次 TLS 才算真跑通。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    tls = ctx.wrap_socket(sock, server_hostname=TARGET_HOST)
    try:
        tls.close()
    except Exception:
        pass


def probe_by_socket(detail: dict, timeout: int = _SOCKET_TIMEOUT):
    """按窗口配置的代理直连 TikTok。返回 (status, message)，status 可能是 NEED_WINDOW。"""
    from proxy_health import FAIL, OK

    d = detail or {}
    ptype = str(d.get("proxyType") or "").strip().lower()
    host = str(d.get("host") or "").strip()
    port = str(d.get("port") or "").strip()
    user = str(d.get("proxyUserName") or "").strip()
    pwd = str(d.get("proxyPassword") or "")
    dynamic = str(d.get("dynamicIpUrl") or "").strip()

    if dynamic:
        return NEED_WINDOW, "动态IP窗口，需开窗口验证"
    if ptype in ("", "noproxy", "none", "no"):
        # 窗口走本机直连：直接测本机到 TikTok
        try:
            sock = _connect_tcp(TARGET_HOST, TARGET_PORT, timeout)
            try:
                _tls_handshake(sock)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
            return OK, "窗口直连（未配置代理），本机可达 TikTok"
        except Exception as e:
            return FAIL, f"窗口直连但本机连不上 TikTok：{e}"

    if not host or not port:
        return NEED_WINDOW, f"代理类型 {ptype} 无固定地址，需开窗口验证"
    try:
        port_i = int(port)
    except Exception:
        return NEED_WINDOW, f"端口异常（{port}），需开窗口验证"

    from proxy_health import UNKNOWN

    started = time.time()
    try:
        if ptype.startswith("socks5"):
            sock = _socks5_connect(host, port_i, user, pwd, timeout)
        elif ptype in ("http", "https"):
            sock = _http_connect(
                host, port_i, user, pwd, timeout, tls_to_proxy=(ptype == "https")
            )
        else:
            return NEED_WINDOW, f"未知代理类型 {ptype}，需开窗口验证"
    except _Inconclusive as e:
        return UNKNOWN, f"代理端口 {host}:{port} 可达，但{e}"
    except Exception as e:
        return FAIL, f"代理端口连不上（{ptype} {host}:{port}）：{e}"

    try:
        _tls_handshake(sock)
    except Exception as e:
        return FAIL, f"代理已连上但隧道不通（{host}:{port}）：{e}"
    finally:
        try:
            sock.close()
        except Exception:
            pass

    cost = int((time.time() - started) * 1000)
    return OK, f"可达 TikTok（{ptype} {host}:{port}，{cost}ms）"


def probe_by_window(bb, profile_id: str):
    """真开比特窗口访问 TikTok，最贴近实际发布时的链路。"""
    from proxy_health import FAIL, OK

    opened = False
    try:
        info = bb.open(profile_id)
        opened = True
        ws = (info or {}).get("ws")
        if not ws:
            return FAIL, "窗口已开但没有调试地址"

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws)
            try:
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                resp = page.goto(
                    _PROBE_URL,
                    wait_until="domcontentloaded",
                    timeout=_WINDOW_GOTO_TIMEOUT_MS,
                )
                status = resp.status if resp is not None else 0
                # 能拿到任何 HTTP 响应就说明网络跑通了，403/404 也算通
                if status:
                    return OK, f"开窗口验证可达 TikTok（HTTP {status}）"
                return FAIL, "开窗口后未拿到任何响应"
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        return FAIL, f"开窗口验证失败：{str(e).splitlines()[0][:160]}"
    finally:
        if opened:
            try:
                bb.close(profile_id)
            except Exception:
                pass


def _row(account: dict, status: str, message: str) -> dict:
    return {
        "profile_id": account.get("profile_id"),
        "name": account.get("name") or account.get("profile_id"),
        "proxy_group": account.get("proxy_group"),
        "proxy_hint": account.get("proxy_hint"),
        "status": status,
        "message": message,
    }


def check_account_proxy_real(bb, account: dict) -> dict:
    """单账号体检：先直连探测，测不了的再开窗口。"""
    from proxy_health import UNKNOWN

    pid = account.get("profile_id")
    try:
        detail = bb.detail(pid)
    except Exception as e:
        return _row(account, UNKNOWN, f"读取窗口配置失败：{e}")

    status, message = probe_by_socket(detail)
    if status != NEED_WINDOW:
        return _row(account, status, message)

    status, message = probe_by_window(bb, pid)
    return _row(account, status, message)


def check_accounts_proxy_real(accounts, api="http://127.0.0.1:54345", *, max_workers=8):
    """两阶段体检：先并发直连探测，再对少数窗口逐个开窗验证。"""
    import bitbrowser
    from proxy_health import UNKNOWN

    accounts = list(accounts or [])
    if not accounts:
        return []

    bb = bitbrowser.BitBrowser(api)
    total = len(accounts)
    results: dict[str, dict] = {}
    need_window: list[dict] = []

    _progress(f"代理体检开始：{total} 个号，先做直连探测（不开窗口）")

    def _detail_with_retry(pid, attempts=3):
        last = None
        for i in range(attempts):
            try:
                return bb.detail(pid)
            except Exception as e:
                last = e
                # 比特本地 API 并发高了会返回 429，退避重试即可
                if "429" in str(e) or "Too Many" in str(e):
                    time.sleep(0.6 * (i + 1))
                    continue
                raise
        raise last

    def phase1(account):
        pid = account.get("profile_id")
        try:
            detail = _detail_with_retry(pid)
        except Exception as e:
            return account, UNKNOWN, f"读取窗口配置失败：{e}"
        status, message = probe_by_socket(detail)
        return account, status, message

    workers = max(1, min(int(max_workers or 8), 4, total))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(phase1, a) for a in accounts]
        for fut in as_completed(futs):
            try:
                account, status, message = fut.result()
            except Exception as e:
                logger.warning("直连探测异常: %s", e)
                continue
            done += 1
            if status == NEED_WINDOW:
                need_window.append(account)
                continue
            results[account.get("profile_id")] = _row(account, status, message)
            _progress(
                f"代理体检 {done}/{total} | {account.get('name') or account.get('profile_id')} -> {status}"
            )

    if need_window:
        _progress(
            f"{len(need_window)} 个窗口无固定代理地址，改为开窗口验证（每次最多 {_WINDOW_CONCURRENCY} 个）"
        )
        deadline = time.time() + _WINDOW_BUDGET_SEC
        with ThreadPoolExecutor(max_workers=_WINDOW_CONCURRENCY) as pool:
            futs = {
                pool.submit(probe_by_window, bb, a.get("profile_id")): a
                for a in need_window
            }
            pending = set(futs)
            while pending:
                left = deadline - time.time()
                if left <= 0:
                    for fut in pending:
                        fut.cancel()
                        a = futs[fut]
                        results[a.get("profile_id")] = _row(
                            a, UNKNOWN, "开窗口验证超时，未判定"
                        )
                    _progress("开窗口验证超时，其余标为未知（不拦截）")
                    break
                try:
                    for fut in as_completed(list(pending), timeout=min(5.0, left)):
                        pending.discard(fut)
                        a = futs[fut]
                        try:
                            status, message = fut.result()
                        except Exception as e:
                            status, message = UNKNOWN, f"开窗口验证异常：{e}"
                        results[a.get("profile_id")] = _row(a, status, message)
                        _progress(
                            f"开窗口验证 | {a.get('name') or a.get('profile_id')} -> {status}"
                        )
                except TimeoutError:
                    continue
                except Exception:
                    continue

    out = []
    for a in accounts:
        pid = a.get("profile_id")
        out.append(results.get(pid) or _row(a, UNKNOWN, "未检出"))
    _progress(f"代理体检结束 {len(out)}/{total}")
    return out


def apply_proxy_probe_patches() -> None:
    import proxy_health

    if getattr(proxy_health, "_real_probe_patched", False):
        return

    proxy_health.check_account_proxy = check_account_proxy_real  # type: ignore[assignment]
    proxy_health.check_accounts_proxy = check_accounts_proxy_real  # type: ignore[assignment]
    proxy_health._real_probe_patched = True  # type: ignore[attr-defined]
    logger.info(
        "已应用补丁：代理体检改为真实连通性探测（直连 TikTok；动态IP 窗口才开窗验证）"
    )
