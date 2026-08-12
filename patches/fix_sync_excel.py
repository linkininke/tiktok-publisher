"""兼容入口：强制加载 patches/sync_excel.py（覆盖 runtime_site 旧 .pyc）。"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

logger = logging.getLogger("publisher")


def _force_load_patches_sync_excel() -> object:
    root = Path(__file__).resolve().parent.parent
    patches_dir = root / "patches"
    target = patches_dir / "sync_excel.py"
    if not target.is_file():
        raise FileNotFoundError(f"缺少重写映射模块: {target}")

    # 保证后续 import sync_excel 也优先命中 patches
    s = str(patches_dir)
    while s in sys.path:
        sys.path.remove(s)
    sys.path.insert(0, s)

    # 若已加载旧模块，先卸掉再装重写版
    old = sys.modules.get("sync_excel")
    if old is not None:
        old_file = (getattr(old, "__file__", "") or "").replace("\\", "/")
        if "/patches/" in old_file or old_file.endswith("/patches/sync_excel.py"):
            return old
        del sys.modules["sync_excel"]

    spec = importlib.util.spec_from_file_location("sync_excel", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载: {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sync_excel"] = mod
    spec.loader.exec_module(mod)
    return mod


def apply_sync_excel_patches() -> None:
    """强制启用 patches 中的一对多映射逻辑。"""
    sync_excel = _force_load_patches_sync_excel()
    path = getattr(sync_excel, "__file__", "") or ""
    if "patches" not in path.replace("\\", "/"):
        raise RuntimeError(
            f"sync_excel 未加载到 patches 重写版，当前: {path}"
        )
    logger.info("已启用重写版 Excel 映射逻辑: %s", path)


def resync_from_config(root: Path | None = None) -> list:
    root = root or Path(__file__).resolve().parent.parent
    sys.path[:0] = [
        str(root / "patches"),
        str(root / "_tools" / "runtime_site"),
        str(root / "_internal"),
        str(root),
    ]
    apply_sync_excel_patches()
    import config_loader
    import sync_excel

    config_loader.ROOT = root
    cfg = config_loader.load_config(root / "config.yaml")
    excel = Path(cfg["paths"]["excel_file"])
    videos_dir = Path(cfg["paths"]["videos_dir"])
    map_file = root / cfg["paths"]["mapping_file"]
    if not excel.is_absolute():
        excel = root / excel
    if not videos_dir.is_absolute():
        videos_dir = root / videos_dir
    return sync_excel.sync(excel, map_file, videos_dir)
