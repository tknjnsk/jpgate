"""JPGate のデータ構造。

設計上の約束:
- `Item` は**一覧ページから観測できたものだけ**を持つ。商品詳細ページは
  ボット判定でHTMLが返らないため（README「詳細ページは取れない」参照）、
  ここに詳細ページ由来のフィールドを足してはいけない。
- 状態(`status`)はアイコンから導出する。文章からの推測は入れない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# アイコン（プレミアムバンダイが自分で出している機械可読なフラグ）
#
# 実データで観測した語彙のみを載せている（2026-08-17、tamashiiwebshouten /
# hobby / carddas の一覧計20ページ）。未知のアイコンが来たら握り潰さずに
# `UNKNOWN_ICONS` として記録し、doctor で報告する。推測でマップを増やさない。
# --------------------------------------------------------------------------
ICON_LOT_SALES = "ITEM_LOT_SALES"  # 抽選販売
ICON_RESERVE = "ITEM_RESERVE"  # 予約
ICON_RESERVE_END = "ITEM_RESERVE_END"  # 予約終了
ICON_SALE_END = "ITEM_SALE_END"  # 販売終了
ICON_OUT_OF_STOCK = "ITEM_OUT_OF_STOCK"  # 在庫無し
ICON_IN_STOCK = "ITEM_IN_STOCK"  # 在庫商品
ICON_ORDER_PRODUCTION = "ITEM_ORDER_PRODUCTION"  # 受注生産商品
ICON_DEADLINE = "ITEM_DEADLINE"  # 締切間近
ICON_DELIVERY_EACH = "DELIVERY_EACH"  # 個別配送
ICON_SDGS = "ITEM_SDGS"  # SDGs

KNOWN_ICONS = frozenset(
    {
        ICON_LOT_SALES,
        ICON_RESERVE,
        ICON_RESERVE_END,
        ICON_SALE_END,
        ICON_OUT_OF_STOCK,
        ICON_IN_STOCK,
        ICON_ORDER_PRODUCTION,
        ICON_DEADLINE,
        ICON_DELIVERY_EACH,
        ICON_SDGS,
    }
)

# --------------------------------------------------------------------------
# 状態。アイコンからの導出のみ。
# --------------------------------------------------------------------------
STATUS_LOTTERY = "LOTTERY_OPEN"  # 抽選受付中
STATUS_RESERVATION = "RESERVATION_OPEN"  # 予約受付中
STATUS_ON_SALE = "ON_SALE"  # 状態アイコンが無い＝通常販売中
STATUS_CLOSED = "CLOSED"  # 予約終了
STATUS_SALE_END = "SALE_END"  # 販売終了
STATUS_SOLD_OUT = "SOLD_OUT"  # 在庫無し
#: 掲載は確認できたが、在庫状態が**観測できない**ソース向け。
#: ポケモンセンターオンラインは一覧に在庫タグが一切出ず、在庫フラグは
#: 商品ページにしか無い（数千点を毎時叩けない）。
#: 「売っている」と断定できないので ON_SALE と混ぜない。
#: この状態の商品には再販通知を出さない（そもそも遷移が観測できない）。
STATUS_LISTED = "LISTED"

#: 「今なら買える／申し込める」状態。再販判定はこの集合への遷移で見る。
#: STATUS_LISTED は**入れない**。買えると断定できないものを
#: 「再び買えるようになった」の到達先にすると嘘の再販通知が出る。
OPEN_STATUSES = frozenset({STATUS_LOTTERY, STATUS_RESERVATION, STATUS_ON_SALE})
#: 「もう買えない」状態。
SHUT_STATUSES = frozenset({STATUS_CLOSED, STATUS_SALE_END, STATUS_SOLD_OUT})
#: 公開ページに載せる状態。LISTED は在庫が不明なだけで実在する在庫なので載せる。
DISPLAY_STATUSES = OPEN_STATUSES | {STATUS_LISTED}


@dataclass(frozen=True)
class Item:
    """一覧ページ1カードぶんの観測。"""

    source: str  # "p-bandai"
    shop: str  # "tamashiiwebshouten"
    item_id: str  # "1000255333"
    title: str
    url: str
    price_jpy: int | None  # 価格が非表示のカードがあるので None を許す
    image: str | None
    summary: str
    icons: tuple[str, ...]  # 正規化済みアイコンキー
    ship_month: str | None  # "2026-11"（RESERVE_YYYYMM 由来）
    #: 状態をアイコンから導けないソースが明示的に入れる。
    #: アイコン方式のソース（p-bandai）は None のままにすること。
    status_hint: str | None = None

    @property
    def status(self) -> str:
        """アイコンから状態を決める。

        優先順位に意味がある。「終了/在庫無し」を先に見るのは、抽選販売の
        アイコンが**終了後も残ることがある**ため。先に LOT_SALES を見ると
        終わった抽選を受付中として通知してしまう。

        実測（2026-08-17、終了状態135件）では保持は1件だけで、通常は
        終了時に抽選アイコンが外れて予約終了アイコンに置き換わる。
        頻度は低いが、起きたときの結果が誤通知なのでガードは外さない。
        """
        if self.status_hint is not None:
            return self.status_hint
        if ICON_RESERVE_END in self.icons:
            return STATUS_CLOSED
        if ICON_SALE_END in self.icons:
            return STATUS_SALE_END
        if ICON_OUT_OF_STOCK in self.icons:
            return STATUS_SOLD_OUT
        if ICON_LOT_SALES in self.icons:
            return STATUS_LOTTERY
        if ICON_RESERVE in self.icons:
            return STATUS_RESERVATION
        return STATUS_ON_SALE

    @property
    def deadline_soon(self) -> bool:
        return ICON_DEADLINE in self.icons


@dataclass
class CrawlResult:
    """1ショップぶんの走査結果。

    `ok=False` のとき items は**信用してはいけない**。呼び出し側は差分を
    取らずに捨てること（空を「全部消えた」と読むと誤通知になる）。
    """

    source: str
    shop: str
    ok: bool
    items: list[Item] = field(default_factory=list)
    pages_fetched: int = 0
    pages_failed: int = 0
    unknown_icons: set[str] = field(default_factory=set)
    error: str | None = None


@dataclass(frozen=True)
class Event:
    """状態遷移。通知とWeb掲載の単位。"""

    source: str
    shop: str
    item_id: str
    kind: str
    from_status: str | None
    to_status: str


EVENT_LOTTERY_OPEN = "LOTTERY_OPEN"  # 抽選受付が始まった
EVENT_RESERVATION_OPEN = "RESERVATION_OPEN"  # 予約が始まった
EVENT_RESTOCK = "RESTOCK"  # 終了/在庫無し → 再び買える
EVENT_DEADLINE = "DEADLINE"  # 締切間近が付いた
EVENT_NEW_LISTING = "NEW_LISTING"  # 在庫状態は不明だが、掲載が新しく現れた

#: 通知する種別。CLOSED への遷移は通知しない（客に価値が無いうえ、
#: 走査漏れと区別できないため）。
NOTIFIABLE = (
    EVENT_LOTTERY_OPEN,
    EVENT_RESERVATION_OPEN,
    EVENT_RESTOCK,
    EVENT_DEADLINE,
    EVENT_NEW_LISTING,
)
