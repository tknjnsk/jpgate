"""検証するのは値の妥当性ではなく、破れてはいけない制約。

このツールが起こしうる事故は2種類しかない:
  A. 起きていないことを通知する（誤報 → 信用を失う）
  B. 起きたことを取りこぼす（機会損失）

A のほうが致命的なので、A を構造的に防いでいることをテストで固定する。
"""

from __future__ import annotations

import sys
from datetime import date
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
    DISPLAY_STATUSES,
    EVENT_NEW_LISTING,
    OPEN_STATUSES,
    SHUT_STATUSES,
    STATUS_CLOSED,
    STATUS_LISTED,
    STATUS_LOTTERY,
    STATUS_ON_SALE,
    STATUS_RESERVATION,
    STATUS_SALE_END,
    STATUS_SOLD_OUT,
    CrawlResult,
    Item,
)
from jpgate.sources import pbandai, pokecen
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
        image=kw.get("image"),
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


def test_html_entities_are_decoded():
    """`&#039;` が生のまま商品名に残り、X の投稿文に露出した。
    実体参照は種類を数え上げず標準の復号に任せる。"""
    html_doc = FIXTURE.read_text(encoding="utf-8").replace(
        "METAL BUILD", "WORLD TAMER&#039;S BOX &amp; METAL BUILD"
    )
    items, _ = pbandai.parse_listing(html_doc, "tamashiiwebshouten", "u")
    assert "WORLD TAMER'S BOX & METAL BUILD" in items[0].title
    assert "&#" not in items[0].title


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


def test_closed_lottery_card_sells_own_service_not_affiliate(tmp_path, monkeypatch):
    """終了した抽選のカードは、アフィリではなく自社の次回案内を出す。

    抽選は転送代行では原理的に代行できないので、ここを他社に流すと
    自分の堀を売ることになる。カードの描画まで含めて固定する。
    """
    from jpgate import config as cm
    from jpgate import publish as pub
    from jpgate.publish import render_site

    store = Store(tmp_path / "t.sqlite")
    store.apply(
        crawl(item("1", [ICON_LOT_SALES, ICON_OUT_OF_STOCK], title="METAL BUILD Test"))
    )
    cfg = cm.load()
    cfg.affiliate = ENABLED
    # 営業中でなければ自社の案内も出ないので、その状態にして確かめる
    # （2026-08-31 に事業を停止し、勧誘は BUSINESS_OPERATOR で束ねた）。
    monkeypatch.setattr(pub, "BUSINESS_OPERATOR", "Test Operator")
    html = render_site([], {}, Glossary({}), cfg, closed_rows=store.recently_closed())
    assert "Ask us to enter the next round" in html
    # **抽選をアフィリに流さない**のがこのテストの本題。
    # 抽選は転送代行では原理的に代行できないので、他社に流すと自分の堀を売る。
    assert "sponsored nofollow" not in html
    store.close()


def test_waiting_room_is_reported_not_bypassed():
    """順番待ちへの転送は、迂回せず読める理由にして落とす。

    店が意図的に入口を絞っている状態なので、抜ける実装を足してはいけない。
    素の urllib だと「無限リダイレクト」としか出ず、原因が読めないために
    設定を疑う方向へ誤誘導される（実際に一度そうなった）。
    """
    import pytest

    from jpgate.sources.pokecen import ListingError, _DetectWaitingRoom

    h = _DetectWaitingRoom()
    with pytest.raises(ListingError) as e:
        h.redirect_request(
            None, None, 302, "Found", {},
            "https://wr.pokemoncenter-online.com/?c=pol&e=wr20260820ec",
        )
    assert "順番待ち" in str(e.value)


def test_doctor_runs_in_forum_mode(capsys):
    """doctor はフォーラム設定でも通る。

    フォーラム用の分岐は `notify.forum: true` のときしか実行されないので、
    通常設定のテストだけでは落ちない（実際に `categories` をメソッドとして
    呼ぶ誤りが素通りし、切り替えた瞬間に doctor が落ちた）。
    """
    import argparse

    from jpgate import config as cm
    from jpgate.cli import cmd_doctor

    cfg = cm.load()
    cfg.notify_forum = True
    cfg.notify_forum_tags = {"Gunpla": "1", "ソンナジャンルナイ": "2"}
    cmd_doctor(cfg, argparse.Namespace(dry_run=True))
    out = capsys.readouterr().out
    assert "フォーラム" in out
    # 綴り違いはタグが付かないまま静かに壊れるので、doctor で名指しする。
    assert "ソンナジャンルナイ" in out


def test_forum_post_carries_thread_name_and_tags(monkeypatch):
    """フォーラムは1件1投稿。タイトルと タグID が payload に乗る。

    thread_name が無いと Discord は 400 を返す。理由が読めない失敗なので
    形をここで固定しておく。
    """
    from jpgate import notify as nm

    seen = []
    monkeypatch.setattr(nm, "_send", lambda w, p, t: seen.append(p))

    nm.post_forum("https://x", {"title": "t"}, thread_name="RG Exia", tag_ids=["42"])
    assert seen[0]["thread_name"] == "RG Exia"
    assert seen[0]["applied_tags"] == ["42"]
    assert len(seen[0]["embeds"]) == 1  # まとめ送りしない

    # タグIDが無くても投稿できる（タグが付かないだけ）。
    seen.clear()
    nm.post_forum("https://x", {"title": "t"}, thread_name="RG Exia")
    assert "applied_tags" not in seen[0]


