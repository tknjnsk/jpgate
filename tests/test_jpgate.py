"""検証するのは値の妥当性ではなく、破れてはいけない制約。

このツールが起こしうる事故は2種類しかない:
  A. 起きていないことを通知する（誤報 → 信用を失う）
  B. 起きたことを取りこぼす（機会損失）

A のほうが致命的なので、A を構造的に防いでいることをテストで固定する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jpgate.affiliate import AffiliateConfig, links_for, search_query
from jpgate.gates import G1_JP_PHONE, G2_JP_ADDRESS, G3_JP_PAYMENT, SourceGate, evaluate
from jpgate.models import (
    EVENT_LOTTERY_OPEN,
    EVENT_RESERVATION_OPEN,
    EVENT_RESTOCK,
    ICON_IN_STOCK,
    ICON_LOT_SALES,
    ICON_ORDER_PRODUCTION,
    ICON_OUT_OF_STOCK,
    ICON_RESERVE,
    ICON_RESERVE_END,
    ICON_SALE_END,
    SHUT_STATUSES,
    STATUS_CLOSED,
    STATUS_LOTTERY,
    STATUS_ON_SALE,
    STATUS_RESERVATION,
    STATUS_SALE_END,
    STATUS_SOLD_OUT,
    CrawlResult,
    Item,
)
from jpgate.sources import pbandai
from jpgate.store import Store
from jpgate.translate import Glossary

FIXTURE = Path(__file__).parent / "fixtures" / "listing.html"


def item(item_id="1", icons=(), **kw) -> Item:
    return Item(
        source="p-bandai",
        shop="tamashiiwebshouten",
        item_id=item_id,
        title=kw.get("title", "テスト商品"),
        url=f"https://p-bandai.jp/item/item-{item_id}/",
        price_jpy=kw.get("price_jpy", 1000),
        image=None,
        summary="",
        icons=tuple(icons),
        ship_month=kw.get("ship_month"),
    )


# --------------------------------------------------------------------------
# 状態の導出
# --------------------------------------------------------------------------
def test_ended_lottery_is_not_open():
    """抽選アイコンは終了後も残る。終了を先に見ないと終わった抽選を通知する。"""
    assert item(icons=[ICON_LOT_SALES, ICON_RESERVE_END]).status == STATUS_CLOSED
    assert item(icons=[ICON_LOT_SALES]).status == STATUS_LOTTERY


def test_sold_out_beats_reserve():
    assert item(icons=[ICON_RESERVE, ICON_OUT_OF_STOCK]).status == STATUS_SOLD_OUT


def test_sale_end_is_shut_not_on_sale():
    """販売終了アイコンを知らないと「状態なし＝販売中」と読み、
    終わった商品を公開ページに買えるものとして並べてしまう。"""
    assert item(icons=[ICON_SALE_END]).status == STATUS_SALE_END
    assert STATUS_SALE_END in SHUT_STATUSES


def test_attribute_icons_do_not_change_status():
    """在庫商品/受注生産は属性であって状態ではない。"""
    assert item(icons=[ICON_IN_STOCK]).status == STATUS_ON_SALE
    assert item(icons=[ICON_ORDER_PRODUCTION, ICON_RESERVE]).status == STATUS_RESERVATION


# --------------------------------------------------------------------------
# ゲート判定
# --------------------------------------------------------------------------
def test_no_evidence_is_unknown_not_absent():
    """根拠が無い状態を「ゲート無し」と混ぜない。UNKNOWN では売らない。"""
    verdict = evaluate(item(), [])
    assert verdict.unknown is True
    assert verdict.sellable is False


def test_lottery_icon_proves_phone_and_payment_gates():
    verdict = evaluate(item(icons=[ICON_LOT_SALES]), [])
    assert G1_JP_PHONE in verdict.keys
    assert G3_JP_PAYMENT in verdict.keys
    assert verdict.sellable is True


def test_source_gate_requires_evidence_and_date():
    """根拠と確認日の無いゲートは宣言できない（推定を宣言に昇格させない）。"""
    with pytest.raises(ValueError):
        SourceGate.from_config({"key": G2_JP_ADDRESS, "evidence": "", "verified_on": "2026-01-01"})
    with pytest.raises(ValueError):
        SourceGate.from_config({"key": G2_JP_ADDRESS, "evidence": "国内のみ", "verified_on": ""})
    with pytest.raises(ValueError):
        SourceGate.from_config({"key": "G9_NOPE", "evidence": "x", "verified_on": "2026-01-01"})


# --------------------------------------------------------------------------
# パーサ: 静かに嘘を返さないこと
# --------------------------------------------------------------------------
def test_non_listing_page_raises_instead_of_returning_empty():
    """存在しないショップ名でトップページが返る事故が実際にあった。

    0件を返すと「全部売り切れた」と読まれるので、例外にする。
    """
    with pytest.raises(pbandai.ListingError):
        pbandai.parse_listing("<html><body>トップページ</body></html>", "kids", "u")


def test_parses_real_card():
    html = FIXTURE.read_text(encoding="utf-8")
    items, unknown = pbandai.parse_listing(html, "tamashiiwebshouten", "u")
    assert len(items) == 2
    first = items[0]
    assert first.item_id == "1000255333"
    assert first.price_jpy == 39600
    assert first.status == STATUS_LOTTERY
    assert first.ship_month == "2026-11"
    assert unknown == set()


def test_unknown_icon_is_surfaced_not_swallowed():
    """語彙が増えたとき、新しい状態を「状態なし」と読まないための報告経路。"""
    html = FIXTURE.read_text(encoding="utf-8").replace(
        "ITEM_LOT_SALES.gif", "ITEM_BRAND_NEW_THING.gif"
    )
    _, unknown = pbandai.parse_listing(html, "tamashiiwebshouten", "u")
    assert "ITEM_BRAND_NEW_THING" in unknown


def test_crawl_marks_failure_when_nothing_found():
    def fetch(url, timeout):
        return "<html>トップページ</html>"

    result = pbandai.crawl_shop("kids", max_pages=1, fetch=fetch)
    assert result.ok is False
    assert result.items == []


# --------------------------------------------------------------------------
# 差分: 誤通知を構造的に出さないこと
# --------------------------------------------------------------------------
def crawl(*items) -> CrawlResult:
    return CrawlResult(source="p-bandai", shop="tamashiiwebshouten", ok=True, items=list(items))


def test_first_crawl_emits_no_events(tmp_path):
    """初回に数万件の「予約開始」を撒かないための seed 規則。"""
    store = Store(tmp_path / "t.sqlite")
    events = store.apply(crawl(item("1", [ICON_LOT_SALES]), item("2", [ICON_RESERVE])))
    assert events == []
    store.close()


def test_new_item_after_seed_is_announced(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_RESERVE])))
    events = store.apply(crawl(item("1", [ICON_RESERVE]), item("2", [ICON_LOT_SALES])))
    assert [(e.item_id, e.kind) for e in events] == [("2", EVENT_LOTTERY_OPEN)]
    store.close()


def test_missing_item_never_creates_an_event(tmp_path):
    """一覧の先頭数ページしか見ないので、載っていない＝終了ではない。

    「無いこと」からイベントを作らないので、走査ページ数を変えても
    誤通知が増えない。この性質が走査範囲を自由に動かせる根拠になっている。
    """
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_RESERVE]), item("2", [ICON_RESERVE])))
    events = store.apply(crawl(item("1", [ICON_RESERVE])))
    assert events == []
    store.close()


def test_failed_crawl_is_not_applied(tmp_path):
    """落ちた走査を反映すると、取れなかった商品が消えたように見える。"""
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_RESERVE])))
    bad = CrawlResult(source="p-bandai", shop="tamashiiwebshouten", ok=False, error="boom")
    assert store.apply(bad) == []
    # 反映されていないので、次の正常な走査でも状態は保たれている
    assert store.apply(crawl(item("1", [ICON_RESERVE]))) == []
    store.close()


def test_failed_crawl_does_not_count_as_seed(tmp_path):
    """失敗を seed とみなすと、次の成功で全件が新着として通知される。"""
    store = Store(tmp_path / "t.sqlite")
    store.apply(CrawlResult(source="p-bandai", shop="tamashiiwebshouten", ok=False, error="x"))
    assert store.has_successful_crawl("p-bandai", "tamashiiwebshouten") is False
    assert store.apply(crawl(item("1", [ICON_LOT_SALES]))) == []
    store.close()


def test_reopen_is_reported_as_restock_not_as_start(tmp_path):
    """「予約終了 → 抽選受付中」は再実施。開始として出すと最重要の事実が消える。"""
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_LOT_SALES, ICON_RESERVE_END])))
    events = store.apply(crawl(item("1", [ICON_LOT_SALES])))
    assert [e.kind for e in events] == [EVENT_RESTOCK]
    store.close()


def test_stable_item_does_not_repeat_events(tmp_path):
    """同じ状態を見続けても通知は1回だけ。毎時実行しても連投しない。"""
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_RESERVE])))
    store.apply(crawl(item("1", [ICON_RESERVE]), item("2", [ICON_RESERVE])))
    for _ in range(3):
        assert store.apply(crawl(item("1", [ICON_RESERVE]), item("2", [ICON_RESERVE]))) == []
    store.close()


def test_on_sale_first_sighting_is_silent(tmp_path):
    """既に売っていた商品が一覧の見える範囲に入っただけ。新情報ではない。"""
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_RESERVE])))
    assert store.apply(crawl(item("1", [ICON_RESERVE]), item("9", []))) == []
    store.close()


# --------------------------------------------------------------------------
# アフィリエイト: 出さないほうが正しい場面で出さないこと
# --------------------------------------------------------------------------
ENABLED = AffiliateConfig(ebay_campaign_id="5338888888", amazon_us_tag="jpgate-20")


def test_no_links_without_ids():
    """審査に通る前にリンクを出すと規約違反。既定で無効。"""
    links = links_for(
        status=STATUS_SOLD_OUT,
        title_en="METAL BUILD God Gundam",
        gate_sellable=True,
        lottery=False,
        cfg=AffiliateConfig(),
    )
    assert links == []


def test_lottery_is_never_affiliated():
    """抽選は転送代行では原理的に代行できない＝自社の堀。他社に流さない。"""
    links = links_for(
        status=STATUS_SOLD_OUT,
        title_en="METAL BUILD God Gundam",
        gate_sellable=True,
        lottery=True,
        cfg=ENABLED,
    )
    assert links == []


def test_open_items_are_not_affiliated():
    """一次流通が生きているものを二次流通に流す理由がない。"""
    for status in (STATUS_RESERVATION, STATUS_ON_SALE):
        assert (
            links_for(
                status=status,
                title_en="METAL BUILD God Gundam",
                gate_sellable=True,
                lottery=False,
                cfg=ENABLED,
            )
            == []
        )


def test_closed_items_get_links():
    links = links_for(
        status=STATUS_SALE_END,
        title_en="METAL BUILD God Gundam",
        gate_sellable=True,
        lottery=False,
        cfg=ENABLED,
    )
    assert len(links) == 2
    assert all(l.sponsored for l in links)
    assert "campid=5338888888" in links[0].url
    assert "tag=jpgate-20" in links[1].url


def test_query_drops_labels_and_japanese():
    """日本語混じりの語で検索させても0件になる。無意味なリンクは出さない。"""
    q = search_query("【Event Exclusive／Advance CTM Lottery】METAL BUILD ゴッドガンダム（明鏡止水）")
    assert "METAL BUILD" in q
    assert "Lottery" not in q
    assert "ゴッド" not in q


def test_untranslatable_title_yields_no_link():
    """英字が残らなかった商品は検索語として成立しないのでリンクを出さない。"""
    links = links_for(
        status=STATUS_SALE_END,
        title_en="【抽選販売】ねんどろいど 明鏡止水",
        gate_sellable=True,
        lottery=False,
        cfg=ENABLED,
    )
    assert links == []


def test_closed_lottery_card_sells_own_service_not_affiliate(tmp_path):
    """終了した抽選のカードは、アフィリではなく自社の次回案内を出す。

    抽選は転送代行では原理的に代行できないので、ここを他社に流すと
    自分の堀を売ることになる。カードの描画まで含めて固定する。
    """
    from jpgate import config as cm
    from jpgate.publish import render_site

    store = Store(tmp_path / "t.sqlite")
    store.apply(
        crawl(item("1", [ICON_LOT_SALES, ICON_OUT_OF_STOCK], title="METAL BUILD Test"))
    )
    cfg = cm.load()
    cfg.affiliate = ENABLED
    html = render_site([], {}, Glossary({}), cfg, closed_rows=store.recently_closed())
    assert "Ask us to enter the next round" in html
    assert "sponsored nofollow" not in html
    store.close()


def test_notify_marks_only_after_send(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_RESERVE])))
    store.apply(crawl(item("1", [ICON_RESERVE]), item("2", [ICON_LOT_SALES])))
    pending = store.pending_events()
    assert len(pending) == 1
    store.mark_notified([pending[0]["id"]])
    assert store.pending_events() == []
    store.close()
