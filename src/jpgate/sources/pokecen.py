"""ポケモンセンターオンライン の走査。

## 取得経路の事実（2026-08-18 実測）

- robots.txt は `User-agent: * / Disallow:` で**全面許可**。
- **正直な User-Agent で 200 が返る。** p-bandai と違いブラウザを騙る必要が
  無いので、素性を名乗るUAを使う。名乗れる相手に名乗らない理由は無い。
- カテゴリURL（例 `/plush-toys/plush/`）が**そのまま一覧ページ**になる。
  カテゴリの一覧は `sitemap_0.xml` に98件。
- ページングは Salesforce Commerce Cloud の `?start=<件目>&sz=<件数>`。
- 商品カードは `<li class="product" data-pid="<JAN>-M">`。
  商品名・価格・画像・商品URL(`/<pid>.html`)が入っている。

## この元では在庫状態が取れない

`<ul class="tagList">` は**常に空**で、在庫や抽選の表示が一覧に出ない。
在庫フラグは商品ページの `<input id="availability" value="true">` にしか無く、
数千点を毎時叩くわけにはいかない。

したがってこのソースが出せるのは **「掲載が新しく現れた」＝新商品/発売開始**
だけで、再販や売り切れは**観測できない**。全商品を `STATUS_LISTED`
（在庫不明）で登録し、`ON_SALE`（売っていると断定）とは混ぜない。

これは設計と噛み合っている。イベントは元々「在ること」からしか作らないので、
状態遷移を観測できないソースを足しても誤通知は増えない。
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from html import unescape

from ..models import STATUS_LISTED, CrawlResult, Item

SOURCE = "pokemon-center"
BASE = "https://www.pokemoncenter-online.com"

#: 素性を名乗るUA。このサイトは騙らなくても200を返す。
_UA = "JPGate/0.1 (+https://github.com/tknjnsk/jpgate)"
_HEADERS = {"User-Agent": _UA, "Accept-Language": "ja"}

_RE_CARD = re.compile(r'<li class="product" data-pid="([^"]+)">(.*?)</li>', re.S)
_RE_TITLE = re.compile(r'<p class="txt">\s*<a[^>]*>(.*?)</a>', re.S)
_RE_PRICE = re.compile(r'<p class="price[^"]*">.*?([\d,]+)\s*<small>円</small>', re.S)
_RE_IMG = re.compile(r'<div class="pho">\s*<a[^>]*>\s*<img src="([^"]+)"', re.S)
_RE_TAG = re.compile(r"<[^>]+>")


class ListingError(RuntimeError):
    """一覧ページとして読めなかった。空の結果と区別するための例外。"""


#: 順番待ち(バーチャルウェイティングルーム)へ飛ばされたときの転送先ホスト。
#:
#: 大型発売の前後に、サイト全体がここへリダイレクトされる。**これは障害では
#: なく、店が意図的に入口を絞っている状態**なので、迂回してはいけない。
#: 迂回は相手が立てたアクセス制御を破ることであり、実際の客の順番も奪う。
#: 待って、次の走査で通ればよい。
_WAITING_ROOM_HOST = "wr.pokemoncenter-online.com"


class _DetectWaitingRoom(urllib.request.HTTPRedirectHandler):
    """順番待ちへの転送を、意味の分かる例外に変える。

    そのままだと urllib が「無限リダイレクト」として失敗し、ログには
    HTTP の内部事情しか残らない。原因が読み取れないと、設定を疑って
    カテゴリ名を書き換えるといった見当違いの対処を誘発する。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _WAITING_ROOM_HOST in newurl:
            raise ListingError(
                "ポケモンセンターが順番待ち中です。走査は迂回せず見送ります "
                f"(転送先 {_WAITING_ROOM_HOST})。混雑が引けば次の走査で通ります。"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_DetectWaitingRoom)


def _fetch(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    return unescape(_RE_TAG.sub("", text)).replace(" ", " ").strip()


def _assert_listing_page(html: str, url: str) -> None:
    """一覧ページであることを確かめる。

    カテゴリ名を間違えたときに 404 ではなく別ページが返る可能性があるため、
    商品カードのマークアップが在ることを明示的に要求する。
    p-bandai で存在しないショップ名がトップページを返した事故と同じ対策。
    """
    if '<li class="product"' not in html and "product-grid" not in html:
        raise ListingError(
            f"一覧ページのマークアップが無い（別ページが返っている可能性）: {url} "
            f"len={len(html)}"
        )


def parse_listing(html: str, shop: str, url: str) -> tuple[list[Item], set[str]]:
    """一覧HTML → Item のリスト。

    第2要素は未知フラグの集合で、この元では常に空。p-bandai と戻り値の形を
    揃えて呼び出し側を単純にするために置いている。将来 `tagList` に語彙が
    現れたらここで拾う。
    """
    _assert_listing_page(html, url)

    items: list[Item] = []
    unknown: set[str] = set()

    for pid, block in _RE_CARD.findall(html):
        m_title = _RE_TITLE.search(block)
        if not m_title:
            continue
        m_price = _RE_PRICE.search(block)
        m_img = _RE_IMG.search(block)

        # 在庫表示が出るようになったら気づけるように、空でないタグを拾って報告する。
        m_tags = re.search(r'<ul class="tagList">(.*?)</ul>', block, re.S)
        if m_tags:
            for raw in re.findall(r">([^<>]+)<", m_tags.group(1)):
                if raw.strip():
                    unknown.add(raw.strip())

        items.append(
            Item(
                source=SOURCE,
                shop=shop,
                item_id=pid,
                title=_clean(m_title.group(1)),
                url=f"{BASE}/{pid}.html",
                price_jpy=int(m_price.group(1).replace(",", "")) if m_price else None,
                image=m_img.group(1) if m_img else None,
                summary="",
                icons=(),
                ship_month=None,
                # 在庫は一覧から観測できない。売っていると断定しない。
                status_hint=STATUS_LISTED,
            )
        )

    return items, unknown


def crawl_shop(
    shop: str,
    *,
    max_pages: int = 3,
    per_page: int = 48,
    delay_sec: float = 1.5,
    timeout: int = 60,
    fetch=_fetch,
) -> CrawlResult:
    """1カテゴリを走査する。`shop` はカテゴリパス（例 `plush-toys/plush`）。

    p-bandai の `crawl_shop` と同じ形にしてある（CLI が両方を同じ扱いで
    呼べるようにするため）。
    """
    result = CrawlResult(source=SOURCE, shop=shop, ok=True)
    seen: set[str] = set()

    for page in range(max_pages):
        url = f"{BASE}/{shop.strip('/')}/?start={page * per_page}&sz={per_page}"
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

        fresh = [i for i in items if i.item_id not in seen]
        if not fresh:
            # 同じ商品しか返らない＝ページ送りが効いていないか末尾。
            # 空振りを繰り返さずに打ち切る。
            break
        for item in fresh:
            seen.add(item.item_id)
            result.items.append(item)

        if len(items) < per_page:
            break
        if page + 1 < max_pages:
            time.sleep(delay_sec)

    if result.ok and not result.items:
        result.ok = False
        result.error = (
            f"{shop}: 1件も取れなかった。一覧の構造が変わったか遮断された可能性。"
        )

    return result