def test_forum_thread_name_never_empty_or_too_long(monkeypatch):
    """タイトルは100字上限。**空も拒否される**ので切り詰めで空にしない。"""
    from jpgate import notify as nm

    seen = []
    monkeypatch.setattr(nm, "_send", lambda w, p, t: seen.append(p))

    nm.post_forum("https://x", {}, thread_name="あ" * 300)
    assert len(seen[0]["thread_name"]) == 100

    nm.post_forum("https://x", {}, thread_name="   ")
    assert seen[1]["thread_name"]  # 空白だけでも空にしない


def test_quote_shipping_matches_the_official_table():
    """送料は日本郵便の公式額そのもの。**丸めない**。

    ここがずれると、こちらが黙って差額を被る(客には見えない)。
    KujiRadar が同じ表を別に持っているので、料金改定のときは
    両方のテストが落ちるようになっている。
    """
    from jpgate.quote import AIRPACKET, EMS, shipping_jpy

    assert shipping_jpy(100) == (1200, AIRPACKET)
    assert shipping_jpy(1000) == (3090, AIRPACKET)
    assert shipping_jpy(2000) == (5190, AIRPACKET)
    # 刻みは切り上げ。101g は 200g の料金。
    assert shipping_jpy(101) == (1410, AIRPACKET)
    # 2kg を1gでも超えたら EMS しか無い。
    assert shipping_jpy(2001) == (9100, EMS)


def test_quote_minimum_fee_protects_cheap_items():
    """安い商品でも1点あたりの手間は消えない。20%が下限を割ったら下限を採る。

    カタログの中央値は¥3,080で、半分は¥3,000以下。**主戦場がここ**なので
    下限が効かないと、手間だけかかって取り分が消える。
    """
    from jpgate.quote import LineItem

    assert LineItem("cheap", 1320, 100).fee_jpy == 500  # 20% なら¥264
    assert LineItem("mid", 3080, 250).fee_jpy == 616


def test_quote_duty_only_above_the_us_threshold():
    """関税は申告額 US$100 超のときだけ。**手数料と送料は課税対象ではない**。

    総額に掛けると客に過大請求になる。立替は実費でしか受け取らないと
    書いてあるので、多く取ったら約束を破ることになる。
    """
    from jpgate.quote import LineItem, build_quote

    under = build_quote([LineItem("a", 14000, 500)])
    assert under.duty_jpy == 0
    # 敷居以下なら「暫定」も立たない(そもそも関税が発生しない)。
    assert under.duty_provisional is False

    over = build_quote([LineItem("a", 39600, 1000)])
    assert over.duty_jpy == 3960  # 商品代のみ×10%。手数料・送料は含めない
    assert over.duty_provisional is True

    # 実額が分かったら暫定の印が消える。
    known = build_quote([LineItem("a", 39600, 1000)], duty_rate=0.043)
    assert known.duty_provisional is False


def test_quote_net_excludes_pass_through():
    """手取りは手数料だけ。商品代・送料・関税は預かっているだけで売上ではない。

    ここを混ぜると、立替の大きい高額案件が儲かっているように見える。
    """
    from jpgate.quote import LineItem, build_quote

    q = build_quote([LineItem("a", 39600, 1000)])
    assert q.net_jpy == q.item_fees_jpy + q.shipment_fee_jpy - q.paypal_jpy
    assert q.net_jpy < q.item_fees_jpy + q.shipment_fee_jpy  # PayPal のぶん必ず減る


def test_customer_quote_has_no_japanese():
    """客に貼る文面に日本語を混ぜない。

    便種の表示名がそのまま漏れて「Shipping (国際エアパケット, 650g)」と
    出た。海外の客には読めないうえ、機械翻訳の雑な代行に見える。
    """
    from jpgate.quote import LineItem, build_quote, render_en

    for weight in (650, 3000):  # エアパケットと EMS の両方
        text = render_en(build_quote([LineItem("Item", 39600, weight)]))
        leaked = [c for c in text if "぀" <= c <= "ヿ" or "一" <= c <= "鿿"]
        assert not leaked, f"{weight}g の文面に日本語: {leaked}"


