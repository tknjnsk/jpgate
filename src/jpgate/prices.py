"""eBay US の相場を引く。「日本ではいくら / 海外ではいくら」を出すため。

## なぜ出すのか

海外の客が本当に知りたいのは「その商品が日本にある」ことではなく
**「自分の国で買うといくら損か」**。差額はそのまま代行の価値の説明になる。
薄いまとめは検索で評価されないが、**独自の価格データがあるページは別物**。

## 一番危険なこと

**別の商品の値段を出すこと。** 「日本で¥2,860 / eBayで$300」は儲け話では
なく、たいてい**無関係な商品を掴んだ証拠**。しかも客はこの数字を見て
こちらの信用を測るので、1件外すと全部の数字が疑われる。

したがってこのモジュールの既定は**「出さない」**。下の4つを全部通った
ときだけ価格を返し、1つでも欠けたら None を返す。**Noneは失敗ではなく
正常な結論**として扱うこと。

  1. 訳出率     : 商品名が英語になっていること。日本語のまま投げると
                  eBay は無関係な商品を返す
  2. 特定語     : その商品を1つに絞る語("Freedom" "Exia" 型番)が
                  1つ以上あること。**グレードや縮尺は数えない** ――
                  "RG" と "1/144" はRGの出品すべてに入るので、
                  これを根拠にすると別キットを掴んでも検知できない
  3. 件数       : 出品が3件以上あること。1〜2件は相場ではない
  4. 一致率     : **返ってきた商品名に特定語が実際に入っている**割合が
                  6割以上。eBay の検索は語を落として部分一致を返すので、
                  クエリを投げただけでは同定にならない

## 限界

Browse API が返すのは**出品中の希望価格で、落札額ではない**。希望価格は
実売より高く出る。落札額には Marketplace Insights API の申請が要る。
サイトには「asking price」と明記して出すこと。実売と偽ってはいけない。

同名の通常版と限定版も混ざる(実測: 小売版RGフリーダム$14 と
プレバン限定版$104 が同じ検索に並んだ)。**だから点ではなく幅で出す。**
"""

from __future__ import annotations

import base64
import json
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_SCOPE = "https://api.ebay.com/oauth/api_scope"

#: 商品を同定する力を持つトークン。ラテン文字2字以上、または型番。
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9'.\-]{1,}")
_MODEL = re.compile(r"\d+/\d+|[A-Za-z0-9]{2,}[-][A-Za-z0-9]{2,}")

#: どの出品にも出てくる語。**アンカーに数えない**。
#:
#: ここを緩めると「検査に通すために検査を緩める」ことになる。たとえば
#: "Gundam" をアンカーにすると、ガンプラの出品は全部一致するので、
#: 別のキットを掴んでも一致率が下がらず検知できない。
_STOPWORDS = {
    "the", "and", "for", "with", "ver", "version", "new", "set", "vol", "no",
    "japan", "japanese", "limited", "edition", "lottery", "pre", "order",
    "shipping", "batch", "bandai", "premium", "gundam", "pokemon", "figure",
    "model", "kit", "anime", "official", "exclusive", "custom",
}

#: 商品名に付く付帯情報。商品そのものではないので落とす。
#:
#: **ただし 【】 の中は先に検査する。** セット番号(【OP-14】【UA55BT】)が
#: そこに入っていることがあり、それは商品を1点に特定する最強の語。
#: 括弧ごと消していたため、カードゲームの相場が全部同じ幅になっていた。
_NOISE = [
    re.compile(r"【[^】]*】"),
    re.compile(r"\[[^\]]*\]"),
    re.compile(r"\([^)]*shipping[^)]*\)", re.I),
]
#: 括弧の中から拾うセット番号。英字+数字で、区切りがあってもなくてもよい。
_SET_CODE = re.compile(r"[【\[]\s*([A-Z]{2,}[-]?\d{1,3}[A-Z]{0,2})\s*[】\]]")
#: 抜き出したあとのセット番号そのもの（訳出率の免除判定に使う）。
_SET_CODE_TOKEN = re.compile(r"[A-Z]{2,}[-]?\d{1,3}[A-Z]{0,2}")

#: 出さない判断をした理由。DBに残して、次回に同じ計算を繰り返さない。
NO_MATCH = "no-match"


@dataclass(frozen=True)
class PriceQuote:
    """eBay US の出品価格。**落札額ではない**。"""

    median_usd: float
    #: 25〜75パーセンタイル。**サイトにはこの幅で出す**（点で出さない）。
    low_usd: float
    high_usd: float
    sample_n: int
    #: アンカーが返ってきた商品名に入っていた割合。低いほど別商品の疑い。
    confidence: float


#: 探す範囲は狭めるが、**商品を1つに特定しない**語。グレードと縮尺。
#:
#: これを同定の根拠に数えてはいけない。RGのキットは出品名に必ず
#: "RG" と "1/144" の両方が入るので、この2つで「2語一致」を満たすと
#: **別のキットを掴んでも一致率が下がらず、検知できなくなる**。
#: 実測でそうなった（RGの相場が全部$41〜$59に張り付いた）。
#: クエリには乗せる（範囲は確実に狭まる）が、確信度には数えない。
_NARROWERS = {
    # ガンプラのグレードと縮尺
    "rg", "hg", "hgu", "mg", "mgex", "pg", "sd", "sdcs", "re", "fm", "eg",
    "1/144", "1/100", "1/60", "1/48", "1/12", "1/8", "1/7", "1/6", "1/4",
    # フィギュアのブランド
    "s.h.figuarts", "figuarts", "figure-rise", "metal", "build", "robot",
    # カードゲーム。**作品名も入れる**: "ONE PIECE" はその作品の出品すべてに
    # 入るので、これを特定語に数えると別の弾を掴んでも一致率が落ちない。
    # 特定するのはセット番号(OP-14)のほう。
    "one", "piece", "card", "game", "booster", "pack", "deck", "starter",
    "union", "arena", "digimon", "carddass", "collection", "box",
}


