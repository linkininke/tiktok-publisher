"""仅按 Excel 品名语种，影响：标题/话题语言 + 小黄车展示名语言。

不改自动化流程（不包装 run_for_account）。
原流程 caption_factory 只返回 caption 字符串，小黄车名仍走配置常量；
因此 generate 后把对应语种的小黄车名写入 stash，由发布 loop 在组 Job 时读取。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("publisher")

# 最近一次 generate 产出的小黄车名（供原流程 PublishJob 使用）
_LAST_CART: dict[str, str] = {"product_id": "", "display": "", "lang": ""}

CART_PHRASES: dict[str, str] = {
    "zh": "小黄车下单",
    "en": "Shop via yellow cart",
    "th": "สั่งซื้อที่รถเข็นเหลือง",
    "id": "Beli di keranjang kuning",
    "vi": "Mua qua giỏ vàng",
    "ms": "Beli di troli kuning",
    "ja": "黄色カートで購入",
    "ko": "노란 카트로 구매",
    "ru": "Купить в жёлтой корзине",
}

LANG_LABELS: dict[str, str] = {
    "zh": "中文",
    "en": "English",
    "th": "ไทย / Thai",
    "id": "Bahasa Indonesia",
    "vi": "Tiếng Việt",
    "ms": "Bahasa Melayu",
    "ja": "日本語",
    "ko": "한국어",
    "ru": "Русский",
}

_LANG_INSTRUCTIONS: dict[str, str] = {
    "en": (
        "Write ALL 5 titles in English for TikTok Shop. "
        "Hashtags in English or common tags (#TikTokShop #fyp). "
        "May mention yellow cart / shop now naturally."
    ),
    "zh": (
        "5 条标题全部用简体中文，面向 TikTok Shop 橱窗带货；"
        "可自然提到小黄车/去购买。标签用中文或通用话题（可含 #TikTokShop #fyp）。"
    ),
    "th": (
        "เขียนชื่อเรื่องทั้ง 5 เป็นภาษาไทย สำหรับ TikTok Shop "
        "น้ำเสียงพูดคุย ชวนกดตะกร้าเหลือง/สั่งซื้อ "
        "แฮชแท็กเป็นภาษาไทยหรือสากล (#TikTokShop #fyp ได้)"
    ),
    "id": (
        "Tulis 5 judul dalam Bahasa Indonesia untuk TikTok Shop. "
        "Ajakan beli via keranjang kuning. Hashtag Indonesia/umum (#TikTokShop #fyp ok)."
    ),
    "vi": (
        "Viết cả 5 tiêu đề bằng tiếng Việt cho TikTok Shop. "
        "Kêu gọi mua qua giỏ vàng. Hashtag tiếng Việt/quốc tế (#TikTokShop #fyp được)."
    ),
    "ms": (
        "Tulis 5 tajuk dalam Bahasa Melayu untuk TikTok Shop. "
        "Galakkan beli melalui troli kuning. Hashtag Melayu/umum."
    ),
    "ja": (
        "タイトル5本はすべて日本語。TikTok Shop向け、黄色カート誘導可。"
        "ハッシュタグは日本語または一般タグ。"
    ),
    "ko": (
        "제목 5개는 모두 한국어. 틱톡샵/노란 카트 유도 가능. 해시태그는 한국어 또는 공통 태그."
    ),
    "ru": (
        "Все 5 заголовков на русском для TikTok Shop. "
        "Можно упомянуть жёлтую корзину. Хэштеги на русском или общие."
    ),
}


def normalize_lang(code: str | None) -> str:
    c = (code or "en").strip().lower().replace("_", "-")
    if c in ("zh-cn", "zh-hans", "cn", "chinese", "zh-tw", "zh-hant"):
        return "zh"
    if c in CART_PHRASES:
        return c
    if c.startswith("en"):
        return "en"
    return c if c in _LANG_INSTRUCTIONS else "en"


def detect_lang_from_text(text: str) -> str:
    """根据 Excel 品名粗检语种。"""
    s = text or ""
    if re.search(r"[\u0E00-\u0E7F]", s):
        return "th"
    if re.search(r"[\u4E00-\u9FFF]", s):
        return "zh"
    if re.search(r"[\u3040-\u30FF]", s):
        return "ja"
    if re.search(r"[\uAC00-\uD7AF]", s):
        return "ko"
    if re.search(r"[\u0400-\u04FF]", s):
        return "ru"
    if re.search(r"[ăâêôơưđĂÂÊÔƠƯĐ]", s):
        return "vi"
    return "en"


def cart_phrase_for_lang(lang: str) -> str:
    return CART_PHRASES.get(normalize_lang(lang), CART_PHRASES["en"])


def get_cart_display_for_product(product_id: str, fallback: str | None = None) -> str:
    """供发布 loop 在 caption_factory（generate）之后读取小黄车名。"""
    if _LAST_CART.get("product_id") == str(product_id) and _LAST_CART.get("display"):
        return _LAST_CART["display"]
    return (fallback or "").strip() or CART_PHRASES["zh"]


def build_prompt_for_lang(product_name: str, selling_points: str, keywords: str, lang: str) -> str:
    lang = normalize_lang(lang)
    instr = _LANG_INSTRUCTIONS.get(lang, _LANG_INSTRUCTIONS["en"])
    lang_name = LANG_LABELS.get(lang, lang)
    return f"""写 5 条 tiktok「{product_name}」橱窗带货热门标题和 5 条热门标签，然后选出一条最合适的。