def test_solicitation_and_disclosure_move_together(monkeypatch):
    """**勧誘を出すなら特商法表記も出る。出さないなら両方出ない。**

    表示義務は「広告」に対してかかる。手数料や「代わりに買います」を
    載せた時点でそのページは広告になり、氏名の表示が要る。

    危ないのは片方だけ動かすこと:
      - 表記だけ消して勧誘を残す → 義務を果たさずに広告を出している状態
      - 勧誘だけ消して表記を残す → 事業をしていないのに本名を晒している

    2026-08-31 に事業を停止したときは前者を踏みかけた（表記を消す実装を
    先に入れ、CTA が残っていた）。以来ひとつのスイッチに束ねてある。

    住所・電話を省略する条件は「請求があれば遅滞なく提供する」と書いて
    あること。営業中の分岐でその一文が消えたら省略が成立しなくなる。
    """
    from jpgate import config as cm
    from jpgate import publish as pub

    cfg = cm.load()

    # 停止中（いまの状態）: 名前もメールも勧誘も出ない。
    page = pub.render_site([], {}, Glossary({}), cfg, closed_rows=[])
    assert "Business information" not in page
    assert "ask us to enter for you" not in page
    assert cfg.contact_url not in page

    # 営業中に戻したら、勧誘と表記が**両方**復活する。
    monkeypatch.setattr(pub, "BUSINESS_OPERATOR", "Test Operator")
    monkeypatch.setattr(pub, "BUSINESS_CONTACT", "test@example.com")
    monkeypatch.setattr(
        pub, "BUSINESS_TERMS_EN", [("What we sell", "A purchasing service.")]
    )
    page = pub.render_site([], {}, Glossary({}), cfg, closed_rows=[])
    assert "Test Operator" in page
    assert "provided without delay on request" in page
    assert "ask us to enter for you" in page


def test_fullwidth_model_codes_become_searchable():
    """型番が全角のままだと海外の検索に当たらない（`ＨＧ` では eBay で0件）。"""
    g = Glossary({})
    assert g.render("ＨＧ 1/144 ジェスタ").startswith("HG")
    assert "RE/100" in g.render("ＲＥ/100 1/100 ガンダムリントヴルム")


def test_notify_marks_only_after_send(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_RESERVE])))
    store.apply(crawl(item("1", [ICON_RESERVE]), item("2", [ICON_LOT_SALES])))
    pending = store.pending_events()
    assert len(pending) == 1
    store.mark_notified([pending[0]["id"]])
    assert store.pending_events() == []
    store.close()


# --------------------------------------------------------------------------
# 在庫が観測できないソース（ポケモンセンター）
# --------------------------------------------------------------------------
def listed(item_id="p1") -> Item:
    return Item(
        source="pokemon-center",
        shop="plush-toys/plush",
        item_id=item_id,
        title="ぬいぐるみ Pokémon fit モロバレル",
        url=f"https://www.pokemoncenter-online.com/{item_id}.html",
        price_jpy=1540,
        image=None,
        summary="",
        icons=(),
        ship_month=None,
        status_hint=STATUS_LISTED,
    )


def test_listed_is_not_on_sale():
    """在庫が観測できない商品を「売っている」と断定しない。"""
    assert listed().status == STATUS_LISTED
    assert STATUS_LISTED not in OPEN_STATUSES
    assert STATUS_LISTED not in SHUT_STATUSES
    # ただし実在する在庫なので公開ページには載せる。
    assert STATUS_LISTED in DISPLAY_STATUSES


def test_listed_first_sighting_emits_new_listing(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    seed = CrawlResult("pokemon-center", "plush-toys/plush", True, [listed("p1")])
    store.apply(seed)
    events = store.apply(
        CrawlResult("pokemon-center", "plush-toys/plush", True, [listed("p1"), listed("p2")])
    )
    assert [(e.item_id, e.kind) for e in events] == [("p2", EVENT_NEW_LISTING)]
    store.close()


def test_listed_never_produces_restock(tmp_path):
    """LISTED を OPEN に入れると、売り切れ→掲載 が偽の再販として出る。
    遷移が観測できないソースで再販を名乗ってはいけない。"""
    store = Store(tmp_path / "t.sqlite")
    store.apply(CrawlResult("pokemon-center", "plush-toys/plush", True, [listed("p1")]))
    for _ in range(3):
        events = store.apply(
            CrawlResult("pokemon-center", "plush-toys/plush", True, [listed("p1")])
        )
        assert events == []
    store.close()


def test_pokecen_rejects_non_listing_page():
    with pytest.raises(pokecen.ListingError):
        pokecen.parse_listing("<html><body>404</body></html>", "plush-toys/plush", "u")


def test_placeholder_analytics_token_is_rejected():
    """ドキュメントの `$SITE_TOKEN` をそのまま貼る取り違えは実際に起きる。
    受け入れると「解析を入れたつもり」になり、readiness も OK に化ける。"""
    from jpgate.config import _clean_cf_token

    assert _clean_cf_token("$SITE_TOKEN") == ""
    assert _clean_cf_token("") == ""
    assert _clean_cf_token("token") == ""
    good = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert _clean_cf_token(f"  {good}  ") == good


def test_source_hashtag_never_names_the_wrong_seller():
    """販売元タグは**ソースから引く**。固定文字列にしない.

    実際に `#PBandai` が固定で付いていたため、ポケモンセンターの商品にまで
    #PBandai が付いた投稿がキューに出ていた。海外コレクターは販売元で
    フォローするので、ここを間違えると最も効く層に「調べていない」と伝わる。
    知らないソースはタグ**無し**が正しい(推測で販売元を名乗らない)。
    """
    from jpgate.publish import SOURCE_HASHTAG

    assert SOURCE_HASHTAG["p-bandai"] == "#PBandai"
    assert SOURCE_HASHTAG["pokemon-center"] == "#PokemonCenter"
    assert SOURCE_HASHTAG.get("未知のソース", "") == ""
    # 同じタグを2つのソースに割り当てない(販売元の識別にならなくなる)。
    assert len(set(SOURCE_HASHTAG.values())) == len(SOURCE_HASHTAG)


# --------------------------------------------------------------------------
# X 下書きの Discord 配信（コピペ用）
# --------------------------------------------------------------------------
def test_x_draft_is_never_sent_twice_for_the_same_item(tmp_path):
    """掲載中の商品は毎時の走査で何度でも出てくる.

    既読管理が無いと同じ文面が1時間ごとに飛び、真っ先にミュートされる。
    商品ごとに一度だけであることを固定する。
    """
    from jpgate import config as cm
    from jpgate.publish import build_x_posts

    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_LOT_SALES]), item("2", [ICON_LOT_SALES])))
    cfg = cm.load()

    rows = store.open_items()
    first = build_x_posts(rows, {}, Glossary({}), cfg, exclude=store.x_sent_keys())
    assert len(first) == 2

    store.mark_x_sent([p.key for p in first])
    # 商品は掲載され続けているが、二度目は何も出ない。
    again = build_x_posts(rows, {}, Glossary({}), cfg, exclude=store.x_sent_keys())
    assert again == []
    store.close()


