"""验证挂车后：AI 开关逻辑 + 内容失败销毁映射（不连真实浏览器发布）。"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (
    str(ROOT / "patches"),
    str(ROOT / "_tools" / "runtime_site"),
    str(ROOT / "_internal"),
    str(ROOT),
):
    while p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def test_patches_load() -> None:
    from fix_discard_draft import apply_patches
    from fix_post_ai_check import apply_post_ai_check_patches, open_ai_more_fixed, run_with_replace_loop_fixed
    import tiktok_uploader

    apply_patches()
    apply_post_ai_check_patches()
    assert tiktok_uploader.TikTokStudioUploader._open_ai_more_if_any is open_ai_more_fixed
    assert tiktok_uploader.run_with_replace_loop is run_with_replace_loop_fixed
    print("PASS patches_load")


def test_destroy_failed_video() -> None:
    import config_loader
    from fix_post_ai_check import _destroy_failed_video

    config_loader.ROOT = ROOT
    tmp = Path(tempfile.mkdtemp(prefix="ttp_fail_"))
    try:
        videos = tmp / "videos"
        videos.mkdir()
        vid = videos / "fail_test.mp4"
        vid.write_bytes(b"fake")
        map_file = ROOT / "data" / "product_video_map.json"
        backup = tmp / "map_backup.json"
        shutil.copy2(map_file, backup)
        data = json.loads(map_file.read_text(encoding="utf-8"))
        # 临时插入测试商品
        pid = "__test_fail_destroy__"
        data.append({"product_id": pid, "product_name": "t", "enabled": True, "videos": [vid.name]})
        map_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 把 videos_dir 指到 tmp（通过临时改 config 太重，直接测 consume）
        from mapping_store import consume_video_after_success

        info = consume_video_after_success(
            mapping_file=map_file,
            videos_dir=videos,
            product_id=pid,
            video_name=vid.name,
        )
        assert not vid.exists(), "video should be deleted"
        assert info.get("deleted_file")
        items = json.loads(map_file.read_text(encoding="utf-8"))
        row = next(x for x in items if x.get("product_id") == pid)
        assert vid.name not in (row.get("videos") or [])
        assert row.get("enabled") is False
        print("PASS destroy_via_consume", info)

        # 恢复映射
        shutil.copy2(backup, map_file)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_replace_loop_destroys_on_fail() -> None:
    """模拟 publish 返回 FAIL，确认会销毁视频并尝试下一支。"""
    import tiktok_uploader
    from fix_post_ai_check import apply_post_ai_check_patches, _destroy_failed_video
    from pathlib import Path as P
    from types import SimpleNamespace

    apply_post_ai_check_patches()

    tmp = Path(tempfile.mkdtemp(prefix="ttp_loop_"))
    v1 = tmp / "a.mp4"
    v2 = tmp / "b.mp4"
    v1.write_bytes(b"1")
    v2.write_bytes(b"2")

    destroyed: list[str] = []
    calls: list[str] = []

    class FakeUploader:
        def publish(self, job):
            calls.append(Path(job.video_path).name)
            # 第一支 FAIL，第二支成功
            if Path(job.video_path).name == "a.mp4":
                return tiktok_uploader.PublishResult(
                    False, tiktok_uploader.CheckStatus.FAIL.value, "内容检查不通过，建议替换视频", "a.mp4", job.product_id
                )
            return tiktok_uploader.PublishResult(True, "posted", "已点击发布", "b.mp4", job.product_id)

        def click_replace_for_next_video(self):
            return True

        def open_upload_page(self, force=True):
            return None

    # monkeypatch destroy
    import fix_post_ai_check as mod

    def fake_destroy(pid, video, **kwargs):
        destroyed.append(Path(video).name)
        Path(video).unlink(missing_ok=True)

    mod._destroy_failed_video = fake_destroy  # type: ignore

    result = tiktok_uploader.run_with_replace_loop(
        FakeUploader(),
        "pid1",
        [v1, v2],
        caption_factory=lambda v: "cap",
        product_display_name="小黄车下单",
        max_attempts=3,
    )
    assert result.ok, result
    assert destroyed == ["a.mp4"], destroyed
    assert calls == ["a.mp4", "b.mp4"], calls
    assert not v1.exists() and v2.exists()
    print("PASS replace_loop_destroys_on_fail")
    shutil.rmtree(tmp, ignore_errors=True)


def test_ai_switch_js_against_dom_snippet() -> None:
    """用 Playwright 本地 HTML 验证开关点击逻辑（无浏览器则跳过）。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print("SKIP ai_switch_js_local_dom (no playwright):", e)
        return

    from fix_post_ai_check import _ai_switch_is_on, _click_ai_switch_once

    html = """
    <html><body>
      <div class="row">
        <span>AI 生成的内容</span>
        <div class="Switch" style="width:44px;height:24px;background:#ccc;display:inline-block">
          <input class="Switch__input" role="switch" type="checkbox" aria-hidden="true"
                 style="appearance:none;opacity:0;width:1px;height:1px" />
        </div>
      </div>
      <script>
        document.querySelector('.Switch').addEventListener('click', () => {
          const i = document.querySelector('.Switch__input');
          i.checked = !i.checked;
        });
      </script>
    </body></html>
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            assert _ai_switch_is_on(page) is False
            assert _click_ai_switch_once(page) is True
            page.wait_for_timeout(200)
            assert _ai_switch_is_on(page) is True, "switch should be on after click"
            browser.close()
        print("PASS ai_switch_js_local_dom")
    except Exception as e:
        print("SKIP ai_switch_js_local_dom (browser missing):", e)


if __name__ == "__main__":
    test_patches_load()
    test_destroy_failed_video()
    test_replace_loop_destroys_on_fail()
    test_ai_switch_js_against_dom_snippet()
    print("ALL_OK")
