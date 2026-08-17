"""仅按 Excel 品名语种，影响：标题/话题语言 + 小黄车展示名语言。

不改自动化流程（不包装 run_for_account）。
原流程 caption_factory 只返回 caption 字符串，小黄车名仍走配置常量；
因此 generate 后把对应语种的小黄车名写入 stash，由发布 loop 在组 Job 时读取。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

logger = logging.getLogger("publisher")

# generate 产出的小黄车名（供原流程 PublishJob 使用）。
# 必须按线程隔离：并发发布时每个账号一个线程，若共用一个全局值，
# 甲号写完还没读走就被乙号覆盖，取值时商品号对不上，就退回配置默认值「小黄车下单」。
_local = threading.local()
# 跨线程兜底：同一商品号语种必然相同，按商品号存不会串号。
_CART_BY_PRODUCT: dict[str, dict[str, str]] = {}
_CART_LOCK = threading.Lock()

CART_PHRASES: dict[str, str] = {
    "zh": "小黄车下单",
    "en": "Shop now",
    "th": "สั่งซื้อที่รถเข็นเหลือง",
    "id": "Beli di keranjang kuning",
    "vi": "Mua qua giỏ vàng",
    "ms": "Beli di troli kuning",
    "ja": "黄色カートで購入",
    "ko": "노란 카트로 구매",
    "ru": "Купить в жёлтой корзине",
}

# 设置页「小黄车底稿(中文)」的出厂值。保持默认就用上面这张调好的词表
# （英文那条是你特意指定的 Shop now）；改成别的文案才去翻译。
DEFAULT_CART_DRAFT = "小黄车下单"

# 挂车弹窗那个输入框有 30 字上限
CART_MAX_CHARS = 30
# 翻一条底稿给的 token 预算。推理型模型（deepseek-v4-flash）光思考就要 1000+，
# 泰文实测 2048 还会被思考吃满，给到 4096 才一次过。这只是上限，按实际输出计费，
# 给宽一点不多花钱，却能省掉一次白跑的请求（约 20 秒）。见 _translate_draft。
CART_TRANSLATE_TOKENS = 4096

_DRAFT_CACHE: dict[str, Any] = {"mtime": None, "text": DEFAULT_CART_DRAFT}
_PHRASE_CACHE: dict[str, str] = {}
_PHRASE_LOCK = threading.Lock()
_PHRASE_FILE_LOADED = False
# 翻译失败过的「语种||底稿」，本次运行内不再重试，避免每条视频都打一次接口
_TRANSLATE_FAILED: set[str] = set()

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

# 这些指令在提示词里的位置比下面的标签配比要求更靠前。以前每条都写着
# 「可含 #TikTokShop #fyp」，等于一边要求最多 1 条泛标签、一边点名推荐泛标签，
# 模型自然照着更靠前的那句办。所以这里只管语种和口吻，标签规则统一交给配比要求。
_LANG_INSTRUCTIONS: dict[str, str] = {
    "en": (
        "Write ALL 5 titles in English for TikTok Shop. "
        "Hashtags in English. May mention yellow cart / shop now naturally."
    ),
    "zh": (
        "5 条标题全部用简体中文，面向 TikTok Shop 橱窗带货；"
        "可自然提到小黄车/去购买。标签用中文。"
    ),
    "th": (
        "เขียนชื่อเรื่องทั้ง 5 เป็นภาษาไทย สำหรับ TikTok Shop "
        "น้ำเสียงพูดคุย ชวนกดตะกร้าเหลือง/สั่งซื้อ "
        "แฮชแท็กเป็นภาษาไทย"
    ),
    "id": (
        "Tulis 5 judul dalam Bahasa Indonesia untuk TikTok Shop. "
        "Ajakan beli via keranjang kuning. Hashtag Bahasa Indonesia."
    ),
    "vi": (
        "Viết cả 5 tiêu đề bằng tiếng Việt cho TikTok Shop. "
        "Kêu gọi mua qua giỏ vàng. Hashtag tiếng Việt."
    ),
    "ms": (
        "Tulis 5 tajuk dalam Bahasa Melayu untuk TikTok Shop. "
        "Galakkan beli melalui troli kuning. Hashtag Bahasa Melayu."
    ),
    "ja": (
        "タイトル5本はすべて日本語。TikTok Shop向け、黄色カート誘導可。"
        "ハッシュタグは日本語。"
    ),
    "ko": (
        "제목 5개는 모두 한국어. 틱톡샵/노란 카트 유도 가능. 해시태그는 한국어."
    ),
    "ru": (
        "Все 5 заголовков на русском для TikTok Shop. "
        "Можно упомянуть жёлтую корзину. Хэштеги на русском."
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


def _clip_phrase(text: str) -> str:
    """裁到输入框的 30 字上限。有空格的语种按词界裁，别切出半个单词。"""
    s = (text or "").strip()
    if len(s) <= CART_MAX_CHARS:
        return s
    head = s[:CART_MAX_CHARS]
    if " " in head:
        trimmed = head.rsplit(" ", 1)[0].rstrip(" ,.;:!-")
        if len(trimmed) >= CART_MAX_CHARS // 2:
            return trimmed
    return head


def _root():
    from pathlib import Path

    try:
        import config_loader

        return Path(getattr(config_loader, "ROOT", None) or Path(__file__).resolve().parent.parent)
    except Exception:
        return Path(__file__).resolve().parent.parent


def cart_draft() -> str:
    """读设置页里的「小黄车底稿(中文)」，配置文件变了就重读。"""
    cfg_file = _root() / "config.yaml"
    try:
        mtime = cfg_file.stat().st_mtime
    except Exception:
        return _DRAFT_CACHE["text"]
    if _DRAFT_CACHE["mtime"] == mtime:
        return _DRAFT_CACHE["text"]
    try:
        import config_loader

        cfg = config_loader.load_config(cfg_file) or {}
        text = str((cfg.get("publish") or {}).get("product_display_name") or "").strip()
    except Exception as e:
        logger.warning("读取小黄车底稿失败，用默认「%s」: %s", DEFAULT_CART_DRAFT, e)
        text = DEFAULT_CART_DRAFT
    _DRAFT_CACHE["mtime"] = mtime
    _DRAFT_CACHE["text"] = text or DEFAULT_CART_DRAFT
    return _DRAFT_CACHE["text"]


def _cache_file():
    return _root() / "data" / "cart_phrase_cache.json"


def _load_phrase_cache() -> None:
    global _PHRASE_FILE_LOADED
    if _PHRASE_FILE_LOADED:
        return
    _PHRASE_FILE_LOADED = True
    try:
        import json

        f = _cache_file()
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _PHRASE_CACHE.update({str(k): str(v) for k, v in data.items()})
    except Exception as e:
        logger.debug("读取小黄车译文缓存失败: %s", e)


def _save_phrase_cache() -> None:
    try:
        import json

        f = _cache_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps(_PHRASE_CACHE, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.debug("写入小黄车译文缓存失败: %s", e)


def _translate_draft(gen: Any, draft: str, lang: str) -> str:
    """把自定义底稿翻成目标语种。失败返回空串，由调用方退回词表。

    默认模型 deepseek-v4-flash 是推理型：token 先喂给 reasoning_content，正文最后才吐。
    预算给少了会 finish_reason=length、content 恒为空串——译文永远出不来，
    表现就是「改了底稿没反应，英文一直是 Shop now」。实测这条提示光思考就要烧
    1000~3000 token（泰文最贵），所以预算给到 CART_TRANSLATE_TOKENS，并且只对
    「预算被思考吃满」这一种失败加倍重试一次；模型正常收尾却给空串则不重试，
    重试也是同样结果。译文一旦拿到就落盘缓存，一条底稿只付一次钱。
    """
    client = getattr(gen, "_client", None)
    completions = getattr(getattr(client, "chat", None), "completions", None)
    if completions is None:
        # 懒加载客户端还没建好（正常流程里 generate 会先 _ensure_client）
        logger.warning("翻译小黄车底稿跳过：DeepSeek 客户端未就绪（退回内置词表）")
        return ""
    lang_name = LANG_LABELS.get(lang, lang)
    messages = [
        {
            "role": "system",
            "content": "你是电商 CTA 短语翻译器。只输出译文本身，不要引号、不要解释、不要标点结尾。",
        },
        {
            "role": "user",
            "content": (
                f"把这句 TikTok Shop 小黄车引导语翻译成{lang_name}（code={lang}）：\n"
                f"{draft}\n\n"
                f"要求：口语化、像按钮文案，不超过 {CART_MAX_CHARS} 个字符，只输出一行译文。"
            ),
        },
    ]

    def ask(max_tokens: int) -> tuple[str, str]:
        resp = completions.create(
            model=getattr(gen, "model", None) or "deepseek-chat",
            messages=messages,
            temperature=0.3,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        text = (getattr(choice.message, "content", None) or "").strip()
        return text, str(getattr(choice, "finish_reason", "") or "")

    out, reason = "", ""
    for attempt, budget in enumerate((CART_TRANSLATE_TOKENS, CART_TRANSLATE_TOKENS * 2)):
        try:
            out, reason = ask(budget)
        except Exception as e:
            logger.warning("翻译小黄车底稿失败（退回内置词表）: %s", e)
            return ""
        if out or reason != "length":
            break
        if attempt == 0:
            logger.warning(
                "翻译小黄车底稿：%s 个 token 全被模型的思考过程吃掉了（finish_reason=length），"
                "改用 %s 再试一次",
                budget,
                budget * 2,
            )

    # 模型可能返回空串、纯空白或前面带空行（推理型模型尤其常见），
    # 取第一条非空行；一条都没有就当失败，交给调用方退回词表。
    lines = [ln.strip().strip("\"'“”「」 ") for ln in out.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        logger.warning(
            "翻译小黄车底稿返回空内容（退回内置词表）finish_reason=%s", reason or "-"
        )
        return ""
    return _clip_phrase(lines[0])


def _resolve_cart_phrase_inner(gen: Any, lang: str) -> str:
    lang = normalize_lang(lang)
    draft = cart_draft()
    if lang == "zh":
        return draft or CART_PHRASES["zh"]
    if not draft or draft == DEFAULT_CART_DRAFT:
        return cart_phrase_for_lang(lang)

    key = f"{lang}||{draft}"
    with _PHRASE_LOCK:
        _load_phrase_cache()
        hit = _PHRASE_CACHE.get(key)
        if not hit and key in _TRANSLATE_FAILED:
            # 这一轮已经翻失败过，别每条视频都去撞一次接口
            return cart_phrase_for_lang(lang)
    if hit:
        return hit

    out = _translate_draft(gen, draft, lang)
    if not out:
        with _PHRASE_LOCK:
            _TRANSLATE_FAILED.add(key)
        return cart_phrase_for_lang(lang)
    with _PHRASE_LOCK:
        _PHRASE_CACHE[key] = out
        _save_phrase_cache()
    logger.info("小黄车底稿「%s」→ %s：「%s」（已缓存）", draft, lang, out)
    return out


def resolve_cart_phrase(gen: Any, lang: str) -> str:
    """底稿是默认值就用内置词表；被改过就翻译它，译文按「底稿+语种」缓存到磁盘。

    这一步只是决定挂车上显示什么文案，绝不能让它把整条发布流程带崩，
    所以任何意外都兜到内置词表。
    """
    try:
        return _resolve_cart_phrase_inner(gen, lang)
    except Exception as e:
        logger.warning("解析小黄车文案出错（退回内置词表）: %s", e)
        try:
            return cart_phrase_for_lang(lang)
        except Exception:
            return CART_PHRASES["en"]


def remember_cart_display(product_id: str, display: str, lang: str) -> None:
    _local.cart = {"product_id": str(product_id), "display": display, "lang": lang}
    with _CART_LOCK:
        _CART_BY_PRODUCT[str(product_id)] = {"display": display, "lang": lang}


def get_cart_display_for_product(product_id: str, fallback: str | None = None) -> str:
    """供发布 loop 在 caption_factory（generate）之后读取小黄车名。"""
    pid = str(product_id)
    mine = getattr(_local, "cart", None)
    if isinstance(mine, dict) and mine.get("product_id") == pid and mine.get("display"):
        return str(mine["display"])
    with _CART_LOCK:
        row = _CART_BY_PRODUCT.get(pid)
    if row and row.get("display"):
        return str(row["display"])
    final = (fallback or "").strip() or CART_PHRASES["zh"]
    logger.warning("小黄车展示名没命中本轮译名，退回默认「%s」product=%s", final, pid)
    return final


# 太宽的标签：作品数动辄千万上亿，进的是所有人的大池子，带不来会下单的人。
# 真机实测的作品数：#TikTokShop 1.5 亿、#Singapore 1010 万、#MustHave 400 万。
_GENERIC_TAGS = {
    "fyp", "fypシ", "fypage", "fypdongggggggg", "foryou", "foryoupage",
    "foryourpage", "viral", "viralvideo", "viraltiktok", "trending", "trend",
    "tiktok", "tiktokviral", "musthave", "musthaves", "explore", "explorepage",
    "xyzbca", "capcut", "duet", "tiktokmademebuyit", "tiktokfinds",
    # 光国名和光 TikTokShop 没有购买意图，换成 #tiktokshopsingapore 这类才有
    "tiktokshop", "singapore", "sgtiktok",
}
_CAPTION_TAG_RE = re.compile(r"#[^\s#]+")


def required_tags() -> list[str]:
    """每条视频都必须带的标签，由 content_gen.REQUIRED_HASHTAGS 定（#tiktok #fyp）。

    这两条是刻意保留的泛标签，不能被下面的裁剪当成占名额的杂标签删掉。
    """
    try:
        import content_gen

        return [str(t) for t in (getattr(content_gen, "REQUIRED_HASHTAGS", ()) or ())]
    except Exception:
        return ["#tiktok", "#fyp"]


def too_broad_tags() -> set[str]:
    """太宽的标签（不含必带的 #tiktok #fyp），给上传端选词时排除用。"""
    return set(_GENERIC_TAGS) - {t.lstrip("#").lower() for t in required_tags()}