def test_x_draft_exclusion_happens_before_the_limit(tmp_path):
    """送信済みは**選抜の前に**落とす.

    後で落とすと、送信済みが枠を食った分だけ新しい商品が出てこなくなり、
    送るほどキューが痩せる。limit=1 で送信済みを1件持たせると露見する。
    """
    from jpgate import config as cm
    from jpgate.publish import build_x_posts

    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_LOT_SALES]), item("2", [ICON_LOT_SALES])))
    cfg = cm.load()
    rows = store.open_items()

    store.mark_x_sent([("p-bandai", "1")])
    got = build_x_posts(
        rows, {}, Glossary({}), cfg, limit=1, exclude=store.x_sent_keys()
    )
    assert [p.item_id for p in got] == ["2"]
    store.close()


def test_x_queue_file_still_shows_everything(tmp_path):
    """ファイルは「いまのキュー全体」. 送信済みで痩せさせない.

    Discord 配信とファイルは別の軸。ここを共有すると、手元のファイルを見て
    貼る従来のやり方が黙って壊れる。
    """
    from jpgate import config as cm
    from jpgate.publish import render_x_posts

    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_LOT_SALES]), item("2", [ICON_LOT_SALES])))
    cfg = cm.load()
    store.mark_x_sent([("p-bandai", "1"), ("p-bandai", "2")])

    text = render_x_posts(store.open_items(), {}, Glossary({}), cfg)
    # 本文の言い回しは商品ごとに変わるので、件数は商品URLで数える。
    assert text.count("/item/item-") == 2
    store.close()


def _row(db, item_id, status="ON_SALE", price=1000, title="Item", icons="[]"):
    return db.execute(
        "SELECT ? AS source, ? AS item_id, ? AS shop, ? AS title, ? AS url,"
        " ? AS price_jpy, ? AS image, ? AS summary, ? AS icons,"
        " ? AS ship_month, ? AS status",
        ("p-bandai", item_id, "hobby", title, f"https://x/{item_id}", price,
         None, "", icons, None, status),
    ).fetchone()


# --------------------------------------------------------------------------
# 相場: **別商品の値段を出さないこと**
# --------------------------------------------------------------------------
class _FakeEbay:
    """検索結果を差し替えるだけの偽クライアント。"""

    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def search(self, query, limit=50):
        self.queries.append(query)
        return self.hits


def test_grade_and_scale_are_not_evidence():
    """"RG" と "1/144" で同定してはいけない。

    RGのキットは出品名に必ず両方入るので、これで「2語一致」を満たすと
    **別のキットを掴んでも一致率が落ちず、検知できない**。
    実測でそうなった（RGの相場が全部$41〜$59に張り付いた）。
    """
    from jpgate.prices import anchors

    key, narrow = anchors("RG 1/144 Freedom Gundam")
    assert key == ["Freedom"]  # Gundam は汎用語なので特定語にしない
    assert set(narrow) == {"RG", "1/144"}


def test_set_code_survives_bracket_stripping():
    """【OP-14】のセット番号は**商品を1点に特定する最強の語**。

    括弧ごと落としていたため、カードゲームの相場が全部同じ幅になった。
    """
    from jpgate.prices import anchors

    key, _ = anchors("ONE PIECE Card Game Booster Pack 蒼海の七傑【OP-14】")
    assert "OP-14" in key
    # 作品名や汎用語は特定語に数えない（その作品の出品すべてに入るため）。
    assert "PIECE" not in key
    assert "Booster" not in key


