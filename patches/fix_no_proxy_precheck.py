"""关掉发布前自动跑的比特浏览器代理体检，改为手动验证。

体检本身对动态 IP 代理判不准，既会误伤能用的号，也拦不住真正连不上的号，
每轮开跑前还要多等一截。总览「代理体检」按钮保留，需要时手动点。

实现上不去掉 _filter_by_proxy_health 的调用：fix_all_proxies_failed 靠它的
返回值统计本轮调度数（total_scheduled），拿掉会让「全部代理失败立刻停」失灵。
这里只把 precheck_proxy 关掉，让它进门就原样返回。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("fix_no_proxy_precheck")


def apply_no_proxy_precheck_patches() -> None:
    import publisher

    original_init = publisher.PublisherService.__init__

    def init_without_precheck(self, *a, **k):
        original_init(self, *a, **k)
        if getattr(self, "precheck_proxy", False):
            logger.info("已关闭发布前的比特代理体检（改为手动验证，总览按钮仍可用）")
        self.precheck_proxy = False

    publisher.PublisherService.__init__ = init_without_precheck  # type: ignore[method-assign]
    logger.info("已应用补丁：不再自动做代理体检")
