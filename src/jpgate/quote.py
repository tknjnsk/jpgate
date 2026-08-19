"""見積の計算。**客に出す数字を手で足さないための場所**。

見積を間違えると、こちらが黙って赤字を被るか、客に訂正を送ることになる。
訂正は「言い値が動く代行業者」に見えるので、実績ゼロの段階では致命的。
だから計算はここに一本化して、テストで固定する。

## 料金の出どころ

料率(発送¥2,500 + 商品ごと20%・最低¥500)は**3箇所に同じ文面がある**:
サイトのフッタ(`publish.BUSINESS_TERMS_EN`)、Discord の #proxy-service、
SOCIAL_COPY.md。ここを変えるなら3箇所とも変えること。食い違うと、
どれが本当の値段か客に判断できない。

## 送料

日本郵便・第4地帯(米国)の公式料金。2026-08-18 に公式表で確認した。
KujiRadar が同じ表を持っている(あちらは仕入れ判定用)。**別々に持っている
ので、片方だけ古くなる余地がある**。テストで実額を固定してあるので、
料金改定のときは両方のテストが落ちる。

容積重量は日本郵便の国際郵便には無い。効くのはサイズ上限のほうで、
**ここではモデル化していない**(エアパケットは長さ60cm・3辺合計90cm)。
大きい箱は計算が通っても送れないことがある。

## 関税

**暫定10%。実測ではない。** 米国は2025-08-29に de minimis を廃止し、
US$100超〜US$2,500以下は事前納付(DDP)が要る。実際の税率は品目の
HTSコードで決まるので、10%は「見積が下振れしないための仮置き」。
1件目を通したら実額に差し替えること。それまでは客にも暫定と伝える。
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# 料率。変えるときは上の docstring にある3箇所も同時に直す。
# --------------------------------------------------------------------------
#: 1発送あたりの固定手数料。梱包・税関書類・郵便局への往復の対価。
#: **商品数で変わらない**ので、まとめて注文するほど客の1点あたりは安くなる。
SHIPMENT_FEE_JPY = 2500
#: 商品ごとの手数料率。
ITEM_FEE_RATE = 0.20
#: 商品ごとの手数料の下限。安い商品でも1点あたりの手間は消えないため。
ITEM_FEE_MIN_JPY = 500

#: 1便の上限重量。これを超えると EMS になる(エアパケットは2kgまで)。
AIRPACKET_MAX_G = 2000

#: 米国の事前納付が要る下限(申告額)。これ以下なら関税の立替は発生しない。
DUTY_THRESHOLD_USD = 100
#: 暫定関税率。**実測ではない**(docstring 参照)。
PROVISIONAL_DUTY_RATE = 0.10
#: 見積時の換算レート。実際の請求は JPY 建てなので、これは
#: 「US$100 の敷居を越えるか」の判定にしか使わない。
USD_JPY_RATE = 150

#: PayPal の海外取引手数料。**JPY建てで請求する前提**。
#: 外貨受取にすると通貨換算で更に4.00%取られ、手取りが半分以下になる。
PAYPAL_RATE = 0.0410
PAYPAL_FIXED_JPY = 40


def _linear_table(base_jpy: int, step_jpy: int, top_g: int) -> dict[int, int]:
    return {g: base_jpy + step_jpy * (g // 100 - 1) for g in range(100, top_g + 1, 100)}


#: 国際エアパケット(追跡あり・2kgまで)。100g 1,200円 + 210円/100g。
AIRPACKET_JPY: dict[int, int] = _linear_table(1200, 210, AIRPACKET_MAX_G)

#: EMS(2kg超。上限30kg)。追跡あり。
EMS_JPY: dict[int, int] = {
    2500: 9100, 3000: 10300, 3500: 11500, 4000: 12700, 4500: 13900,
    5000: 15100, 5500: 16300, 6000: 17500, 7000: 19900, 8000: 22300,
    9000: 24700, 10000: 27100, 15000: 39100, 20000: 51100,
    25000: 63100, 30000: 75100,
}
#: 2kg ちょうどは EMS の刻みにもあるので両方に置く(エアパケットのほうが安い)。
EMS_JPY[2000] = 7900


class QuoteError(ValueError):
    """見積を出せない。**黙って丸めずに断る**ための例外。"""


@dataclass(frozen=True)
class LineItem:
    """見積1行。`weight_g` は梱包後の推定重量。"""

    name: str
    price_jpy: int
    weight_g: int

    @property
    def fee_jpy(self) -> int:
        """この商品ぶんの手数料。20% と最低額の大きいほう。"""
        return max(round(self.price_jpy * ITEM_FEE_RATE), ITEM_FEE_MIN_JPY)


@dataclass(frozen=True)
class Quote:
    items: tuple[LineItem, ...]
    goods_jpy: int
    item_fees_jpy: int
    shipment_fee_jpy: int
    shipping_jpy: int
    duty_jpy: int
    #: 便種キー(`AIRPACKET` / `EMS`)。表示名は `METHOD_JA` / `METHOD_EN` で引く。
    method: str
    weight_g: int
    #: 関税が暫定値かどうか。**True のあいだは客に確定額として出さない**。
    duty_provisional: bool

    @property
    def total_jpy(self) -> int:
        return (
            self.goods_jpy
            + self.item_fees_jpy
            + self.shipment_fee_jpy
            + self.shipping_jpy
            + self.duty_jpy
        )

    @property
    def paypal_jpy(self) -> int:
        """PayPal が総額から取る額。**立替分にも掛かる**ので手数料から引かれる。"""
        return round(self.total_jpy * PAYPAL_RATE) + PAYPAL_FIXED_JPY

    @property
    def net_jpy(self) -> int:
        """こちらの手取り。手数料から PayPal を引いた残り。

        商品代・送料・関税は立替なので売上ではない。ここがマイナスなら
        受けるだけ損をする。
        """
        return self.item_fees_jpy + self.shipment_fee_jpy - self.paypal_jpy


#: 便種。**表示は言語ごとに引き分ける**。客に貼る文面へ日本語が混ざると、
#: 「日本人が機械翻訳で書いた雑な代行」に見える(一度混ざった)。
AIRPACKET = "airpacket"
EMS = "ems"
METHOD_JA = {AIRPACKET: "国際エアパケット", EMS: "EMS"}
METHOD_EN = {AIRPACKET: "Japan Post Air Parcel, tracked", EMS: "EMS, tracked"}


def shipping_jpy(weight_g: int) -> tuple[int, str]:
    """重量 → (送料, 便種キー)。刻みは切り上げ。

    追跡なし(小形包装物)は選ばない。安いが、届いたことを互いに証明できない。
    """
    if weight_g <= 0:
        raise QuoteError("重量が 0 以下です。梱包後の重量を指定してください。")
    if weight_g <= AIRPACKET_MAX_G:
        for g in sorted(AIRPACKET_JPY):
            if weight_g <= g:
                return AIRPACKET_JPY[g], AIRPACKET
    for g in sorted(EMS_JPY):
        if weight_g <= g:
            return EMS_JPY[g], EMS
    raise QuoteError(
        f"{weight_g}g は EMS の上限30kgを超えます。分割してください。"
    )


def build_quote(items: list[LineItem], *, duty_rate: float | None = None) -> Quote:
    """見積を組み立てる。

    `duty_rate` を明示したときだけ関税を確定扱いにする。省略時は暫定10%で
    計算し、`duty_provisional=True` を立てる。**暫定のまま客に確定額として
    出さないための印**で、表示側はこれを見て注記を出す。
    """
    if not items:
        raise QuoteError("商品が1つもありません。")

    weight_g = sum(i.weight_g for i in items)
    ship, method = shipping_jpy(weight_g)
    goods = sum(i.price_jpy for i in items)
    fees = sum(i.fee_jpy for i in items)

    # 関税は**商品代にのみ**かかる。手数料や送料は課税対象ではない。
    provisional = duty_rate is None
    rate = PROVISIONAL_DUTY_RATE if provisional else duty_rate
    duty = 0
    if goods > DUTY_THRESHOLD_USD * USD_JPY_RATE:
        duty = round(goods * rate)
    else:
        # 敷居以下なら事前納付そのものが無い。暫定かどうかも問題にならない。
        provisional = False

    return Quote(
        items=tuple(items),
        goods_jpy=goods,
        item_fees_jpy=fees,
        shipment_fee_jpy=SHIPMENT_FEE_JPY,
        shipping_jpy=ship,
        duty_jpy=duty,
        method=method,
        weight_g=weight_g,
        duty_provisional=provisional,
    )


def render_ja(q: Quote) -> str:
    """自分用の内訳。**手取りまで出す**ので客には見せない。"""
    lines = ["見積（内部用）", ""]
    for i in q.items:
        lines.append(f"  {i.name}  ¥{i.price_jpy:,} ({i.weight_g}g) → 手数料 ¥{i.fee_jpy:,}")
    lines += [
        "",
        f"  商品代            ¥{q.goods_jpy:,}",
        f"  商品ごと手数料     ¥{q.item_fees_jpy:,}",
        f"  発送手数料         ¥{q.shipment_fee_jpy:,}",
        f"  送料 ({METHOD_JA[q.method]} {q.weight_g}g)  ¥{q.shipping_jpy:,}",
    ]
    if q.duty_jpy:
        mark = "  ※暫定10%" if q.duty_provisional else ""
        lines.append(f"  関税              ¥{q.duty_jpy:,}{mark}")
    else:
        lines.append("  関税              なし（申告額 US$100 以下）")
    lines += [
        f"  ── 客の総支払      ¥{q.total_jpy:,}",
        "",
        f"  PayPal が取る      ¥{q.paypal_jpy:,}",
        f"  手取り            ¥{q.net_jpy:,}",
    ]
    if q.net_jpy <= 0:
        lines.append("  ⚠ 手取りがマイナスです。受けると損をします。")
    if q.duty_provisional:
        lines.append("  ⚠ 関税は暫定。客には確定額として出さないこと。")
    return "\n".join(lines)


def render_en(q: Quote) -> str:
    """客に貼る見積。**手取りは出さない**。立替は原価だと分かる形で並べる。"""
    lines = ["**Your quote**", ""]
    for i in q.items:
        lines.append(f"· {i.name} — ¥{i.price_jpy:,}")
    lines += [
        "",
        f"Items                    ¥{q.goods_jpy:,}",
        f"Our fee                  ¥{q.item_fees_jpy + q.shipment_fee_jpy:,}"
        f"  (¥{q.shipment_fee_jpy:,} per shipment + 20% per item)",
        f"Shipping ({METHOD_EN[q.method]}, {q.weight_g}g)  ¥{q.shipping_jpy:,}"
        "  — Japan Post cost, no markup",
    ]
    if q.duty_jpy:
        note = " (estimated — we bill the exact amount)" if q.duty_provisional else ""
        lines.append(f"US customs duty          ¥{q.duty_jpy:,}  — at cost{note}")
    lines += [
        f"**Total                  ¥{q.total_jpy:,}**",
        "",
        "Invoiced in Japanese yen via PayPal Goods & Services, after we've",
        "secured the item — never before.",
    ]
    if q.duty_provisional:
        lines.append("")
        lines.append(
            "The duty figure is an estimate until the parcel is declared. "
            "You pay what US Customs charges, at cost — we never mark it up."
        )
    return "\n".join(lines)