def test_price_is_withheld_without_confidence():
    """確信が持てないなら**出さない**。None は失敗ではなく正常な結論。

    「日本で¥2,860 / eBayで$300」は儲け話ではなく、たいてい無関係な商品を
    掴んだ証拠。客はこの数字でこちらの信用を測るので、1件外すと全部疑われる。
    """
    from jpgate.prices import quote

    # 特定語ゼロ（グレードと縮尺しかない）→ 検索にも行かない
    client = _FakeEbay([(50.0, "RG 1/144 Something Else")] * 10)
    assert quote(client, "RG 1/144 Gundam", 1.0, 0.8) is None
    assert client.queries == []

    # 返ってきた商品名に特定語が入っていない → 別商品を掴んでいる
    client = _FakeEbay([(300.0, "Completely Unrelated Item")] * 10)
    assert quote(client, "Exia Repair Gundam", 1.0, 0.8) is None

    # 件数が足りない → 相場ではない
    client = _FakeEbay([(50.0, "Exia Repair")] * 2)
    assert quote(client, "Exia Repair Gundam", 1.0, 0.8) is None


def test_set_code_overrides_the_coverage_gate():
    """訳出率は「同定できるか」の代理指標。セット番号はその直接の証拠。

    代理指標を直接証拠より優先するのは順序が逆なので、番号があれば通す。
    """
    from jpgate.prices import quote

    hits = [(20.0 + i, f"ONE PIECE OP-14 Booster Box {i}") for i in range(10)]
    # 訳出率0.1（ほぼ日本語のまま）でも、番号があるので通る
    assert quote(_FakeEbay(hits), "Booster Pack 蒼海の七傑【OP-14】", 0.1, 0.8)
    # 番号が無ければ同じ訳出率では通さない
    assert quote(_FakeEbay(hits), "Booster Pack 蒼海の七傑", 0.1, 0.8) is None


def test_price_is_shown_as_a_range_not_a_point():
    """点で出すと「その値段で売れる」と読まれる。それは保証できない。

    同名の通常版と限定版が混ざる（実測: 小売版RGフリーダム$14 と
    プレバン限定版$104 が同じ検索に並んだ）。
    """
    from jpgate.prices import quote

    hits = [(float(p), "Exia Repair kit") for p in (10, 20, 30, 40, 200)]
    q = quote(_FakeEbay(hits), "Exia Repair Gundam", 1.0, 0.8)
    assert q is not None
    assert q.low_usd < q.median_usd < q.high_usd


def test_score_prefers_lotteries_then_price():
    """1日1件に絞ると**選抜が投稿の質そのもの**になる。順序を固定する。

    抽選を最上位に置くのは、応募が1アカウント1口で**転送業者でも
    スケールしない**唯一の関門だから。この事業の核心そのものなので、
    ここが2番手に落ちる並べ替えは間違い。
    """
    import sqlite3

    from jpgate.gates import GateVerdict
    from jpgate.publish import score_interest

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    plain = GateVerdict(keys=("G2_JP_ADDRESS",), unknown=False, evidence={})

    lottery = score_interest(_row(db, "1", "LOTTERY_OPEN", 3000), plain, "Gunpla")
    normal = score_interest(_row(db, "2", "ON_SALE", 3000), plain, "Gunpla")
    assert lottery > normal

    # 同じ条件なら高いほうが上。ただし対数なので差は頭打ちになる。
    cheap = score_interest(_row(db, "3", "ON_SALE", 1000), plain, "Gunpla")
    rich = score_interest(_row(db, "4", "ON_SALE", 100000), plain, "Gunpla")
    assert rich > cheap
    assert rich - cheap < 3.0

    # 前回と同じカテゴリは下がる（連日同じジャンルは「ガンプラbot」に見える）。
    assert score_interest(
        _row(db, "5", "ON_SALE", 3000), plain, "Gunpla", avoid_category="Gunpla"
    ) < normal


def test_x_posts_do_not_repeat_the_same_cta():
    """CTA は先頭の1本だけ。**全部に付けると宣伝botに見える**。

    以前は全件に「We're in Japan: <招待リンク>」が入り、どの投稿も
    リンク＋宣伝文句の同じ形になった。19件並べたら連投できる内容ではない、
    というのが実際の指摘。勧誘はプロフィール欄が持っている。
    """
    import sqlite3

    from jpgate import config as cm
    from jpgate.publish import build_x_posts

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    rows = [
        db.execute(
            "SELECT ? AS source, ? AS item_id, ? AS shop, ? AS title, ? AS url,"
            " ? AS price_jpy, ? AS image, ? AS summary, ? AS icons,"
            " ? AS ship_month, ? AS status",
            ("p-bandai", str(n), "hobby", f"Item {n}", f"https://x/{n}", 1000,
             None, "", '["ITEM_LOT_SALES"]', None, "LOTTERY_OPEN"),
        ).fetchone()
        for n in range(4)
    ]
    posts = build_x_posts(rows, {}, Glossary({}), cm.load(), limit=4)
    assert len(posts) >= 2
    with_cta = [p for p in posts if "We're in Japan" in p.text]
    assert len(with_cta) == 1


