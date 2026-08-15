"""按白名单打交付压缩包：只带运行必需文件，清空账号/映射/状态/日志。"""
from __future__ import annotations

import json
import shutil
import struct
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

# 扩展模块(.pyd)加载时只搜自身目录，不会搜父目录；MSVCP140 等在 _internal/ 根下
# 而 greenlet 在子目录里，裸 Windows 会 ImportError: _greenlet DLL load failed。
RUNTIME_DLLS = {
    "msvcp140.dll",
    "msvcp140_1.dll",
    "vcruntime140_1.dll",
}
# vcruntime140.dll 进程启动时由 python311.dll 载入，无需逐个目录复制
SKIP_RUNTIME = {"vcruntime140.dll"}
RUNTIME_SOURCES = {
    "msvcp140.dll": ROOT / "_internal" / "MSVCP140.dll",
    "vcruntime140.dll": ROOT / "_tools" / "py311" / "vcruntime140.dll",
    "vcruntime140_1.dll": ROOT / "_tools" / "py311" / "vcruntime140_1.dll",
}
SYS32 = Path(r"C:\Windows\System32")


def _imported_dlls(path: Path) -> set[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return set()
    if data[:2] != b"MZ":
        return set()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return set()
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt_off = pe + 24
    magic = struct.unpack_from("<H", data, opt_off)[0]
    dd_off = opt_off + (112 if magic == 0x20B else 96)
    imp_rva = struct.unpack_from("<I", data, dd_off + 8)[0]
    if not imp_rva:
        return set()
    sections = []
    sec_off = opt_off + opt_size
    for i in range(nsec):
        o = sec_off + i * 40
        vaddr, vsize = struct.unpack_from("<II", data, o + 12)
        rawsize, raw = struct.unpack_from("<II", data, o + 16)
        sections.append((vaddr, vsize, raw, rawsize))

    def rva2off(rva):
        for vaddr, vsize, raw, rawsize in sections:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return raw + (rva - vaddr)
        return None

    out = set()
    off = rva2off(imp_rva)
    if off is None:
        return out
    while True:
        entry = data[off:off + 20]
        if len(entry) < 20 or entry == b"\0" * 20:
            break
        name_rva = struct.unpack_from("<I", entry, 12)[0]
        if name_rva:
            no = rva2off(name_rva)
            if no is not None:
                out.add(data[no:data.index(b"\0", no)].decode("ascii", "ignore").lower())
        off += 20
    return out


def _runtime_source(dll: str) -> Path | None:
    src = RUNTIME_SOURCES.get(dll)
    if src and src.is_file():
        return src
    fallback = SYS32 / dll
    return fallback if fallback.is_file() else None


def ensure_runtime_dlls() -> int:
    """把 VC++ 运行库放到每个需要它的 .pyd 旁边，返回新复制的文件数。"""
    want: dict[Path, set[str]] = {}
    for base in (ROOT / "_internal", ROOT / "_tools" / "runtime_site"):
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.suffix.lower() not in (".pyd", ".dll") or not p.is_file():
                continue
            for d in _imported_dlls(p) & RUNTIME_DLLS:
                if d in SKIP_RUNTIME or (p.parent / d).exists():
                    continue
                want.setdefault(p.parent, set()).add(d)

    copied = 0
    pending = dict(want)
    while pending:
        nxt: dict[Path, set[str]] = {}
        for d, dlls in pending.items():
            for dll in sorted(dlls):
                dst = d / dll
                if dst.exists():
                    continue
                src = _runtime_source(dll)
                if src is None:
                    print(f"  警告：找不到 {dll}，无法补齐 {dst.relative_to(ROOT)}")
                    continue
                shutil.copy2(src, dst)
                copied += 1
                for sub in _imported_dlls(dst) & RUNTIME_DLLS:
                    if sub not in SKIP_RUNTIME and not (d / sub).exists():
                        nxt.setdefault(d, set()).add(sub)
        pending = nxt
    return copied


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

    n_dll = ensure_runtime_dlls()
    if n_dll:
        print(f"  已补齐 VC++ 运行库 {n_dll} 个（greenlet 等扩展模块在裸 Windows 可启动）")

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
