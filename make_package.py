"""按白名单打交付压缩包：只带运行必需文件，清空账号/映射/状态/日志。"""
from __future__ import annotations

import json
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent.parent / "TikTokPublisher-clean.zip"
TOP = "TikTokPublisher"

# 运行必需：整目录带走
DIRS = [
    Path("_internal"),
    Path("_tools/py311"),
    Path("_tools/runtime_site"),
    Path("patches"),
]
# 运行必需：单个文件
FILES = [
    Path("TikTokPublisher.exe"),
    Path("run_fixed.py"),
    Path("tools_clear_drafts.py"),
    Path("启动_清理草稿.bat"),
    Path("config.yaml"),
    Path("config.example.yaml"),
    Path(".env"),
    Path(".env.example"),
    Path("assets/app.ico"),
    Path("assets/app_icon.png"),
    Path("启动 TikTokPublisher.lnk"),
]
# 任何层级都不要的目录名
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
# 空目录：程序自己会写进去，但先建好免得报错
EMPTY_DIRS = ["logs", "videos", "data"]

ACCOUNTS_TEMPLATE = """# 比特浏览器窗口 ↔ TikTok 账号列表
# 首次使用：打开软件 →「账号」页 → 同步比特窗口，会自动写入这里
# 默认按比特「相同出口 IP / 相同代理配置」自动划分 proxy_group（ip-1, ip-2…）
#
# 字段：
#   profile_id  : 比特窗口 ID（必填）
#   name        : 备注名
#   proxy_group : IP 组（相同出口/代理自动归为一组）
#   proxy_hint  : 识别依据（lastIp 或 host:port，仅展示）
#   enabled     : 是否参与轮询

accounts: []
"""


def walk(base: Path):
    for p in sorted(base.rglob("*")):
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.is_file():
            yield p


def main() -> None:
    missing = [str(p) for p in DIRS + FILES if not (ROOT / p).exists()]
    if missing:
        print("缺少必需项，已中止：")
        for m in missing:
            print("  -", m)
        raise SystemExit(1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    t0 = time.time()
    count = 0
    raw = 0
    per_dir = {}
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for d in DIRS:
            n = 0
            for f in walk(ROOT / d):
                z.write(f, f"{TOP}/{f.relative_to(ROOT).as_posix()}")
                raw += f.stat().st_size
                n += 1
                count += 1
            per_dir[str(d)] = n
            print(f"  已加入 {d}（{n} 个文件）")

        for f in FILES:
            src = ROOT / f
            z.write(src, f"{TOP}/{f.as_posix()}")
            raw += src.stat().st_size
            count += 1
        print(f"  已加入 {len(FILES)} 个根目录文件")

        for d in EMPTY_DIRS:
            z.writestr(f"{TOP}/{d}/", "")

        # 清空后的模板，不带任何账号/商品/发布记录
        z.writestr(f"{TOP}/data/accounts.yaml", ACCOUNTS_TEMPLATE)
        z.writestr(f"{TOP}/data/product_video_map.json", json.dumps([], ensure_ascii=False) + "\n")
        count += 2
        print("  已写入空的 accounts.yaml / product_video_map.json")

    size = OUT.stat().st_size
    print()
    print(f"输出: {OUT}")
    print(f"文件数: {count} | 原始 {raw / 1048576:.1f} MB → 压缩后 {size / 1048576:.1f} MB "
          f"（{size / raw * 100:.0f}%）| 耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