def test_x_draft_webhook_is_separate_from_the_drops_webhook(monkeypatch):
    """下書きは自分用。#drops に流すとメンバーに同じ商品が2回届く.

    別の環境変数を要求することで取り違えを構造的に防ぐ。未設定なら
    None のままで、呼び出し側が送信を諦められる。
    """
    from jpgate import config as cm

    monkeypatch.setenv("JPGATE_DISCORD_WEBHOOK", "https://example.invalid/drops")
    monkeypatch.delenv("JPGATE_X_QUEUE_WEBHOOK", raising=False)
    cfg = cm.load()
    assert cfg.discord_webhook == "https://example.invalid/drops"
    assert cfg.x_queue_webhook is None


def test_x_draft_too_long_for_discord_is_not_truncated():
    """切れた文面を貼られるのが最悪なので、送らずに落とす."""
    from jpgate.notify import post_text

    with pytest.raises(ValueError):
        post_text("https://example.invalid/hook", ["x" * 2100])


# --------------------------------------------------------------------------
# 堀の定義（転送業者が越えられない関門だけを売る）
# --------------------------------------------------------------------------
def test_address_only_items_are_still_sold():
    """住所しか関門が無い商品でも代行を出す.

    一度 exclusive(SMS認証等)だけに絞ったが、その線引きの根拠が持たなかった
    ので戻した。「転送業者はSMS認証を越えられない」は config に evidence を
    持たない推論で、転送業者は日本の会社なので日本の番号を持っている。
    **根拠の無い線引きで売り先を捨てない。**
    """
    from jpgate.gates import G2_JP_ADDRESS, SourceGate, evaluate

    addr_only = SourceGate(G2_JP_ADDRESS, "国内配送のみ", "2026-08-17")
    verdict = evaluate(item("1", []), [addr_only])
    assert verdict.keys == (G2_JP_ADDRESS,)
    assert verdict.sellable
    # 競合の有無は値付けの材料としてだけ残す。
    assert verdict.exclusive_keys == ()


def test_unknown_items_are_never_sold():
    """関門が確認できない商品には出さない. ここは絞ったままにする.

    越えられる関門を「越えられない」と売ることになるため。
    sellable を緩めたときに、ここまで一緒に緩めてはいけない。
    """
    from jpgate.gates import evaluate

    verdict = evaluate(item("1", []), [])
    assert verdict.unknown
    assert not verdict.sellable


def test_lottery_is_flagged_as_higher_pricing_power():
    """抽選は値付けを強く取れる側として印が付く（売る条件ではない）."""
    from jpgate.gates import G1_JP_PHONE, evaluate

    verdict = evaluate(item("1", [ICON_LOT_SALES]), [])
    assert G1_JP_PHONE in verdict.exclusive_keys
    assert verdict.sellable


def test_badges_shown_for_every_gated_item():
    """関門バッジは全商品に出る。「なぜ買えないか」がサイトの情報価値."""
    from jpgate.gates import G2_JP_ADDRESS, SourceGate, badges_en, evaluate

    verdict = evaluate(item("1", []), [SourceGate(G2_JP_ADDRESS, "国内配送のみ", "2026-08-17")])
    assert badges_en(verdict) == ["🚫 Japanese shipping address required"]


def test_payment_gate_is_sold_but_not_flagged_as_exclusive():
    """国内決済は転送業者が代理決済で解決している＝競合がいる前提で値付けする."""
    from jpgate.gates import G3_JP_PAYMENT, SourceGate, evaluate

    verdict = evaluate(item("1", []), [SourceGate(G3_JP_PAYMENT, "国内カードのみ", "2026-08-17")])
    assert verdict.sellable
    assert verdict.exclusive_keys == ()


def test_venue_pickup_items_are_not_offered():
    """会場受取は当てても送れない. 通常商品として見積もらない.

    関門としては最強(海外の人は日本の会場に行けない)が、**こちらも誰かが
    行かないと物が動かない**。移動時間と交通費が通常の見積に入っていないので
    既定では出さない。「越えられるか」と「引き受けられるか」は別問題。
    """
    from jpgate.gates import G4_IN_STORE, evaluate

    it = item("1", [ICON_LOT_SALES], title="(1)【抽選販売】LUFFY's NBA HOUSE(会場受取)")
    verdict = evaluate(it, [])
    assert G4_IN_STORE in verdict.keys
    assert verdict.needs_travel
    assert not verdict.sellable          # 代行は出さない
    assert not verdict.unknown           # が、関門は判明しているのでバッジは出る


def test_venue_phrase_matching_stays_narrow():
    """曖昧な語で現地受取を推測しない.

    ここはアイコンでも config 宣言でもない第三の根拠なので、他に読みようの
    ない文字列だけに限る。「イベント」「限定」で拾い始めると (a)(b) の規律が死ぬ。
    """
    from jpgate.gates import G4_IN_STORE, evaluate

    for title in ("イベント限定フィギュア", "会場限定カラー", "店頭販売中"):
        verdict = evaluate(item("1", [ICON_LOT_SALES], title=title), [])
        assert G4_IN_STORE not in verdict.keys, title


def test_ordinary_lottery_is_still_offered():
    """現地受取でない抽選は従来どおり売る（絞りすぎの検出）."""
    from jpgate.gates import evaluate

    verdict = evaluate(item("1", [ICON_LOT_SALES], title="S.H.Figuarts テスト"), [])
    assert not verdict.needs_travel
    assert verdict.sellable