商品名：{product_name}
补充卖点：{selling_points}
补充关键词：{keywords}

场景：短视频挂橱窗/小黄车，引导观众点购物车下单（TikTok Shop showcase affiliate）。
目标语言：{lang_name}（code={lang}）——必须与商品名语种一致。

语言与文案要求：
{instr}
另外：
1) 每条标题不超过 100 个字符，标题里不要写 hashtag。
2) 5 条热门标签带 #，尽量像当前热门搜索词，可含 #TikTokShop #fyp，但必须和商品相关，且整体语言与标题一致。
3) 只根据商品名写，不要假设看过视频。
4) 从 5 条标题里选出最适合的一条放入 best_title（必须与 titles 中某条完全一致）。
5) 严格输出 JSON（不要 markdown）：
{{{{"titles":["t1","t2","t3","t4","t5"],"hashtags":["#a","#b","#c","#d","#e"],"best_title":"最合适的那一条"}}}}
"""


def apply_content_i18n_patches() -> None:
    import content_gen

    original_generate = content_gen.ContentGenerator.generate

    def generate_fixed(
        self,
        video_path,
        product_id: str,
        keywords=None,
        extra_hashtags=None,
        product_name: str = "",
        selling_points=None,
    ):
        # 核心：只看 Excel 品名语种（不改发布流程）
        lang = detect_lang_from_text(product_name or "")
        lang_name = LANG_LABELS.get(lang, lang)
        cart = cart_phrase_for_lang(lang)
        logger.info(
            "品名语种 → %s (%s) | 品名=%s | 小黄车「%s」",
            lang,
            lang_name,
            (product_name or "")[:40],
            cart,
        )

        old_prompt = getattr(content_gen, "PROMPT_TEMPLATE", "")
        content_gen.PROMPT_TEMPLATE = build_prompt_for_lang(
            "{product_name}", "{selling_points}", "{keywords}", lang
        )

        system_msg = (
            f"你是 TikTok Shop 橱窗带货文案专家（目标语言：{lang_name}/{lang}）。"
            "只输出合法 JSON，不要解释。"
        )
        client = getattr(self, "_client", None)
        completions = getattr(getattr(client, "chat", None), "completions", None)
        real_create = getattr(completions, "create", None) if completions is not None else None

        def create_fixed(*args, **kwargs):
            msgs = kwargs.get("messages")
            if isinstance(msgs, list):
                rewritten = []
                for m in msgs:
                    if isinstance(m, dict) and m.get("role") == "system":
                        rewritten.append({**m, "content": system_msg})
                    else:
                        rewritten.append(m)
                kwargs = {**kwargs, "messages": rewritten}
            return real_create(*args, **kwargs)

        if real_create is not None:
            completions.create = create_fixed

        try:
            result = original_generate(
                self,
                video_path,
                product_id,
                keywords=keywords,
                extra_hashtags=extra_hashtags,
                product_name=product_name,
                selling_points=selling_points,
            )
            if isinstance(result, dict):
                result["language"] = lang
                result["product_display_name"] = cart
                _LAST_CART["product_id"] = str(product_id)
                _LAST_CART["display"] = cart
                _LAST_CART["lang"] = lang
                logger.info(
                    "文案语种=%s | 小黄车「%s」| caption前80=%s",
                    lang,
                    cart,
                    str(result.get("caption") or "")[:80].replace("\n", " "),
                )
            return result
        finally:
            content_gen.PROMPT_TEMPLATE = old_prompt
            if real_create is not None:
                try:
                    completions.create = real_create
                except Exception:
                    pass

    content_gen.ContentGenerator.generate = generate_fixed  # type: ignore[method-assign]
    logger.info("已应用补丁：按 Excel 品名语种生成标题/话题，并同步小黄车展示名（不改自动化流程）")
