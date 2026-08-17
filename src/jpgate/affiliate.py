"""アフィリエイトリンクの生成。

## なぜ「検索リンク」しか作らないのか

商品への直リンクは「この出品はその商品だ」と断言することになる。
日英の商品同定は [[KujiRadar]] を最も苦しめた問題で、裸の採番は世界的に
非ユニーク（`15/89` がタイヤに、`N4/N25` がフォトカプラに当たる）。
断言した先が別商品だと、こちらが嘘をついたことになる。

**検索リンクは何も断言しない。**「この語で探すとここに出る」以上のことを
言わないので、名寄せが外れても嘘にならない。収益は多少落ちるが、
このサイトの資産は信用なので、そこを削って得る収益は割に合わない。

同じ理由で「$XX で買えます」のような価格表示もしない。価格を出すなら
実在を保証する義務が生じ、Browse API の一致率ガードが要る。

## ゲートによる振り分け

- 抽選（G1）: **アフィリを出さない。** 転送代行では原理的に代行できない
  （アカウント作成のSMS認証を越えられない）ここが自社の堀。
  ここを他社に流すのは堀を売ることに等しい。
- 終了/売り切れ: 二次流通しか経路が残っていないので eBay / Amazon。
- それ以外: 一次流通が生きているので、まず公式へ誘導する。
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .models import SHUT_STATUSES

#: 検索語から落とす語。販売形態のラベルや記号は二次流通の検索では雑音になる。
_STRIP = re.compile(
    r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)|"
    r"Lottery|Pre-order|Restock|Event Exclusive|Advance CTM Lottery|"
    r"Store Event Exclusive|Premium Bandai Exclusive|Tamashii Web Shop Exclusive|"
    r"Made-to-Order|Exclusive",
    re.I,
)
_RE_JA = re.compile(r"[぀-ヿ一-鿿]")


@dataclass(frozen=True)
class AffiliateConfig:
    """審査に通るまで ID は空。空のあいだリンクは一切生成されない。"""

    ebay_campaign_id: str = ""
    amazon_us_tag: str = ""

    @property
    def any_enabled(self) -> bool:
        return bool(self.ebay_campaign_id or self.amazon_us_tag)


@dataclass(frozen=True)
class Link:
    label: str
    url: str
    #: 広告であることの明示。ステマ規制（景品表示法）と各プログラムの規約の両方で必須。
    sponsored: bool = True


def search_query(title_en: str) -> str:
    """英語タイトルから二次流通の検索語を作る。

    日本語が残っていたら落とす。eBay/Amazon の海外出品タイトルは英字なので、
    日本語を混ぜると検索が0件になる（「探せない語で探させる」ほうが
    誤った商品に当てるより害が小さいとはいえ、無意味なリンクは出さない）。
    """
    cleaned = _STRIP.sub(" ", title_en)
    cleaned = _RE_JA.sub(" ", cleaned)
    cleaned = re.sub(r"[／/、,・]+", " ", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def links_for(
    *,
    status: str,
    title_en: str,
    gate_sellable: bool,
    lottery: bool,
    cfg: AffiliateConfig,
) -> list[Link]:
    """1商品に出すアフィリリンク。出さないほうが正しい場面では空を返す。"""
    if lottery:
        # 堀。他社に流さない。
        return []
    if status not in SHUT_STATUSES:
        # 一次流通が生きている。公式で買えるものを二次流通に流す理由がない。
        return []
    if not cfg.any_enabled:
        return []

    query = search_query(title_en)
    if len(query) < 4:
        # 検索語として成立していない。空振りリンクは出さない。
        return []

    out: list[Link] = []
    if cfg.ebay_campaign_id:
        out.append(Link("Search eBay", _ebay_url(query, cfg.ebay_campaign_id)))
    if cfg.amazon_us_tag:
        out.append(Link("Search Amazon", _amazon_url(query, cfg.amazon_us_tag)))
    return out


def _ebay_url(query: str, campaign_id: str) -> str:
    """eBay Partner Network のトラッキング付き検索URL。

    EPN は任意の ebay.com URL にトラッキングパラメータを足す方式。
    mkrid は US サイト(EBAY-US)のロータリーID。他国サイトに向けるなら要変更。
    """
    params = {
        "_nkw": query,
        "mkcid": "1",
        "mkrid": "711-53200-19255-0",
        "siteid": "0",
        "campid": campaign_id,
        "toolid": "10001",
        "mkevt": "1",
    }
    return "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode(params)


def _amazon_url(query: str, tag: str) -> str:
    return "https://www.amazon.com/s?" + urllib.parse.urlencode({"k": query, "tag": tag})


#: ページに必ず出す開示文。
#: 景品表示法のステマ規制（2023年10月〜）と EPN / Amazon アソシエイトの
#: 規約の両方が要求する。出し忘れると規約違反でアカウントが飛ぶ。
DISCLOSURE_EN = (
    "Some outbound links on this page are affiliate links. "
    "If you buy through them we may earn a commission, at no extra cost to you. "
    "We link to searches, not to specific listings — we do not claim any listing "
    "is the item shown. As an eBay Partner and an Amazon Associate we earn from "
    "qualifying purchases."
)