# --------------------------------------------------------------------------
# TikTok 用の縦動画
# --------------------------------------------------------------------------
IMG = "https://example.test/a.jpg"


def _no_network(monkeypatch, data_uri="data:image/png;base64,AAAA"):
    """画像取得だけを差し替える（テストでCDNを叩かない）。"""
    from jpgate import clip

    monkeypatch.setattr(
        clip, "fetch_image", lambda url, source="", timeout=20: data_uri
    )


def test_clip_never_shows_an_item_we_cannot_sell(tmp_path, monkeypatch):
    """UNKNOWN の商品に代行のCTAを出さない規則は媒体を問わない.

    動画には「We can order it for you」が必ず入る。ゲートが確定していない商品を
    入れると、越えられる関門を「越えられない」と売ることになる。
    """
    from jpgate import clip
    from jpgate import config as cm

    _no_network(monkeypatch)
    store = Store(tmp_path / "t.sqlite")
    # 予約アイコンだけ＝アイテム由来の根拠が無い。ソース宣言も渡さない。
    store.apply(crawl(item("1", [ICON_RESERVE], image=IMG)))
    cfg = cm.load()
    assert clip.build_cards(store.open_items(), {}, Glossary({}), cfg) == []

    # 抽選アイコンがあれば根拠になる。同じ経路で今度は出る。
    store.apply(crawl(item("1", [ICON_LOT_SALES], image=IMG)))
    cards = clip.build_cards(store.open_items(), {}, Glossary({}), cfg)
    assert [c.item_id for c in cards] == ["1"]
    store.close()


def test_clip_drops_items_whose_image_cannot_be_fetched(tmp_path, monkeypatch):
    """画像が取れない商品は動画に入れない.

    HTML に CDN の URL を書いて Chromium に読ませると、失敗しても枠だけが
    描かれて**中身の無い動画が黙って完成する**。取得の失敗は「その商品が
    動画から落ちる」という形で表に出さなければならない。
    """
    from jpgate import clip
    from jpgate import config as cm

    _no_network(monkeypatch, data_uri=None)
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_LOT_SALES], image=IMG)))
    assert clip.build_cards(store.open_items(), {}, Glossary({}), cm.load()) == []
    store.close()


def test_clip_never_uses_the_same_item_twice(tmp_path, monkeypatch):
    """掲載中の商品は毎日出てくる。記録が無いと同じ5件の動画を作り続ける。"""
    from jpgate import clip
    from jpgate import config as cm

    _no_network(monkeypatch)
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_LOT_SALES], image=IMG),
                      item("2", [ICON_LOT_SALES], image=IMG)))
    cfg = cm.load()
    rows = store.open_items()

    first = clip.build_cards(rows, {}, Glossary({}), cfg, exclude=store.clip_used_keys())
    assert len(first) == 2

    store.mark_clip_used([c.key for c in first])
    assert clip.build_cards(rows, {}, Glossary({}), cfg, exclude=store.clip_used_keys()) == []
    store.close()


def test_clip_history_is_separate_from_x_drafts(tmp_path):
    """媒体ごとに別の記録.

    共有すると、X に貼った商品が動画から消える（逆も同じ）。片方の消費で
    もう片方のキューが痩せる理由が無い。
    """
    store = Store(tmp_path / "t.sqlite")
    store.mark_x_sent([("p-bandai", "1")])
    assert store.clip_used_keys() == set()
    store.mark_clip_used([("p-bandai", "2")])
    assert store.x_sent_keys() == {("p-bandai", "1")}
    store.close()


def test_clip_length_comes_from_the_input_not_from_zoompan(tmp_path):
    """`zoompan` の `d` に総コマ数を入れると**入力コマ数 × d コマ**出る.

    `-loop 1` で与えた入力にその書き方をしたところ、28秒のつもりの動画が
    150MB を超えても終わらなかった。`d=1` と入力側の `-framerate` で尺を
    決めるのが正しい。両方を固定する。
    """
    from jpgate import clip

    assert ":d=1:" in clip._zoompan(150, zoom_in=True)
    assert ":d=1:" in clip._zoompan(150, zoom_in=False)

    args = clip.compose_args(
        [tmp_path / "a.png", tmp_path / "b.png"], [2.5, 5.5], tmp_path / "o.mp4", "ffmpeg"
    )
    assert args.count("-framerate") == 2
    assert args.count(str(clip.FPS)) >= 2


def test_clip_caption_carries_no_links():
    """TikTok の説明欄でURLは押せない。押せないリンクを並べても信用を落とすだけ。"""
    from jpgate import clip
    from jpgate import config as cm

    card = clip.Card(
        source="p-bandai",
        item_id="1",
        status_label="Lottery open",
        status_cls="lot",
        title="テスト商品",
        price="¥1,000",
        gate_label="Japanese phone number",
        gate_why="",
        shop="tamashiiwebshouten",
        url="https://p-bandai.jp/item/item-1/",
        hashtags=("#TCG",),
        image_data_uri="data:image/png;base64,AAAA",
    )
    text = clip.caption([card], cm.load())
    assert "http" not in text
    assert "Japanese phone number" in text