def trim_generic_tags(caption: str) -> str:
    """删掉太宽的标签，但必带标签和精准标签一条不动。"""
    keep = {t.lstrip("#").lower() for t in required_tags()}
    dropped: list[str] = []

    def repl(m: "re.Match[str]") -> str:
        body = m.group(0).lstrip("#").lower()
        if body in keep or body not in _GENERIC_TAGS:
            return m.group(0)
        dropped.append(m.group(0))
        return ""

    out = _CAPTION_TAG_RE.sub(repl, caption or "")
    if not dropped:
        return caption
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    logger.info("去掉太宽的标签 %s（必带 %s 保留）", dropped, sorted(keep))
    return out.strip()


# ---------------- 种子词：给面板用的查询词，不是最终标签 ----------------
# 让模型凭空说 5 条精准热门标签它做不到（实测只会给 #TikTokShop #Singapore
# 这种安全牌）；但「这个品名属于哪个品类、什么属性、什么场景」是抽取题，
# 它做得很稳。真实标签一律由 Studio 的联想面板提供，那里每条都带真实作品数。
SEED_COUNT = 6
SEED_TOKENS = 4096
_SEEDS_BY_PRODUCT: dict[str, list[str]] = {}
_SEED_LOCK = threading.Lock()
_SEED_DISK_LOADED = False
_SEED_DISK: dict[str, list[str]] = {}
_SEED_FAILED: set[str] = set()
_WORD_RE = re.compile(r"[0-9A-Za-z\u00c0-\uffff]+")


