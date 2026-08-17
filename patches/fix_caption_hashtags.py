"""话题标签：在原版填写/发布流程上轻量守卫，不重写上传自动化。

打开界面、选视频、填文案、挂车仍走原版 publish / _fill_caption。

并发关键问题：
1) 禁止在 publish 期间改写类方法（会串号冲掉另一窗口文案）
2) 挂车 / 显示更多 / AI 开关后，Studio 常清空描述框；
   点「发布」前若只做「缺失才补」，并发时容易漏补 → 发布出去没有标题/话题。
   因此：挂车完成后、点发布前，强制用本账号文案再覆盖一次。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("publisher")

# 标签选词校准数据。区间阈值（多大算太宽、多小算没搜索量）不能凭空拍，
# 得先攒一批真机数据：每打一个话题，把联想面板返回的全部行连作品数落盘。
_PROBE_LOCK = threading.Lock()
_PNAME_CACHE: dict[str, str] = {}


def _root() -> Path:
    try:
        import config_loader

        r = getattr(config_loader, "ROOT", None)
        if r:
            return Path(r)
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent


def hashtag_probe_path() -> Path:
    return _root() / "logs" / "hashtag_panel.jsonl"


def _product_name(pid: str) -> str:
    """品名是当前唯一的精准信号，校准时必须知道每条数据出自哪个品。"""
    if not pid:
        return ""
    if pid in _PNAME_CACHE:
        return _PNAME_CACHE[pid]
    name = ""
    try:
        import incident_log

        fn = getattr(incident_log, "_lookup_product_name", None)
        if callable(fn):
            name = str(fn(pid) or "")
    except Exception:
        name = ""
    _PNAME_CACHE[pid] = name
    return name


def _record_panel(rec: dict) -> None:
    """追加一条联想面板观测；永不抛出。"""
    try:
        ctx = {}
        try:
            import incident_log

            ctx = incident_log.get_account_context() or {}
        except Exception:
            ctx = {}
        row = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "account": ctx.get("account") or "",
            "ip_group": ctx.get("ip_group") or "",
            **rec,
        }
        path = hashtag_probe_path()
        with _PROBE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("话题面板观测落盘跳过: %s", e)

_TAG_RE = re.compile(r"#([^\s#]+)")
# TikTok 只认字母数字下划线，话题在第一个别的字符处就断掉
_TAG_DROP_RE = re.compile(r"[^0-9A-Za-z_\u00c0-\uffff]+")


def _normalize_tag(raw: str) -> str:
    """把标签压成一个连写词。

    写 `#tiktok shop` 发出去只有 `#tiktok` 是话题，`shop` 变成正文里的散字，
    带连字符/点号的同理会被截断。
    """
    body = _TAG_DROP_RE.sub("", (raw or "").lstrip("#"))
    return f"#{body}" if body else ""


def _tags_from_tag_line(line: str) -> list[str]:
    """标签行里被空格拆开的词，并回它前面那个标签。"""
    out: list[str] = []
    for token in line.split():
        if token.startswith("#"):
            out.append(token)
        elif out:
            out[-1] += token
    return out


def _split_title_tags(caption: str) -> tuple[str, list[str]]:
    text = (caption or "").replace("\r\n", "\n").strip()
    if not text:
        return "", []
    tags: list[str] = []

    def add(raw: str) -> None:
        tag = _normalize_tag(raw)
        if tag and tag not in tags:
            tags.append(tag)

    lines: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            for raw in _tags_from_tag_line(s):
                add(raw)
            continue
        for m in _TAG_RE.finditer(s):
            add(m.group(0))
        cleaned = _TAG_RE.sub("", s)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;|")
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip(), tags


def _read_caption_text(page) -> str:
    try:
        return (
            page.evaluate(
                """() => {
                  const el = document.querySelector('[data-e2e="caption_container"] [contenteditable="true"]')
                    || [...document.querySelectorAll('[contenteditable="true"]')]
                         .find(e => e.offsetParent !== null);
                  return el ? (el.innerText || '').trim() : '';
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def _caption_missing_tags(page, tags: list[str]) -> list[str]:
    """哪些话题不在框里。

    只接受精确匹配后，落进去的必然就是想打的那一条（只差大小写），
    所以不再需要「本想打 A 实际落了 B」的映射层。
    """
    actual = _read_caption_text(page).lower()
    missing = []
    for t in tags:
        key = t.lstrip("#").lower()
        if not key:
            continue
        if key in actual or f"#{key}" in actual:
            continue
        missing.append(t)
    return missing


_CAPTION_SELECTORS = (
    '[data-e2e="caption_container"] [contenteditable="true"]',
    '[contenteditable="true"]',
)


def _caption_locator(page):
    for sel in _CAPTION_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                return loc
        except Exception:
            continue
    return None


# 列出话题联想面板的候选。面板在编辑器外面（Draft.js Mention），
# 点中它 Studio 才会把 #xxx 记成话题实体；整行插入再 Escape 只是关掉面板，
# DOM 里仍是普通文本，提交时 text_extra 为空，手机端点不动。
#
# 两个必须硬排掉的冒牌货（实测点到过，后果是文案被打进「位置」输入框）：
#   * 描述框下面那排「历史记录」chip：# 和文字是两个元素，innerText 读出来是 "# fyp"
#   * 「位置」字段的下拉项：新加坡 / Central Singapore …
# 判据是作品数：真联想每行都带 "4.0M 个作品"，chip 和位置项都没有。
_LIST_SUGGEST_JS = """() => {
  const editor = document.querySelector('[data-e2e="caption_container"] [contenteditable="true"]')
    || [...document.querySelectorAll('[contenteditable="true"]')].find((e) => e.offsetParent !== null);
  if (!editor) return { rows: [], reject: { noeditor: 1 } };
  const er = editor.getBoundingClientRect();
  const TAG = /#([^\\s#]+)/;
  const METRIC = /([0-9][0-9.,]*)\\s*(亿|万|[KkMmBb])?\\s*(个作品|个视频|个帖子|个投稿|posts?|videos?|views?|次观看|次播放)/i;
  const parseNum = (num, unit) => {
    const v = parseFloat(String(num).replace(/,/g, ''));
    if (!isFinite(v)) return 0;
    const u = String(unit || '').toLowerCase();
    const mul = u === 'b' ? 1e9 : u === 'm' ? 1e6 : u === 'k' ? 1e3
      : u === '亿' ? 1e8 : u === '万' ? 1e4 : 1;
    return v * mul;
  };
  const reject = { tiny: 0, far: 0, notag: 0, nometric: 0 };
  const vis = [];
  const nodes = document.querySelectorAll(
    '[role="option"], [role="listbox"] li, [role="listbox"] div, li,'
    + '[class*="uggest"] *, [class*="opic"] *, [class*="ashtag"] *, [class*="ention"] *'
  );
  for (const n of nodes) {
    if (editor.contains(n)) continue;
    const r = n.getBoundingClientRect();
    if (r.width < 60 || r.height < 14) { reject.tiny += 1; continue; }
    // 面板贴着描述框弹，左边缘和描述框基本对齐；
    // 右侧那个手机预览里也有 #话题 和播放量，靠横向条件排掉
    if (r.top < er.top - 40 || r.top > er.bottom + 620) { reject.far += 1; continue; }
    if (r.right < er.left + 20 || r.left > er.right - 20) { reject.far += 1; continue; }
    const txt = (n.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!txt || txt.length > 120) { reject.notag += 1; continue; }
    const tag = txt.match(TAG);
    if (!tag) { reject.notag += 1; continue; }
    const met = txt.match(METRIC);
    if (!met) { reject.nometric += 1; continue; }
    vis.push({
      n, tag: tag[1], txt,
      views: parseNum(met[1], met[2]),
      cx: r.left + r.width / 2, cy: r.top + r.height / 2,
    });
  }
  // 面板容器也会命中，它的文本是所有行拼起来的，只留最里层那个
  const keep = vis.filter((x) => !vis.some((y) => y !== x && x.n.contains(y.n)));
  window.__ttTagCands = keep.map((x) => x.n);
  return {
    reject,
    rows: keep.map((x, i) => ({
      i, tag: x.tag, txt: x.txt.slice(0, 40), views: x.views, cx: x.cx, cy: x.cy,
    })),
  };
}"""

# 面板多半只听 mousedown，单发 click 不一定生效
_CLICK_INDEX_JS = """(i) => {
  const n = (window.__ttTagCands || [])[i];
  if (!n) return false;
  for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
    n.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
  }
  return true;
}"""

# 焦点跑到「位置」之类的输入框后还继续敲键盘，文案就整段打进那个框里，
# 描述框只剩一个 # —— 这次商品挂不上、描述只有 # 就是这么来的
_FOCUS_JS = """() => {
  const editor = document.querySelector('[data-e2e="caption_container"] [contenteditable="true"]')
    || [...document.querySelectorAll('[contenteditable="true"]')].find((e) => e.offsetParent !== null);
  if (!editor) return 'no-editor';
  const a = document.activeElement;
  if (a && (a === editor || editor.contains(a))) return 'editor';
  if (!a || a === document.body) return 'body';
  const e2e = (a.getAttribute && a.getAttribute('data-e2e')) || '';
  const ph = (a.getAttribute && a.getAttribute('placeholder')) || '';
  return (a.tagName || '?').toLowerCase()
    + (e2e ? '[' + e2e + ']' : '')
    + (ph ? '(' + ph.slice(0, 20) + ')' : '')
    + '.' + String(a.className || '').slice(0, 30);
}"""


def _focus_state(page) -> str:
    try:
        return str(page.evaluate(_FOCUS_JS) or "?")
    except Exception as e:
        return f"?({e})"


def _focus_editor(page, loc) -> bool:
    """确认光标还在描述框，不在就点回去并移到末尾。

    抢不回来必须停手：继续敲键盘等于把文案打进别的输入框。
    """
    if _focus_state(page) == "editor":
        return True
    state = _focus_state(page)
    logger.warning("焦点不在描述框（当前=%s），点回描述框", state)
    try:
        loc.click(timeout=2000)
        page.wait_for_timeout(150)
    except Exception as e:
        logger.warning("点回描述框失败: %s", e)
        return False
    if _focus_state(page) != "editor":
        logger.warning("焦点仍不在描述框: %s", _focus_state(page))
        return False
    try:
        page.keyboard.press("Control+End")
    except Exception:
        pass
    return True


def _hashtag_entities(page) -> tuple[int, int]:
    """返回（被编辑器装饰成话题的个数，形如 #xxx 的文本节点总数）。

    Draft.js 给识别出来的话题多套一层无属性 `<span>`（装饰器），
    没识别的就直接挂在 block 下面。两个数不相等就说明有话题只是明文。
    """
    try:
        out = (
            page.evaluate(
                """() => {
                  const el = document.querySelector('[data-e2e="caption_container"] [contenteditable="true"]')
                    || [...document.querySelectorAll('[contenteditable="true"]')]
                         .find(e => e.offsetParent !== null);
                  if (!el) return { dec: 0, total: 0 };
                  let dec = 0, total = 0;
                  for (const s of el.querySelectorAll('span[data-text="true"]')) {
                    const t = (s.innerText || '').trim();
                    if (!/^#[^\\s#]+$/.test(t)) continue;
                    total += 1;
                    const leaf = s.parentElement;
                    const wrap = leaf && leaf.parentElement;
                    if (wrap && wrap.tagName === 'SPAN' && !wrap.hasAttribute('data-offset-key')) dec += 1;
                  }
                  return { dec, total };
                }"""
            )
            or {}
        )
        return int(out.get("dec") or 0), int(out.get("total") or 0)
    except Exception:
        return 0, 0


def _type_keys(page, s: str, delay: int = 35) -> None:
    """逐字敲真键盘。

    联想面板要收到 keydown 才会弹，而 CDP insertText 只发 input 事件，
    面板根本不出现。非 ASCII 仍走 insertText——逐字敲会打散泰文的组合字符。
    """
    for ch in s:
        if ch.isascii() and ch.isprintable():
            page.keyboard.type(ch)
        else:
            page.keyboard.insert_text(ch)
        page.wait_for_timeout(delay)


def _panel_rows(page) -> tuple[list, dict]:
    """联想面板的候选行，按标签去重。

    同一条标签会同时命中行本身和行内的 span，真机数据里 #TikTokShop 就出现了两次。
    """
    try:
        out = page.evaluate(_LIST_SUGGEST_JS) or {}
    except Exception:
        return [], {}
    best: dict[str, dict] = {}
    for r in out.get("rows") or []:
        body = _normalize_tag(str(r.get("tag") or "")).lstrip("#").lower()
        if not body:
            continue
        v = int(r.get("views") or 0)
        if body not in best or v > int(best[body].get("views") or 0):
            best[body] = dict(r, tag=body, views=v)
    return list(best.values()), dict(out.get("reject") or {})


def _pick_suggestion(page, want: str) -> tuple[dict, dict, list]:
    """从联想面板找完全一样的那一条，只接受精确匹配。

    以前没有精确匹配就退而点「作品数最高的近似项」，真机数据抓到过后果：
    新加坡账号想打 #TikTokShop，那一刻面板只回了 2 行、没有精确匹配，
    结果落进去的是 #tiktokshopmalaysia——流量直接送到别的国家。
    面板是渐进加载的，同一个词在另一台机器上同时返回了 10 行含精确匹配，
    所以正确做法是多等一会儿等精确匹配出现，等不到就留明文。
    返回（选中项, 被排掉的候选统计, 面板全部行）。
    """
    rows, reject = _panel_rows(page)
    if not rows:
        return {}, reject, rows
    key = want.lstrip("#").lower()
    for c in rows:
        if c.get("tag") == key:
            return dict(c, n=len(rows), exact=True), reject, rows
    reject["nomatch"] = [c.get("tag") for c in rows[:3]]
    return {}, reject, rows


# ---------------- 精准标签：从面板真实候选里选，不让模型凭空想 ----------------
# 模型说不出「哪个标签现在有多少作品」，实测它只会给安全牌：真机日志里两个完全不同
# 的商品（Agar Agar 和 Mamee Monster snack）拿到一模一样的 #TikTokShop #Singapore
# #MustHave。而 Studio 的联想面板每条都带真实作品数，是唯一可靠的热度来源。
# 于是分工改成：模型只从品名抽种子词，真实标签一律由面板给，按作品数区间挑中长尾。
_TAG_CFG_DEFAULT = {
    # 区间按真机实测标定：#TikTokShop 1.5 亿 / #Singapore 1010 万 / #MustHave 400 万
    # 都太宽；#musthaveproducts 24.8 万、#musthavegadgets 8.1 万这一档才是能带人的。
    "min_posts": 50_000,
    "max_posts": 3_000_000,
    "target_posts": 300_000,
    "count": 5,
    "market": "",
    "probe": True,
}
_TAG_CFG_CACHE: dict = {"mtime": None, "val": dict(_TAG_CFG_DEFAULT)}
_POOL_LOCK = threading.Lock()
_POOL_DISK: dict = {}
_POOL_DISK_LOADED = False
_POOL_TTL_DAYS = 7

_REGION_NAMES = {
    "singapore", "malaysia", "indonesia", "thailand", "vietnam", "philippines",
    "japan", "korea", "taiwan", "china", "india", "australia", "america",
    "brasil", "brazil", "mexico", "espana",
}
# #tiktokshopth / #tiktokshopmy 这类两字母后缀：短代码不能当子串匹配
# （#myfood 里的 my 不是马来西亚），只在这个固定前缀下认。
_SHOP_REGION_RE = re.compile(r"^tiktokshop([a-z]{2})$")
_CODE_TO_REGION = {
    "sg": "singapore", "my": "malaysia", "th": "thailand", "id": "indonesia",
    "ph": "philippines", "vn": "vietnam", "jp": "japan", "kr": "korea",
    "tw": "taiwan", "us": "america", "uk": "britain", "au": "australia",
}


def _tag_cfg() -> dict:
    """读 content 下的选词参数，配置文件变了就重读。"""
    cfg_file = _root() / "config.yaml"
    try:
        mtime = cfg_file.stat().st_mtime
    except OSError:
        return dict(_TAG_CFG_CACHE["val"])
    if _TAG_CFG_CACHE["mtime"] == mtime:
        return dict(_TAG_CFG_CACHE["val"])
    val = dict(_TAG_CFG_DEFAULT)
    try:
        import config_loader

        node = (config_loader.load_config(cfg_file) or {}).get("content") or {}
        if node.get("tag_min_posts") is not None:
            val["min_posts"] = int(node["tag_min_posts"])
        if node.get("tag_max_posts") is not None:
            val["max_posts"] = int(node["tag_max_posts"])
        if node.get("tag_target_posts") is not None:
            val["target_posts"] = int(node["tag_target_posts"])
        if node.get("precise_tag_count") is not None:
            val["count"] = int(node["precise_tag_count"])
        if node.get("market") is not None:
            val["market"] = str(node["market"] or "").strip().lower()
        if node.get("tag_panel_probe") is not None:
            val["probe"] = bool(node["tag_panel_probe"])
    except Exception as e:
        logger.debug("读取选词配置失败，用默认值: %s", e)
    _TAG_CFG_CACHE["mtime"] = mtime
    _TAG_CFG_CACHE["val"] = val
    return dict(val)


def _required_tags() -> list[str]:
    try:
        import fix_content_i18n

        return list(fix_content_i18n.required_tags())
    except Exception:
        return ["#tiktok", "#fyp"]


def _too_broad() -> set[str]:
    try:
        import fix_content_i18n

        return set(fix_content_i18n.too_broad_tags())
    except Exception:
        return {"tiktokshop", "singapore", "musthave", "viral", "trending"}


def tag_region(tag: str) -> str:
    """标签里带的地区，认不出返回空。"""
    body = str(tag).lstrip("#").lower()
    m = _SHOP_REGION_RE.match(body)
    if m:
        return _CODE_TO_REGION.get(m.group(1), "")
    for name in _REGION_NAMES:
        if name in body:
            return name
    return ""


def is_wrong_region(tag: str, market: str) -> bool:
    """带了别的地区就不能用。

    市场没配时，任何带地区的标签都拒掉：宁可少一条，也不要把新加坡号的流量
    送去马来西亚。想放开就在 config 里填 content.market。
    """
    region = tag_region(tag)
    return bool(region) and region != str(market or "").strip().lower()


def select_precise_tags(
    pool: dict,
    *,
    count: int,
    min_posts: int,
    max_posts: int,
    target_posts: int,
    market: str = "",
    exclude=(),
) -> list[str]:
    """从面板候选池里挑精准标签。

    pool: {标签: {"views": 作品数, "seed": 来自哪个种子词}}
    作品数按数量级比较：5 万到 300 万之间差 60 倍，线性距离没意义。
    """
    ban = {str(t).lstrip("#").lower() for t in exclude} | _too_broad()
    cands: list[tuple[str, int, str]] = []
    for tag, info in (pool or {}).items():
        body = str(tag).lstrip("#").lower()
        if not body or len(body) < 3 or body in ban:
            continue
        views = int((info or {}).get("views") or 0)
        if views < min_posts or views > max_posts:
            continue
        if is_wrong_region(body, market):
            continue
        seed = str((info or {}).get("seed") or "").lower()
        # 面板是按词匹配返回的，正常候选一定和种子词互为子串。
        # 不设这道就会把 #singaporetamil 这种只是碰巧同前缀的词当成精准标签。
        if seed and body != seed and seed not in body and body not in seed:
            continue
        cands.append((body, views, seed))
    lt = math.log10(max(int(target_posts), 1))
    # 离目标档位越近越好；同档优先更长的（更具体）
    cands.sort(key=lambda c: (abs(math.log10(max(c[1], 1)) - lt), -len(c[0])))

    out: list[str] = []
    per_seed: dict[str, int] = {}
    for body, _views, seed in cands:
        if per_seed.get(seed, 0) >= 2:  # 别让一个种子词包场，标签要覆盖多个角度
            continue
        out.append(f"#{body}")
        per_seed[seed] = per_seed.get(seed, 0) + 1
        if len(out) >= count:
            return out
    for body, _views, _seed in cands:  # 种子词太少时放开配额补满
        tag = f"#{body}"
        if tag not in out:
            out.append(tag)
        if len(out) >= count:
            break
    return out


def _pool_cache_file() -> Path:
    return _root() / "data" / "tag_pool_cache.json"


def _load_pool_disk() -> None:
    global _POOL_DISK_LOADED
    if _POOL_DISK_LOADED:
        return
    _POOL_DISK_LOADED = True
    try:
        p = _pool_cache_file()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _POOL_DISK.update(data)
    except Exception as e:
        logger.debug("候选池缓存读取跳过: %s", e)


def _save_pool_disk() -> None:
    try:
        p = _pool_cache_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(_POOL_DISK, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug("候选池缓存落盘跳过: %s", e)


def _cached_pool(key: str) -> dict:
    with _POOL_LOCK:
        _load_pool_disk()
        node = _POOL_DISK.get(key)
        if not isinstance(node, dict):
            return {}
        try:
            age = time.time() - float(node.get("ts") or 0)
        except (TypeError, ValueError):
            return {}
        if age > _POOL_TTL_DAYS * 86400:
            return {}
        tags = node.get("tags")
        return dict(tags) if isinstance(tags, dict) else {}


def _store_pool(key: str, pool: dict) -> None:
    if not pool:
        return
    with _POOL_LOCK:
        _load_pool_disk()
        _POOL_DISK[key] = {"ts": time.time(), "tags": pool}
        _save_pool_disk()


def _probe_seed(page, loc, seed: str) -> list:
    """在描述框里打 #种子词，读联想面板，再清空。

    面板是渐进加载的（真机同一个词有时只回 2 行、有时 10 行），
    所以要等行数不再变了才读，否则拿到的是半截列表。
    """
    if not _focus_editor(page, loc):
        return []
    rows: list = []
    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(100)
        _type_keys(page, "#")
        page.wait_for_timeout(150)
        _type_keys(page, seed)
        stable = 0
        for _ in range(8):
            page.wait_for_timeout(220)
            now, _ = _panel_rows(page)
            if now and len(now) == len(rows):
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            rows = now
    except Exception as e:
        logger.debug("种子词 %s 探测跳过: %s", seed, e)
    finally:
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.wait_for_timeout(80)
        except Exception:
            pass
    return rows


def build_tag_pool(page, loc, seeds: list, cache_key: str) -> dict:
    """逐个种子词查联想，攒成候选池。一个商品只探一次，之后走缓存。"""
    hit = _cached_pool(cache_key)
    if hit:
        logger.info("候选池用缓存 %s 条 key=%s", len(hit), cache_key)
        return hit
    pool: dict = {}
    for seed in seeds:
        rows = _probe_seed(page, loc, seed)
        for r in rows:
            body = str(r.get("tag") or "")
            views = int(r.get("views") or 0)
            old = pool.get(body)
            if old is None or views > int(old.get("views") or 0):
                pool[body] = {"views": views, "seed": seed}
        logger.info("种子词 #%s 查到 %s 条候选", seed, len(rows))
    if pool:
        _store_pool(cache_key, pool)
    top = sorted(pool.items(), key=lambda kv: -int(kv[1].get("views") or 0))[:8]
    logger.info(
        "候选池 %s 条 | 作品数最高的几条=%s",
        len(pool),
        [f"#{k}:{v['views']}" for k, v in top],
    )
    return pool


def _seed_words(product_id: str) -> list:
    try:
        import fix_content_i18n

        return list(fix_content_i18n.seed_words_for(product_id))
    except Exception as e:
        logger.debug("读取种子词跳过: %s", e)
        return []


def _dedup_tags(tags: list) -> list:
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        body = _normalize_tag(str(t)).lstrip("#").lower()
        if not body or body in seen:
            continue
        seen.add(body)
        out.append(f"#{body}")
    return out


def _resolve_tags(page, loc, ai_tags: list, ctx: dict) -> tuple[list, str]:
    """定这条视频最终打哪些标签：面板选出的精准标签 + 必带标签。

    面板探测失败（没种子词、面板不出、区间里一条都没有）就退回模型给的标签，
    至少不比改之前差。
    """
    cfg = _tag_cfg()
    required = _required_tags()
    fallback = _dedup_tags(list(ai_tags) + required)
    if not cfg["probe"]:
        return fallback, "ai(已关面板选词)"
    pid = str((ctx or {}).get("product_id") or "")
    seeds = _seed_words(pid)
    if not seeds:
        logger.info("没有种子词，标签沿用模型输出 product=%s", pid or "-")
        return fallback, "ai(无种子词)"
    pool = build_tag_pool(page, loc, seeds, f"{pid}||{cfg['market'] or 'nomarket'}")
    picked = select_precise_tags(
        pool,
        count=int(cfg["count"]),
        min_posts=int(cfg["min_posts"]),
        max_posts=int(cfg["max_posts"]),
        target_posts=int(cfg["target_posts"]),
        market=str(cfg["market"]),
        exclude=required,
    )
    if not picked:
        logger.warning(
            "候选池 %s 条里没有落在 %s~%s 作品数区间的，标签沿用模型输出",
            len(pool),
            cfg["min_posts"],
            cfg["max_posts"],
        )
        return fallback, "ai(区间内无候选)"
    ctx["pool_size"] = len(pool)
    ctx["picked_precise"] = picked
    logger.info(
        "精准标签取自面板 %s（池 %s 条，区间 %s~%s）+ 必带 %s",
        picked,
        len(pool),
        cfg["min_posts"],
        cfg["max_posts"],
        required,
    )
    return _dedup_tags(picked + required), "panel"


def _click_suggestion(page, hit: dict) -> bool:
    try:
        page.mouse.click(float(hit["cx"]), float(hit["cy"]))
        return True
    except Exception as e:
        logger.debug("鼠标点联想失败，改派事件: %s", e)
    try:
        return bool(page.evaluate(_CLICK_INDEX_JS, int(hit["i"])))
    except Exception:
        return False


def _type_one_hashtag(page, tag: str, loc, ctx: dict | None = None) -> str:
    """输入一个 #话题，等联想出现后点选（和人手点一条一致）。

    只在认出真联想行时才点。以前「有候选就按 Enter」，而候选里混着位置下拉，
    Enter 直接把位置选上了，后面的字全打进位置框——文案和挂车一起废掉。
    返回: picked / typed
    """
    raw = _normalize_tag(tag)
    if not raw:
        return "typed"
    body = raw[1:]
    obs: dict = dict(ctx or {})
    obs["typed"] = raw

    def done(how: str, landed: str = "", why: str = "") -> str:
        obs["how"] = how
        obs["landed"] = landed
        if why:
            obs["why"] = why
        _record_panel(obs)
        return how

    dec0, _ = _hashtag_entities(page)
    _type_keys(page, "#")
    page.wait_for_timeout(150)
    _type_keys(page, body)

    # 面板渐进加载，精确匹配常常不在第一批返回里。多等几轮，等不到再留明文——
    # 绝不拿近似项顶替（真机上就是这样把新加坡的标签换成马来西亚的）。
    hit: dict = {}
    reject: dict = {}
    rows: list = []
    for _ in range(10):
        page.wait_for_timeout(220)
        hit, reject, rows = _pick_suggestion(page, raw)
        if hit.get("tag"):
            break

    # 面板全部行都记下来：将来定「多大算太宽」的阈值要靠这批真实作品数
    obs["panel"] = [
        {"tag": r.get("tag"), "views": int(r.get("views") or 0)} for r in rows[:20]
    ]
    obs["reject"] = reject

    if not hit.get("tag"):
        logger.warning(
            "联想面板里等不到和 %s 完全一样的一条，留明文（手机端点不动）| 排除情况=%s",
            raw,
            reject,
        )
        _ensure_trailing_space(page)
        return done("typed", why="no_row")

    landed = f"#{hit['tag']}"
    obs["exact"] = True
    obs["landed_views"] = int(hit.get("views") or 0)
    if not _click_suggestion(page, hit):
        logger.warning("联想行点不动，留明文 %s", raw)
        _ensure_trailing_space(page)
        return done("typed", landed, why="click_failed")

    page.wait_for_timeout(280)
    dec1, _ = _hashtag_entities(page)
    if not _focus_editor(page, loc):
        logger.warning("点联想后焦点被带走且抢不回来: %s", landed)
        return done("typed", landed, why="focus_lost")
    if dec1 <= dec0:
        logger.warning(
            "点了联想但编辑器没把它记成话题 %s（实体 %s→%s），当明文算",
            landed,
            dec0,
            dec1,
        )
        _ensure_trailing_space(page)
        return done("typed", landed, why="not_entity")

    logger.info(
        "已点选话题联想 %s（作品数≈%s，可点候选%s）| 面板=%s",
        landed,
        int(hit.get("views") or 0),
        hit.get("n"),
        [f"{r.get('tag')}:{int(r.get('views') or 0)}" for r in rows[:6]],
    )
    _ensure_trailing_space(page)
    return done("picked", landed)


def _ensure_trailing_space(page) -> None:
    """点完联想 Studio 有时自己补空格、有时不补，不补就自己补一个。

    两个话题贴在一起（#a#b）编辑器只会认出前一个。
    """
    try:
        # 不能用 _read_caption_text：它 trim 过，末尾是不是空白就看不出来了
        tail = str(
            page.evaluate(
                """() => {
                  const el = document.querySelector('[data-e2e="caption_container"] [contenteditable="true"]')
                    || [...document.querySelectorAll('[contenteditable="true"]')]
                         .find(e => e.offsetParent !== null);
                  return el ? (el.innerText || '').slice(-2) : '';
                }"""
            )
            or ""
        )
    except Exception:
        tail = ""
    if tail and tail[-1].isspace():
        return
    try:
        page.keyboard.type(" ")
    except Exception:
        pass


def _type_caption_real(page, text: str, probe_ctx: dict | None = None) -> bool:
    """用真实键盘输入写描述，话题逐个 # 并点选联想。

    原版走 `el.innerText = text` + 合成 InputEvent 直接改 DOM，
    但 Studio 的描述框是富文本编辑器，自己维护一份数据模型，
    不认这种非可信事件。结果就是框里看得见字、提交上去却是空的，
    发出来显示「暂无描述」。

    标题仍用 CDP insertText（整段插入，泰文不会被拆）。
    话题必须逐字敲键、等联想、点选：只有点过联想，提交体里才有 text_extra，
    发出去的话题在手机端才点得动、才进话题流。
    """
    loc = _caption_locator(page)
    if loc is None:
        logger.warning("没找到描述框，无法真实键入")
        return False
    if not hasattr(page.keyboard, "insert_text"):
        logger.warning("当前 Playwright 不支持 insert_text")
        return False
    title, ai_tags = _split_title_tags(text)
    ctx = probe_ctx if isinstance(probe_ctx, dict) else {}
    try:
        loc.click(timeout=3000)
        page.wait_for_timeout(150)
        if _focus_state(page) != "editor":
            logger.warning("点了描述框但焦点没进去: %s", _focus_state(page))
            return False
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(120)
        # 探测要在空框里做（要反复清空），所以必须排在写标题之前
        tags, src = _resolve_tags(page, loc, ai_tags, ctx)
        ctx["tags_wanted"] = tags
        ctx["tag_source"] = src
        if tags:
            ctx["final_caption"] = f"{title}\n\n{' '.join(tags)}".strip()
        if not _focus_editor(page, loc):
            logger.warning("探测候选池后焦点丢了，抢不回来")
            return False
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(120)
        if title:
            for i, line in enumerate(title.split("\n")):
                if i:
                    page.keyboard.press("Enter")
                if line:
                    page.keyboard.insert_text(line)
                    page.wait_for_timeout(60)
        if tags:
            page.keyboard.press("Enter")
            page.wait_for_timeout(80)
            stats = {"picked": 0, "typed": 0}
            for tag in tags:
                if not _focus_editor(page, loc):
                    logger.warning(
                        "焦点丢了又抢不回来，剩下的话题不打了（再敲会打进别的输入框）"
                    )
                    break
                how = _type_one_hashtag(page, tag, loc, ctx)
                stats[how] = stats.get(how, 0) + 1
            if _focus_state(page) == "editor":
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
            page.wait_for_timeout(120)
            dec, total = _hashtag_entities(page)
            logger.info(
                "话题点选 picked=%s typed=%s / 共%s（来源=%s）| 编辑器已识别 %s/%s",
                stats["picked"],
                stats["typed"],
                len(tags),
                src,
                dec,
                total,
            )
            if stats["picked"] == 0:
                logger.warning(
                    "没有一个话题点上联想，发出去是明文（手机端不可点）"
                )
        else:
            page.wait_for_timeout(250)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    except Exception as e:
        logger.warning("真实键入描述异常: %s", e)
        return False

    actual = _read_caption_text(page).replace("\n", " ")
    head = (title or text).replace("\n", " ").strip()[:12]
    if head and head in actual:
        return True
    logger.warning(
        "真实键入后校验不通过 期望前12=%s 实际前60=%s", head, actual[:60]
    )
    return False


def _active_caption(uploader) -> str:
    return str(getattr(uploader, "_publish_caption", "") or "")


def _dump_caption_dom(page) -> str:
    """把描述框的 DOM 结构打出来。

    话题到底有没有被编辑器识别，光看 innerText 是看不出来的（纯文本和实体
    读出来一模一样）。只有看 innerHTML 才能确定 Studio 是怎么渲染 #xxx 的。
    """
    try:
        return (
            page.evaluate(
                """() => {
                  const el = document.querySelector('[data-e2e="caption_container"] [contenteditable="true"]')
                    || [...document.querySelectorAll('[contenteditable="true"]')]
                         .find(e => e.offsetParent !== null);
                  return el ? (el.innerHTML || '') : '(未找到描述框)';
                }"""
            )
            or ""
        )
    except Exception as e:
        return f"(读取失败: {e})"


def apply_caption_hashtag_patches() -> None:
    import tiktok_uploader

    cls = tiktok_uploader.TikTokStudioUploader
    # v4 逐字 type() 会打散泰文；v5 回退 JS 写入但发出去是「暂无描述」；
    # v6 整段 insertText；v7 想点联想但仍用 insertText，面板不弹等于没改；
    # v8 话题逐字敲真键盘再点联想（提交体里才有 text_extra，手机端才可点）
    # v9 标签不再由模型凭空想：种子词查联想面板，按真实作品数区间选中长尾
    already = getattr(cls, "_caption_hashtag_patched_v9", False)
    if already:
        return

    original_fill = cls._fill_caption
    # 若已打过 v2，publish/ai/wait/post 可能已是守卫；再包一层会重复。
    # 以「当前类上的方法」为底座，保证挂到最新链路上。
    original_publish = cls.publish
    original_ai = cls._open_ai_more_if_any
    original_wait = cls._wait_content_check
    original_post = cls._click_post
    original_attach = cls._attach_product

    def fill_caption_fixed(self, page, caption: str):
        """优先真实键入，失败才回退原版 JS 写入。

        原版 JS 写入会让描述在页面上看着是好的、发出去却是「暂无描述」，
        所以它只能当兜底：真实键入至少有机会成功，回退了也不比现在更差。
        逐字 type() 不用，实测会把泰文的组合字符打散。
        """
        text = (caption or "").strip()
        if not text:
            raise RuntimeError("文案为空，拒绝填写")
        try:
            self._discard_unsaved_draft(page)
        except Exception as e:
            logger.debug("键入前清草稿跳过: %s", e)

        ctx = getattr(self, "_probe_ctx", None)
        if not isinstance(ctx, dict):
            ctx = {}
            self._probe_ctx = ctx
        if _type_caption_real(page, text, ctx):
            # 面板选出的标签和模型给的不是同一批，守卫必须按实际写进去的这版比对，
            # 否则每个守卫点都判定「标签缺失」，然后清空重打一遍。
            final = str(ctx.get("final_caption") or "").strip()
            if final and final != text:
                self._publish_caption = final
                logger.info(
                    "本条实际文案（守卫按它比对）: %s", final.replace("\n", " ")[:80]
                )
            logger.info(
                "已填写描述/标题（真实键入，编辑器已收到）: %s",
                text.replace("\n", " ")[:60],
            )
            return None

        logger.warning("真实键入失败，回退原版 JS 写入（有发出去没描述的风险）")
        return original_fill(self, page, caption)

    def ensure_with_original(
        self, page, caption: str, *, force: bool = False, where: str = ""
    ) -> None:
        if page is None or not (caption or "").strip():
            if force:
                logger.warning(
                    "发布前强制回填跳过：本账号文案为空 where=%s", where or "-"
                )
            return
        title, tags = _split_title_tags(caption)
        try:
            missing = _caption_missing_tags(page, tags) if tags else []
            actual = _read_caption_text(page)
        except Exception as e:
            if not force:
                return
            missing = list(tags)
            actual = ""
            logger.warning("读取描述失败，将强制回填 where=%s: %s", where or "-", e)

        flat = actual.replace("\n", " ")
        title_ok = (not title) or (title[:12] in flat)
        tags_ok = not (tags and missing)
        # force 的本意是「并发时描述被清空要能补回来」，不是每次都重写一遍。
        # 内容已经完整还重写，只会反复清空重打、把文案越弄越残。
        if title_ok and tags_ok and actual.strip():
            logger.info(
                "描述完整，跳过回填 where=%s force=%s actual前60=%s",
                where or "-",
                force,
                flat[:60],
            )
            return
        need = force or not tags_ok or not title_ok
        if not need and actual.strip():
            logger.info(
                "描述仍在，跳过回填 where=%s actual前60=%s",
                where or "-",
                flat[:60],
            )
            return

        logger.warning(
            "回填描述/话题 where=%s force=%s missing=%s actual前60=%s",
            where or "-",
            force,
            missing,
            (actual or "").replace("\n", " ")[:60],
        )
        try:
            fill_caption_fixed(self, page, caption)
            after = _read_caption_text(page)
            logger.info(
                "回填后描述前80=%s",
                (after or "").replace("\n", " ")[:80],
            )
        except Exception as e:
            logger.warning("补写文案失败 where=%s: %s", where or "-", e)

    def open_ai_guard(self, page):
        original_ai(self, page)
        try:
            # AI/显示更多后常见丢文案：缺则补；不强制以免打断开关状态
            ensure_with_original(
                self, page, _active_caption(self), force=False, where="ai后"
            )
        except Exception as e:
            logger.debug("AI后话题守卫跳过: %s", e)

    def wait_check_guard(self, page):
        try:
            ensure_with_original(
                self, page, _active_caption(self), force=False, where="内容检测前"
            )
        except Exception:
            pass
        return original_wait(self, page)

    def click_post_guard(self, page):
        # 关键：点发布前强制覆盖一次，避免挂车后空描述被发出去
        try:
            ensure_with_original(
                self, page, _active_caption(self), force=True, where="点发布前"
            )
        except Exception as e:
            logger.warning("点发布前强制回填异常: %s", e)
        try:
            logger.info(
                "即将点发布，当前描述前80=%s",
                _read_caption_text(page).replace("\n", " ")[:80],
            )
            logger.info("点发布前描述框DOM=%s", _dump_caption_dom(page)[:600])
        except Exception:
            pass
        return original_post(self, page)

    def attach_product_fixed(self, page, product_id, product_display_name=None):
        out = original_attach(self, page, product_id, product_display_name)
        # 挂车弹窗关闭后 Studio 经常清空描述：立刻强制写回
        try:
            ensure_with_original(
                self,
                getattr(self, "page", None) or page,
                _active_caption(self),
                force=True,
                where="挂车后",
            )
        except Exception as e:
            logger.warning("挂车后回填文案失败: %s", e)
        return out

    def publish_fixed(self, job):
        """文案挂在实例上，供 AI/挂车/点发布守卫读取；不改类方法。"""
        prev = getattr(self, "_publish_caption", None)
        self._publish_caption = getattr(job, "caption", "") or ""
        pid = str(getattr(job, "product_id", "") or "")
        self._probe_ctx = {
            "product_id": pid,
            "product_name": _product_name(pid),
            "video": Path(str(getattr(job, "video_path", "") or "")).name,
            "dry_run": bool(getattr(self, "dry_run", False)),
        }
        try:
            return original_publish(self, job)
        finally:
            if prev is None:
                try:
                    delattr(self, "_publish_caption")
                except Exception:
                    self._publish_caption = ""
            else:
                self._publish_caption = prev

    cls._fill_caption = fill_caption_fixed  # type: ignore[method-assign]
    cls._open_ai_more_if_any = open_ai_guard  # type: ignore[method-assign]
    cls._wait_content_check = wait_check_guard  # type: ignore[method-assign]
    cls._click_post = click_post_guard  # type: ignore[method-assign]
    cls._attach_product = attach_product_fixed  # type: ignore[method-assign]
    cls.publish = publish_fixed  # type: ignore[method-assign]
    cls._caption_hashtag_patched_v2 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v3 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v4 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v5 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v6 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v7 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v8 = True  # type: ignore[attr-defined]
    cls._caption_hashtag_patched_v9 = True  # type: ignore[attr-defined]
    logger.info(
        "已应用补丁：话题从联想面板按真实作品数选中长尾，逐字敲键点选（手机端可点）"
    )