def test_clip_never_claims_an_open_date_for_seeded_items(tmp_path):
    """初回走査で入った商品に開始日を付けない.

    `first_seen` は「最初に観測した時刻」。seed で入った数百件は全部その日の
    first_seen を持つので、そのまま出すと**同じ日に一斉に始まった**という
    嘘の動画ができる。言えないときは何も書かない。
    """
    from jpgate import clip

    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_LOT_SALES], image=IMG)))
    seed = store.seed_at()[("p-bandai", "tamashiiwebshouten")]

    assert clip._opened_on(store.open_items()[0], seed) == ""

    # seed より後に現れた商品なら、毎時走査しているので開始日と1時間以内で一致する。
    store.db.execute(
        "UPDATE items SET first_seen = ? WHERE item_id = '1'",
        ("2027-01-02T04:00:00+00:00",),
    )
    assert clip._opened_on(store.open_items()[0], seed) == "02 Jan"
    store.close()


def test_clip_puts_the_site_on_every_card(tmp_path, monkeypatch):
    """導線は最後の1枚に置かない.

    動画の目的はサイトに来てもらうこと。CTAを最後だけにすると、途中で
    離脱した人には何も残らない。
    """
    from jpgate import clip
    from jpgate import config as cm

    _no_network(monkeypatch)
    store = Store(tmp_path / "t.sqlite")
    store.apply(crawl(item("1", [ICON_LOT_SALES], image=IMG),
                      item("2", [ICON_LOT_SALES], image=IMG)))
    cfg = cm.load()
    cards = clip.build_cards(store.open_items(), {}, Glossary({}), cfg)

    html = clip.deck_html(cards, cfg, date(2026, 8, 19))
    # 商品カードの数だけ帯が出る（＋最後の1枚のCTA）。
    assert html.count("jpgate.net") >= len(cards)
    store.close()


def test_clip_bitrate_is_capped_so_it_fits_discord(tmp_path):
    """商品を増やすとファイルが伸びる。上限を尺から決めておく.

    CRF だけで作ると商品数に比例して伸び、5件で 8.0MB まで来た（Discord の
    添付上限は10MB）。送る直前に落とすこともできるが、**動画を作り終えてから
    落ちる**ので意味が薄い。長い動画ほど上限ビットレートが下がることを固定する。
    """
    from jpgate import clip
    from jpgate.notify import FILE_LIMIT

    def maxrate(n_items):
        durations = [clip.INTRO_SEC] + [clip.ITEM_SEC] * n_items + [clip.OUTRO_SEC]
        pngs = [tmp_path / f"{i}.png" for i in range(len(durations))]
        args = clip.compose_args(pngs, durations, tmp_path / "o.mp4", "ffmpeg")
        return int(args[args.index("-maxrate") + 1])

    short, long = maxrate(3), maxrate(10)
    assert long < short

    # どちらの尺でも、上限ビットレート×尺が Discord の上限を超えない。
    for n, rate in ((3, short), (10, long)):
        seconds = clip.INTRO_SEC + clip.ITEM_SEC * n + clip.OUTRO_SEC
        assert (rate + 96_000) * seconds / 8 <= FILE_LIMIT


def test_clip_caption_is_sent_as_a_copyable_block():
    """動画の説明文は TikTok に貼るための文.

    X 下書きと同じで、そのままコピーできる形でないと意味が無い。素のテキストで
    出すと Discord のUIから選択しづらく、モバイルではコピーボタンも出ない。
    """
    from jpgate.notify import code_block

    body = code_block("Japan-only drops\n#Gunpla #PBandai")
    assert body.startswith("```\n") and body.endswith("\n```")
    assert "#Gunpla #PBandai" in body

    # 商品名にバッククォートが混ざってもブロックが割れない。
    assert "```" not in code_block("壊す```商品名")[4:-4]


def test_clip_topic_tags_only_appear_on_matching_items(tmp_path, monkeypatch):
    """商品名に入っている語からしかタグを作らない.

    用語集は綴りを1つしか選べないが、タグは複数出せる（`カプセルトイ` は
    商品名では Capsule Toy、タグでは #CapsuleToy と #Gashapon の両方）。
    ただし**その語が入っていない商品には付けない**。販売元のタグを
    間違えないのと同じ規律で、間違ったタグは「調べていない」と読まれる。
    """
    from jpgate import clip
    from jpgate import config as cm

    _no_network(monkeypatch)
    store = Store(tmp_path / "t.sqlite")
    store.apply(
        crawl(
            item("1", [ICON_LOT_SALES], image=IMG, title="キーホルダー【カプセルトイ】"),
            item("2", [ICON_LOT_SALES], image=IMG, title="RG 1/144 テスト"),
        )
    )
    cards = {c.item_id: c for c in clip.build_cards(
        store.open_items(), {}, Glossary({}), cm.load()
    )}
    assert "#Gashapon" in cards["1"].hashtags
    assert "#Gashapon" not in cards["2"].hashtags
    store.close()