def _seed_cache_file():
    from pathlib import Path

    try:
        import config_loader

        root = Path(getattr(config_loader, "ROOT", None) or ".")
    except Exception:
        root = Path(__file__).resolve().parent.parent
    return root / "data" / "seed_words_cache.json"


def _load_seed_disk() -> None:
    global _SEED_DISK_LOADED
    if _SEED_DISK_LOADED:
        return
    _SEED_DISK_LOADED = True
    try:
        import json

        p = _seed_cache_file()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list):
                        _SEED_DISK[str(k)] = [str(x) for x in v if str(x).strip()]
    except Exception as e:
        logger.debug("种子词缓存读取跳过: %s", e)


def _save_seed_disk() -> None:
    try:
        import json

        p = _seed_cache_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(_SEED_DISK, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug("种子词缓存落盘跳过: %s", e)


def seeds_from_name(product_name: str) -> list[str]:
    """接口不可用时的兜底：直接把品名切成词。

    「Mamee Monster snack」→ mameemonster / mamee / monster / snack，
    虽然不含场景人群，但至少是贴着这个品的词，比泛标签强。
    """
    words = [w.lower() for w in _WORD_RE.findall(product_name or "") if len(w) > 1]
    out: list[str] = []
    if len(words) >= 2:
        out.append("".join(words[:2]))
    for w in words:
        if w not in out:
            out.append(w)
    return out[:SEED_COUNT]


def _seed_prompt(product_name: str, lang: str) -> str:
    lang_name = LANG_LABELS.get(lang, lang)
    return f"""从商品名提取用于在 TikTok 里搜话题的种子词。这是抽取任务，不要创作。

商品名：{product_name}

输出 {SEED_COUNT} 个词，按这个结构：
- 2 个品类词：一个尽量具体，一个稍宽一档
- 2 个属性词：口味 / 材质 / 规格 / 品牌
- 2 个场景或人群词：什么时候吃用、谁会买

每个词必须是一个连写的小写词：不带 #，不带空格、连字符和标点。
语言为 {lang_name}（code={lang}），与商品名语种一致。
只输出 JSON（不要 markdown）：{{"seeds":["w1","w2","w3","w4","w5","w6"]}}
"""


def _ask_seed_words(gen, product_name: str, lang: str) -> list[str]:
    client = getattr(gen, "_client", None)
    if client is None:
        return []
    import json

    resp = client.chat.completions.create(
        model=getattr(gen, "model", None) or "deepseek-v4-flash",
        messages=[
            {"role": "system", "content": "你做关键词抽取，只输出合法 JSON。"},
            {"role": "user", "content": _seed_prompt(product_name, lang)},
        ],
        max_tokens=SEED_TOKENS,
        extra_body={"thinking": {"type": "disabled"}},
    )
    text = str(resp.choices[0].message.content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    l, r = text.find("{"), text.rfind("}")
    if l >= 0 and r > l:
        text = text[l : r + 1]
    raw = json.loads(text).get("seeds") or []
    out: list[str] = []
    for item in raw:
        w = "".join(_WORD_RE.findall(str(item))).lower()
        if w and w not in out:
            out.append(w)
    return out[:SEED_COUNT]


def resolve_seed_words(gen, product_id: str, product_name: str, lang: str) -> list[str]:
    """拿这个商品的种子词，一个商品只问一次接口。"""
    key = f"{product_id}||{lang}"
    with _SEED_LOCK:
        _load_seed_disk()
        hit = _SEED_DISK.get(key)
        if hit:
            _SEEDS_BY_PRODUCT[key] = list(hit)
            return list(hit)
        failed = key in _SEED_FAILED
    if not failed:
        try:
            seeds = _ask_seed_words(gen, product_name, lang)
        except Exception as e:
            seeds = []
            logger.warning("种子词抽取失败，退回品名切词: %s", e)
        if seeds:
            with _SEED_LOCK:
                _SEED_DISK[key] = list(seeds)
                _SEEDS_BY_PRODUCT[key] = list(seeds)
                _save_seed_disk()
            logger.info("种子词 %s → %s", (product_name or "")[:30], seeds)
            return seeds
        with _SEED_LOCK:
            _SEED_FAILED.add(key)
    fallback = seeds_from_name(product_name)
    logger.info("种子词用品名切词 %s → %s", (product_name or "")[:30], fallback)
    return fallback


def remember_seed_words(product_id: str, seeds: list[str], lang: str) -> None:
    if not seeds:
        return
    with _SEED_LOCK:
        _SEEDS_BY_PRODUCT[f"{product_id}||{lang}"] = list(seeds)
        _SEEDS_BY_PRODUCT[str(product_id)] = list(seeds)


def seed_words_for(product_id: str) -> list[str]:
    """供上传流程读取本商品的种子词（发布 loop 不传这个字段）。"""
    with _SEED_LOCK:
        got = _SEEDS_BY_PRODUCT.get(str(product_id))
        if got:
            return list(got)
        for k, v in _SEEDS_BY_PRODUCT.items():
            if k.startswith(f"{product_id}||") and v:
                return list(v)
    return []


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
2) 5 条标签带 #，整体语言与标题一致，每条必须是一个连写的词：
   # 后面不能有空格、连字符、点号或标点，例如写 #tiktokshopsingapore，不能写 #tiktok shop singapore。
   按精准到宽的顺序排（排在后面的会被截掉），配比：
   - 前 3 条紧贴商品本身：品类、口味/材质/规格、使用场景或人群，例如 #koreanramyun #spicynoodles #midnightsnack
   - 第 4 条地区购物场景，例如 #tiktokshopsingapore #sgfoodie
   - 第 5 条可以宽一点，但仍要和商品沾边
   下面这些一条都不要写，系统会另外补：#fyp #foryou #viral #trending #tiktok #musthave #explore
   也不要写光秃秃的 #TikTokShop 或光国名 #Singapore，它们有上千万条作品，没有购买意图。
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
        cart = resolve_cart_phrase(self, lang)
        logger.info(
            "品名语种 → %s (%s) | 品名=%s | 小黄车底稿「%s」→「%s」",
            lang,
            lang_name,
            (product_name or "")[:40],
            cart_draft(),
            cart,
        )

        seeds = resolve_seed_words(self, str(product_id), product_name or "", lang)

        old_prompt = getattr(content_gen, "PROMPT_TEMPLATE", "")
        content_gen.PROMPT_TEMPLATE = build_prompt_for_lang(
            "{product_name}", "{selling_points}", "{keywords}", lang
        )
        # _finalize_tags 里 n_others = hashtag_count - len(REQUIRED_HASHTAGS)。
        # hashtag_count 是 5、必带占 2，AI 写的 5 条就只有前 3 条能活下来，
        # 后两条（往往正是贴商品的那两条）被直接截掉。把必带的份额加回去。
        old_count = getattr(self, "hashtag_count", 5)
        try:
            self.hashtag_count = int(old_count) + len(required_tags())
        except Exception:
            pass

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
                trimmed = trim_generic_tags(str(result.get("caption") or ""))
                if trimmed:
                    result["caption"] = trimmed
                tags = result.get("hashtags")
                if isinstance(tags, list):
                    keep = {t.lstrip("#").lower() for t in required_tags()}
                    result["hashtags"] = [
                        t
                        for t in tags
                        if str(t).lstrip("#").lower() in keep
                        or str(t).lstrip("#").lower() not in _GENERIC_TAGS
                    ]
                remember_cart_display(str(product_id), cart, lang)
                remember_seed_words(str(product_id), seeds, lang)
                logger.info(
                    "文案语种=%s | 小黄车「%s」| caption前80=%s",
                    lang,
                    cart,
                    str(result.get("caption") or "")[:80].replace("\n", " "),
                )
            return result
        finally:
            content_gen.PROMPT_TEMPLATE = old_prompt
            self.hashtag_count = old_count
            if real_create is not None:
                try:
                    completions.create = real_create
                except Exception:
                    pass

    content_gen.ContentGenerator.generate = generate_fixed  # type: ignore[method-assign]
    logger.info("已应用补丁：按 Excel 品名语种生成标题/话题，并同步小黄车展示名（不改自动化流程）")
