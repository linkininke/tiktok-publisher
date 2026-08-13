"""挂车最后一步「商品名称」输入框：不依赖界面语言定位，且填完必须读回校验。

原版 _fill_product_display_name 只认中文界面：
    selectors = ('input[placeholder*="商品名称"]', 'input[placeholder*="名称"]',
                 'textarea[placeholder*="名称"]', '[role="dialog"] input[type="text"]',
                 'input[type="text"]')
英文（新加坡区）界面下前三条匹配不到；第四条依赖 role="dialog"，而 Studio 的弹窗
是 TUXModal，不一定带这个属性；第五条 input[type="text"] 取 .last，会拿到整页
最后一个输入框。全部落空后走 JS 兜底，而兜底找 innerText=='商品名称' 的标签，
英文界面同样找不到，就退回 document.body 取整页第一个 input 写进去 —— 写错元素
却照样 return True，于是日志打出「商品展示名已改为」，页面上其实没改。

这里改成：
1. 在最上层弹窗里按结构特征挑输入框（长度上限、已预填商品标题、就近标签多语言词表），
   排除搜索框，不依赖界面是中文还是英文。
2. 填完读回 value 校验，对不上就换键盘输入再试；仍对不上返回 False，
   让上层打出「未能修改商品展示名」而不是假成功。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("tiktok_uploader")

_MARK = "data-ttp-cart-name"

# 各语言「商品名称 / 名称」的说法，命中只是加分项，不命中也能靠结构特征选中
_NAME_WORDS = [
    "商品名称", "商品名稱", "名称", "名稱", "商品名",
    "product name", "display name", "item name", "name",
    "ชื่อสินค้า", "ชื่อ",
    "nama produk", "nama",
    "tên sản phẩm", "tên",
    "상품명", "이름",
    "название",
]

# 搜索框要排除掉：上一步选商品用的就是它
_SEARCH_WORDS = r"搜索|搜尋|search|ค้นหา|cari|tìm|検索|검색|поиск"

_FIND_JS = r"""
(cfg) => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width >= 60 && r.height >= 12 && s.visibility !== 'hidden'
      && s.display !== 'none' && s.opacity !== '0';
  };
  document.querySelectorAll('[' + cfg.mark + ']').forEach(e => e.removeAttribute(cfg.mark));

  // 弹窗选择器与挂车补丁保持一致：Studio 的弹窗常常只有 TUXModal 类名，没有 role=dialog
  const modalSel = '[role="dialog"], [class*="TUXModal"], [class*="Modal"], [class*="Dialog"]';
  const modals = [...document.querySelectorAll(modalSel)].filter(vis);
  const bad = new RegExp(cfg.searchWords, 'i');

  // 逐个弹窗都找，再兜底整页：只认「最后一个弹窗」的话，遇到末尾那层没有输入框的
  // 遮罩/portal 容器就会一个候选都拿不到（实测就是这么挂的）。
  const okInput = (el) => {
    if (!vis(el) || el.disabled || el.readOnly) return false;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'input' && !['text', 'search', ''].includes(type)) return false;
    const hint = [el.placeholder, el.getAttribute('aria-label'), el.name, el.id]
      .filter(Boolean).join(' ');
    if (bad.test(hint)) return false;
    return true;
  };

  const seen = new Set();
  const cands = [];
  for (const sc of [...modals, document.body]) {
    for (const el of sc.querySelectorAll('input, textarea')) {
      if (seen.has(el)) continue;
      seen.add(el);
      if (!okInput(el)) continue;
      el.__inModal = sc !== document.body;
      cands.push(el);
    }
  }
  if (!cands.length) {
    return {
      empty: true,
      modals: modals.length,
      inputsOnPage: document.querySelectorAll('input, textarea').length,
    };
  }

  const nearText = (el) => {
    let p = el.parentElement, s = '';
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) s += ' ' + (p.innerText || '').slice(0, 120);
    return s.toLowerCase();
  };
  const score = (el) => {
    let s = 0;
    const hint = [el.placeholder, el.getAttribute('aria-label')]
      .filter(Boolean).join(' ').toLowerCase();
    if (cfg.words.some(w => hint.includes(w))) s += 50;
    const ml = parseInt(el.getAttribute('maxlength') || '0', 10);
    if (ml >= 10 && ml <= 100) s += 30;          // 展示名字段有长度上限，搜索框一般没有
    if ((el.value || '').trim()) s += 20;        // 这一步默认预填了商品标题
    if (cfg.words.some(w => nearText(el).includes(w))) s += 25;
    if (el.__inModal) s += 40;                   // 弹窗里的优先于整页兜底捞到的
    return s;
  };

  const ranked = cands.map(el => ({ el, s: score(el) })).sort((a, b) => b.s - a.s);
  const best = ranked[0];
  // 兜底到整页时要求有把握（长度上限/预填/就近标签任一），否则宁可不写，免得写错框
  if (!best.el.__inModal && best.s < 30) {
    return { empty: true, modals: modals.length, inputsOnPage: cands.length, weak: best.s };
  }
  best.el.setAttribute(cfg.mark, '1');
  return {
    score: best.s,
    candidates: cands.length,
    inModal: !!best.el.__inModal,
    modals: modals.length,
    maxlength: parseInt(best.el.getAttribute('maxlength') || '0', 10) || 0,
    before: (best.el.value || '').slice(0, 60),
    placeholder: (best.el.placeholder || '').slice(0, 40),
  };
}
"""


def _read_back(loc) -> str:
    try:
        return (loc.input_value(timeout=1500) or "").strip()
    except Exception:
        return ""


def _locate(page, tries: int = 16, interval_ms: int = 500):
    """弹窗内容是异步渲染的，点完「下一步」后要等它出来，不能只看一眼就走。"""
    last = None
    for i in range(max(1, tries)):
        try:
            info = page.evaluate(
                _FIND_JS,
                {"mark": _MARK, "words": _NAME_WORDS, "searchWords": _SEARCH_WORDS},
            )
        except Exception as e:
            logger.warning("定位商品名称输入框出错: %s", e)
            return None
        if info and not info.get("empty"):
            if i:
                logger.info("商品名称输入框在第 %d 次探测（约 %.1fs）后出现", i + 1, i * interval_ms / 1000)
            return info
        last = info
        try:
            page.wait_for_timeout(interval_ms)
        except Exception:
            break
    logger.warning(
        "等不到商品名称输入框：可见弹窗=%s 页面输入框=%s 兜底最高分=%s",
        (last or {}).get("modals"),
        (last or {}).get("inputsOnPage"),
        (last or {}).get("weak", "—"),
    )
    return None


def fill_product_display_name_fixed(self, page, display_name=None) -> bool:
    want = str(display_name or "小黄车下单").strip()
    info = _locate(page)
    if not info:
        return False

    limit = int(info.get("maxlength") or 0)
    text = want[:limit] if limit and len(want) > limit else want
    if text != want:
        logger.warning("商品名称上限 %d 字，「%s」截断为「%s」", limit, want, text)
    logger.info(
        "商品名称输入框：弹窗内=%s 候选=%d 匹配分=%d 占位符=%r 原值=%r",
        info.get("inModal"), info.get("candidates"), info.get("score"),
        info.get("placeholder"), info.get("before"),
    )

    loc = page.locator(f"[{_MARK}='1']").first
    actual = ""
    for attempt in (1, 2):
        try:
            loc.click(timeout=1500)
            if attempt == 1:
                loc.fill("")
                loc.fill(text)
            else:
                # React 受控组件对 fill 不一定响应，换成真键盘输入
                loc.press("Control+a")
                page.keyboard.press("Delete")
                page.keyboard.insert_text(text)
        except Exception as e:
            logger.warning("第 %d 次填写商品名称失败: %s", attempt, e)
        actual = _read_back(loc)
        if actual == text:
            logger.info("商品名称已写入并校验通过：「%s」", actual)
            return True
        logger.warning("第 %d 次写入后校验不符：期望「%s」实际「%s」", attempt, text, actual)

    logger.warning("商品展示名写入失败，期望「%s」实际「%s」", text, actual)
    return False


# 「商品名称」这一步的标题，各语言说法。原版只等中文，非中文界面要白等 10 秒，
# 还会打出「未进入商品名称编辑页」的假警告（其实后面照样填）。
_NAME_PAGE_TEXTS = (
    "商品名称", "商品名稱", "将出现在你的视频中",
    "Product name", "Product title", "will appear on your video",
    "ชื่อสินค้า", "Nama produk", "Tên sản phẩm", "상품명", "商品名",
)


def apply_cart_display_fill_patches() -> None:
    import tiktok_uploader

    cls = tiktok_uploader.TikTokStudioUploader
    if getattr(cls, "_cart_display_fill_patched", False):
        return

    original_attach = cls._attach_product
    original_wait = cls._wait_for_texts

    def attach_product_lastmile(self, page, product_id, product_display_name=None):
        """填之前再解析一次译名：不依赖「生成时记下 → 循环里取出」那段跨函数传递。

        只要 content_gen 这一轮为该商品记过译名，就以它为准；没记过才用传进来的值。
        """
        final = product_display_name
        try:
            from fix_content_i18n import get_cart_display_for_product

            final = get_cart_display_for_product(str(product_id), product_display_name)
        except Exception as e:
            logger.warning("取本轮小黄车译名失败，沿用传入值「%s」: %s", product_display_name, e)
        if final and final != product_display_name:
            logger.info(
                "挂车展示名改用本轮译名：传入「%s」→ 实用「%s」",
                product_display_name,
                final,
            )
        else:
            logger.info("挂车展示名：「%s」", final)
        return original_attach(self, page, product_id, final)

    def wait_for_texts_i18n(self, page, texts, *a, **k):
        """等「商品名称」页时把各语种说法一起等上，别只认中文。"""
        try:
            seq = tuple(texts)
        except TypeError:
            seq = (texts,)
        if any(t in ("商品名称", "将出现在你的视频中") for t in seq):
            seq = tuple(dict.fromkeys(seq + _NAME_PAGE_TEXTS))
        return original_wait(self, page, seq, *a, **k)

    cls._fill_product_display_name = fill_product_display_name_fixed  # type: ignore[method-assign]
    cls._attach_product = attach_product_lastmile  # type: ignore[method-assign]
    cls._wait_for_texts = wait_for_texts_i18n  # type: ignore[method-assign]
    cls._cart_display_fill_patched = True  # type: ignore[attr-defined]
    logger.info("已应用补丁：商品展示名按结构定位 + 填写前重新解析译名 + 名称页多语种等待")