def anchors(title_en: str) -> tuple[list[str], list[str]]:
    """(特定語, 絞り込み語) を返す。

    特定語 = その商品を1つに絞る力のある語（"Freedom" "Exia" 型番）。
    絞り込み語 = グレードや縮尺。検索範囲は狭めるが同定はしない。

    **特定語が1つも無い商品は相場を出さない。** 出せば必ず別商品の値段になる。
    """
    # 括弧を落とす前にセット番号を確保する。
    codes = _SET_CODE.findall(title_en)
    text = title_en
    for pat in _NOISE:
        text = pat.sub(" ", text)
    text = " ".join(codes) + " " + text
    key: list[str] = []
    narrow: list[str] = []
    seen: set[str] = set()
    for tok in _MODEL.findall(text) + _LATIN.findall(text):
        low = tok.lower().strip(".-'")
        if len(low) < 2 or low in _STOPWORDS or low.isdigit() or low in seen:
            continue
        seen.add(low)
        (narrow if low in _NARROWERS else key).append(tok)
    return key, narrow


class EbayClient:
    """Browse API の最小クライアント。

    KujiRadar が同等のものを持っている。**別々に持っているのは意図的**で、
    片方の相場ロジックを変えたときにもう片方の仕入れ判定が黙って変わる
    ほうが危ない。共有するならライブラリに切り出すこと。
    """

    def __init__(self, client_id: str, client_secret: str, marketplace: str = "EBAY_US"):
        self._id = client_id
        self._secret = client_secret
        self._marketplace = marketplace
        self._token: str | None = None
        self._expires_at = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": _SCOPE}
        ).encode()
        req = urllib.request.Request(
            _TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 7200))
        return self._token

    def search(self, query: str, limit: int = 50) -> list[tuple[float, str]]:
        """(価格USD, 商品名) の一覧。失敗しても例外を投げず空を返す。

        価格が引けないことは事業を止める理由にならない（サイトは価格が
        無くても成立する）。走査や公開を巻き込んで落とすほうが害が大きい。
        """
        url = f"{_SEARCH_URL}?" + urllib.parse.urlencode(
            {
                "q": query,
                "limit": str(min(limit, 200)),
                # オークションの途中価格は相場ではないので即決のみ。
                "filter": "buyingOptions:{FIXED_PRICE}",
            }
        )
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "X-EBAY-C-MARKETPLACE-ID": self._marketplace,
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            return []

        out: list[tuple[float, str]] = []
        for entry in data.get("itemSummaries") or []:
            price = entry.get("price") or {}
            if price.get("currency") != "USD":
                continue
            try:
                out.append((float(price["value"]), str(entry.get("title", ""))))
            except (KeyError, TypeError, ValueError):
                continue
        return out


#: 出す/出さないの敷居。**緩めるときは「なぜ緩めてよいか」を書くこと**。
MIN_ANCHORS = 2
MIN_SAMPLES = 3
MIN_CONFIDENCE = 0.6


def quote(
    client: EbayClient,
    title_en: str,
    coverage: float,
    min_coverage: float,
) -> PriceQuote | None:
    """相場を1件引く。**確信が持てなければ None**（それが正常な結論）。

    `coverage` は Glossary の訳出率。日本語が残ったままのクエリを投げると
    eBay は語を落として無関係な商品を返すので、ここで先に落とす。

    **ただしセット番号があれば訳出率は問わない。** 訳出率は「同定できるか」
    の代理指標にすぎず、"OP-14" のような番号はその直接の証拠だから。
    代理指標を直接証拠より優先するのは順序が逆。
    """
    key, narrow = anchors(title_en)
    has_code = any(_SET_CODE_TOKEN.fullmatch(k) for k in key)
    if coverage < min_coverage and not has_code:
        return None
    if not key:
        # 特定語ゼロ。出せば必ず別商品の値段になるので引きにも行かない。
        return None
    # 特定語が1語しかない商品は珍しくない("RG 1/144 Freedom Gundam" は
    # Gundam が汎用語、RG と 1/144 が絞り込み語なので Freedom だけ残る)。
    # その場合は「その1語が入っていること」を要求する。
    need = min(MIN_ANCHORS, len(key))

    # クエリには絞り込み語も乗せる（範囲は確実に狭まる）。
    hits = client.search(" ".join(key[:5] + narrow[:3]))
    if len(hits) < MIN_SAMPLES:
        return None

    # **一致の判定は特定語だけで行う。** 絞り込み語を混ぜると、
    # 同じグレードの別キットが全部「一致」になる。
    needles = [k.lower() for k in key]
    matched = [
        (price, name)
        for price, name in hits
        if sum(n in name.lower() for n in needles) >= need
    ]
    confidence = len(matched) / len(hits)
    if confidence < MIN_CONFIDENCE or len(matched) < MIN_SAMPLES:
        return None

    prices = sorted(p for p, _ in matched)
    # 幅で出す。**1つの数字に丸めない。**
    # 同じ商品名でも通常版と限定版、新品と中古が混ざる（実測で
    # 小売版RGフリーダム$14 とプレバン限定版$104 が同じ検索に並んだ）。
    # 点で出すと「その値段で売れる」と読まれるが、それは保証できない。
    lo = prices[int(len(prices) * 0.25)]
    hi = prices[min(len(prices) - 1, int(len(prices) * 0.75))]
    return PriceQuote(
        median_usd=statistics.median(prices),
        low_usd=lo,
        high_usd=hi,
        sample_n=len(matched),
        confidence=confidence,
    )
