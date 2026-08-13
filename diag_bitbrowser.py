"""比特窗口列表为空的取证脚本。

在报错的那台机器上运行：
    _tools\\py311\\python.exe diag_bitbrowser.py

程序内部只取 data.data.list，接口若返回 {"success": false, "msg": ...}
错误信息会被丢掉，最终一样表现为「未获取到任何比特窗口」。
这里把原始返回原样打印出来，用来区分到底是没有窗口还是请求被拒。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (str(ROOT), str(ROOT / "_internal"), str(ROOT / "_tools" / "runtime_site")):
    sys.path.insert(0, p)


def read_api_url() -> str:
    api = ""
    cfg = ROOT / "config.yaml"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("api:"):
                api = s.split(":", 1)[1].strip()
                break
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("BITBROWSER_API="):
                api = line.split("=", 1)[1].strip() or api
    return api or "http://127.0.0.1:54345"


def show(title: str, obj, limit: int = 1200) -> None:
    txt = json.dumps(obj, ensure_ascii=False, indent=2)
    print(f"--- {title} ---")
    print(txt[:limit] + (f"\n…（还有 {len(txt) - limit} 字符）" if len(txt) > limit else ""))
    print()


def main() -> None:
    api = read_api_url().rstrip("/")
    print(f"比特 API 地址: {api}")

    print("\n=== 代理环境 ===")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        v = os.environ.get(k)
        if v:
            print(f"  {k}={v}")
    bypass = (os.environ.get("NO_PROXY") or "") + (os.environ.get("no_proxy") or "")
    if (os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")) and "127.0.0.1" not in bypass:
        print("  ⚠ 设了代理但没把 127.0.0.1 排除，本机 API 请求可能被送去代理")
    else:
        print("  本机地址不会被代理劫持")
    # 取证时一律直连本机，避免代理干扰判断
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost,::1"

    import requests

    print("=== /health ===")
    try:
        r = requests.post(f"{api}/health", json={}, timeout=8)
        print(f"  HTTP {r.status_code}")
        show("原始返回", r.json())
    except Exception as e:
        print(f"  连不上: {e}")
        print("\n  → 比特浏览器客户端没启动，或本地 API 端口不是这个。")
        print("     在比特客户端里确认「设置 → 本地接口」已开启，端口与上面一致。")
        return

    total_seen = 0
    for page in (0, 1):
        print(f"=== /browser/list  page={page} pageSize=100（程序就是这么调的）===")
        try:
            r = requests.post(f"{api}/browser/list", json={"page": page, "pageSize": 100}, timeout=15)
            body = r.json()
        except Exception as e:
            print(f"  请求失败: {e}\n")
            continue
        print(f"  HTTP {r.status_code}")
        success = body.get("success")
        msg = body.get("msg") or body.get("message") or ""
        data = body.get("data") or {}
        rows = data.get("list") or data.get("data") or []
        total = data.get("totalNum") if isinstance(data, dict) else None
        print(f"  success={success}  msg={msg!r}  totalNum={total}  本页条数={len(rows)}")
        total_seen += len(rows)
        if rows:
            print("  前 3 个窗口:")
            for row in rows[:3]:
                print(f"    id={row.get('id') or row.get('browserId')} "
                      f"name={row.get('name')!r} seq={row.get('seq')} "
                      f"lastIp={row.get('lastIp')} proxyType={row.get('proxyType')}")
        else:
            show("原始返回（本页为空，看这里）", body)

    print("=== 结论 ===")
    if total_seen:
        print(f"  接口能取到 {total_seen} 个窗口 → 程序应该也能同步，请把这份输出发回来。")
    else:
        body_ok = True
        try:
            r = requests.post(f"{api}/browser/list", json={"page": 0, "pageSize": 100}, timeout=15)
            b = r.json()
            body_ok = bool(b.get("success"))
            if not body_ok:
                print(f"  接口拒绝了请求: {b.get('msg') or b.get('message')}")
                print("  → 多为比特客户端未登录、本地接口未开启，或该版本接口有变。")
        except Exception:
            pass
        if body_ok:
            print("  接口正常但窗口数为 0 → 这台机器的比特账号里确实没有窗口。")
            print("  → 打开比特客户端看窗口列表：若是空的，说明登录的不是拥有那 11 个窗口的账号；")
            print("     窗口是按比特账号云端同步的，换成正确账号登录后再点同步。")


if __name__ == "__main__":
    main()
