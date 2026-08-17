"""プレミアムバンダイ 一覧ページの走査。

## 取得経路の事実（2026-08-17 実測）

- `https://p-bandai.jp/<shop>/list-da<件数>-n<オフセット>/` は素のHTTPで実HTMLを返す。
  robots.txt が禁じているのは `/search/` `/mypage/` 等で、この一覧は対象外。
  sitemap-index.xml からも同種の一覧URLが公開されている。
- **商品詳細 `/item/item-<id>/` はボット判定でHTMLが返らない**（中身の無い
  チャレンジシェルが返る。Playwright のヘッドレスも「アクセス制限」で弾かれた）。
  よって詳細ページに依存する設計にしてはいけない。幸い一覧カードに
  ID・商品名・価格・状態アイコン・発送月が全部載っているので詳細は要らない。
- User-Agent が素（`Mozilla/5.0` だけ等）だとトップページすらチャレンジになる。
  ブラウザ相当のUAが必須。

## 静かに嘘を返す経路（実際に踏んだ）

存在しないショップ名を渡すと **404 にならずトップページが返る**。
`kids` で 157KB のトップページを掴んだ。商品0件を「売り切れ」と読むと
全商品に再販通知を出しかねないので、パースは必ず
`_assert_listing_page()` を通す。
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request

from ..models import KNOWN_ICONS, CrawlResult, Item

SOURCE = "p-bandai"
BASE = "https://p-bandai.jp"

#: 素のUAだとチャレンジに落ちるので固定。
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9",
}

#: 一覧ページは Shift_JIS。UTF-8 で読むと日本語が全部壊れる。
_ENCODING = "cp932"

_RE_CARD = re.compile(r'<div class="article_area">(.*?)<!-- / article_area -->', re.S)
_RE_ITEM_ID = re.compile(r"/item/item-(\d+)/")
_RE_TITLE = re.compile(r'<p class="article_title">\s*<a[^>]*>(.*?)</a>', re.S)
_RE_PRICE = re.compile(r'<p class="price">([\d,]+)\s*円')
_RE_SUMMARY = re.compile(r'<p class="summary">(.*?)</p>', re.S)
_RE_IMG = re.compile(r'<div class="article_photo_s">\s*<a[^>]*>\s*<img src="([^"]+)"', re.S)
_RE_ICON = re.compile(r"/bc/img/icon/([A-Za-z_0-9]+)\.gif")
_RE_TOTAL_PAGES = re.compile(r"（全(\d+)ページ）")
_RE_SHIP_MONTH = re.compile(r"^RESERVE_(\d{4})(\d{2})$")
_RE_TAG = re.compile(r"<[^>]+>")


class ListingError(RuntimeError):
    """一覧ページとして読めなかった。空の結果と区別するための例外。"""


def _fetch(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode(_ENCODING, errors="replace")


def _assert_listing_page(html: str, url: str) -> None:
    """一覧ページであることを確かめる。

    存在しないショップ名でトップページが返る事故があるため、
    「商品カードのマークアップが在る」ことを明示的に要求する。
    """
    if '<div class="article_area">' not in html:
        raise ListingError(
            f"一覧ページのマークアップが無い（別ページが返っている可能性）: {url} "
            f"len={len(html)}"
        )


def _clean(text: str) -> str:
    return _RE_TAG.sub("", text).replace("&amp;", "&").replace("&nbsp;", " ").strip()


def parse_listing(html: str, shop: str, url: str) -> tuple[list[Item], set[str]]:
    """一覧HTML → Item のリストと、未知アイコンの集合。

    未知アイコンを握り潰さずに返すのは、販売元が語彙を増やしたときに
    黙って取りこぼさないため（新しい状態を「状態なし」と読むと誤判定になる）。
    """
    _assert_listing_page(html, url)

    items: list[Item] = []
    unknown: set[str] = set()

    for block in _RE_CARD.findall(html):
        m_id = _RE_ITEM_ID.search(block)
        m_title = _RE_TITLE.search(block)
        if not m_id or not m_title:
            continue

        raw_icons = _RE_ICON.findall(block)
        icons: list[str] = []
        ship_month: str | None = None
        for name in raw_icons:
            m_month = _RE_SHIP_MONTH.match(name)
            if m_month:
                ship_month = f"{m_month.group(1)}-{m_month.group(2)}"
                continue
            if name not in KNOWN_ICONS:
                unknown.add(name)
                continue
            icons.append(name)

        m_price = _RE_PRICE.search(block)
        m_img = _RE_IMG.search(block)
        summaries = [_clean(s) for s in _RE_SUMMARY.findall(block)]

        item_id = m_id.group(1)
        items.append(
            Item(
                source=SOURCE,
                shop=shop,
                item_id=item_id,
                title=_clean(m_title.group(1)),
                url=f"{BASE}/item/item-{item_id}/",
                price_jpy=int(m_price.group(1).replace(",", "")) if m_price else None,
                image=m_img.group(1) if m_img else None,
                summary=next((s for s in summaries if s), ""),
                icons=tuple(sorted(set(icons))),
                ship_month=ship_month,
            )
        )

    return items, unknown


def total_pages(html: str) -> int | None:
    m = _RE_TOTAL_PAGES.search(html)
    return int(m.group(1)) if m else None


def crawl_shop(
    shop: str,
    *,
    max_pages: int = 5,
    per_page: int = 20,
    delay_sec: float = 1.5,
    timeout: int = 60,
    fetch=_fetch,
) -> CrawlResult:
    """1ショップを新着順に `max_pages` ページ走査する。

    一覧の既定の並びは新着順なので、先頭数ページだけで「今日始まったもの」は
    拾える。取りこぼしが怖いジャンルは `max_pages` を上げること。

    1ページでも落ちたら `ok=False` にする。部分成功を成功として扱うと、
    落ちたページの商品が「消えた」ように見えるため。
    """
    result = CrawlResult(source=SOURCE, shop=shop, ok=True)
    seen: set[str] = set()

    for n in range(max_pages):
        url = f"{BASE}/{shop}/list-da{per_page}-n{n}/"
        try:
            html = fetch(url, timeout)
            items, unknown = parse_listing(html, shop, url)
        except (urllib.error.URLError, ListingError, OSError) as exc:
            result.ok = False
            result.pages_failed += 1
            result.error = f"{url}: {type(exc).__name__}: {exc}"
            break

        result.pages_fetched += 1
        result.unknown_icons |= unknown

        if not items:
            # 一覧のマークアップは在るのにカードが0件。最終ページを越えた場合に
            # 起きうるので、ここは失敗にせず打ち切る。
            break

        for item in items:
            if item.item_id in seen:
                continue
            seen.add(item.item_id)
            result.items.append(item)

        pages = total_pages(html)
        if pages is not None and n + 1 >= pages:
            break

        if n + 1 < max_pages:
            time.sleep(delay_sec)

    if result.ok and not result.items:
        result.ok = False
        result.error = (
            f"{shop}: 1件も取れなかった。一覧の構造が変わったか遮断された可能性。"
        )

    return result
