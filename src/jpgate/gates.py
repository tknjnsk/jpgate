"""ゲート判定 ―― 「日本にいないと物理的にできないこと」の同定。

この事業の堀は「日本人だから」ではなく、**海外の客がどう頑張っても越えられない
具体的な関門**にある。関門が特定できて初めて代行に値段が付く。

したがってゲートは推測してはいけない。根拠は次の3つに限る:

  (a) アイテム由来 : 販売元が自分で出している機械可読なフラグ（抽選販売アイコン）
  (b) ソース由来   : サイト全体のポリシー。`config.yaml` に **根拠の引用と
                     確認日を添えて**宣言する。書いてなければ存在しない扱い。
  (c) 商品名の明示 : 販売元が商品名に書いている「会場受取」等。**文章の解釈では
                     なく、他に読みようのない文字列の完全一致だけ**
                     (`_IN_STORE_PHRASES`)。ここを緩めると (a)(b) の規律が死ぬ。

どの根拠も無ければ `UNKNOWN` であって「ゲート無し」ではない。
UNKNOWN の商品に代行のCTAを出してはいけない（越えられる関門を
「越えられない」と売ることになる）。

なお「関門があるか」と「こちらが引き受けられるか」は別問題。
現地受取は関門としては最強だが、**誰かが会場へ行かないと物が動かない**ので
既定では代行を出さない（`requires_travel`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ICON_LOT_SALES, Item

# --------------------------------------------------------------------------
# ゲート定義
# --------------------------------------------------------------------------
G1_JP_PHONE = "G1_JP_PHONE"
G2_JP_ADDRESS = "G2_JP_ADDRESS"
G3_JP_PAYMENT = "G3_JP_PAYMENT"
G4_IN_STORE = "G4_IN_STORE"
G5_JP_ID = "G5_JP_ID"


@dataclass(frozen=True)
class GateDef:
    key: str
    label_en: str
    label_ja: str
    #: 客がこれを越えられない理由。そのまま営業コピーになる。
    why_en: str
    #: **転送業者(Buyee/ZenMarket等)にとって越えにくい関門か。値付けの材料。**
    #:
    #: 売る/売らないの判定には使わない(`GateVerdict.sellable` の説明を参照)。
    #: 住所や決済は転送業者が1点数百円で解決しているので、そこは競合がいる前提で
    #: 値段を考える。実店舗・本人確認は代行できる相手が少ない。
    #:
    #: **G1(SMS認証)を True にしているが、根拠は弱い**: 転送業者は日本の会社なので
    #: 日本の番号を持っており、認証自体は越えられるはず。実際に効いているのは
    #: 「抽選は1アカウント1口」でスケールしないことのほうで、それはこちらにも
    #: 同じく効く。**ここを事実として扱う前に、config に evidence つきで
    #: 宣言し直すこと。** 現状は値付けの目安でしかない。
    #:
    #: 掲載商品の実測(2026-08-18, 531件): G1 が 13.2% で平均単価 ¥15,663、
    #: G2 が 86.8% で平均 ¥5,300。**件数は少ないが単価は3倍**。
    beats_forwarder: bool = False
    #: **こちらが現地へ出向かないと履行できないか。**
    #:
    #: 越えられる/越えられないの話ではなく「引き受けられるか」の話。
    #: 会場受取や店頭抽選は、当てても誰かが物理的に取りに行かないと物が動かない。
    #: 移動時間と交通費が乗るので、通常の見積では赤字になる。
    #: 出向くと決めたなら個別に見積もる話で、**既定では代行を出さない**。
    requires_travel: bool = False


GATE_DEFS: dict[str, GateDef] = {
    G1_JP_PHONE: GateDef(
        G1_JP_PHONE,
        "Japanese phone number",
        "日本の携帯電話番号",
        "Entry requires SMS verification on a Japanese mobile number. "
        "Overseas numbers are rejected at signup.",
        beats_forwarder=True,
    ),
    G2_JP_ADDRESS: GateDef(
        G2_JP_ADDRESS,
        "Japanese shipping address",
        "日本国内の配送先",
        "The store ships to domestic addresses only. No international option exists at checkout.",
        # 転送業者が解決済みの領域。ここに代行を出すと価格で必ず負ける。
        beats_forwarder=False,
    ),
    G3_JP_PAYMENT: GateDef(
        G3_JP_PAYMENT,
        "Japanese payment method",
        "国内発行の決済手段",
        "Checkout accepts Japanese-issued cards, konbini or carrier billing only.",
        # 同上。転送業者は代理決済を標準で提供している。
        beats_forwarder=False,
    ),
    G4_IN_STORE: GateDef(
        G4_IN_STORE,
        "In-person at a Japanese store",
        "実店舗での抽選・受取",
        "Allocation happens in a physical store in Japan. There is no online path at all.",
        beats_forwarder=True,
        requires_travel=True,
    ),
    G5_JP_ID: GateDef(
        G5_JP_ID,
        "Japanese ID document",
        "日本の本人確認書類",
        "The account requires identity verification against a Japanese-issued document.",
        beats_forwarder=True,
    ),
}

#: 判断材料が無い状態。「ゲート無し」と混ぜないための明示的な値。
UNKNOWN = "UNKNOWN"

#: 商品名に出てくる「現地で受け取る」の明示表現。
#:
#: これはアイコンでも config 宣言でもない**第三の根拠**なので、範囲を極端に
#: 狭く保つ。曖昧な語(「限定」「イベント」等)は入れない ――
#: 増やすときは、その文字列が「現地受取」以外に読めないことを確認すること。
_IN_STORE_PHRASES = ("会場受取", "会場受け取り", "店頭受取", "店頭受け取り", "店頭引換")


@dataclass(frozen=True)
class SourceGate:
    """`config.yaml` で宣言するソース単位のゲート。

    `evidence` と `verified_on` は必須。根拠を書けないものは宣言できない、
    というのがこのクラスの存在理由。
    """

    key: str
    evidence: str
    verified_on: str

    @staticmethod
    def from_config(raw: dict) -> "SourceGate":
        for required in ("key", "evidence", "verified_on"):
            if not raw.get(required):
                raise ValueError(
                    f"source gate の {required} が空です。"
                    f"根拠と確認日の無いゲートは宣言できません: {raw!r}"
                )
        key = raw["key"]
        if key not in GATE_DEFS:
            raise ValueError(f"未知のゲート {key!r}。GATE_DEFS に無いものは使えません")
        return SourceGate(key, raw["evidence"], str(raw["verified_on"]))


@dataclass(frozen=True)
class GateVerdict:
    """1商品のゲート判定。"""

    #: 確定したゲートのキー。空なら判断材料が無い。
    keys: tuple[str, ...]
    #: keys が空のとき True。`not keys` と同義だが、呼び出し側で
    #: 「ゲート無し」と読み違えないように名前を与えている。
    unknown: bool
    #: 各ゲートをどこから得たか（キー -> 根拠文字列）。
    evidence: dict[str, str]

    @property
    def exclusive_keys(self) -> tuple[str, ...]:
        """転送業者が越えられない関門だけ。ここが自社の堀。"""
        return tuple(k for k in self.keys if GATE_DEFS[k].beats_forwarder)

    @property
    def needs_travel(self) -> bool:
        """履行に現地への移動が要るか（会場受取・店頭抽選）。"""
        return any(GATE_DEFS[k].requires_travel for k in self.keys)

    @property
    def sellable(self) -> bool:
        """代行のCTAを出してよいか。UNKNOWN では出さない。

        **関門の種類では絞らない。** 一度 exclusive_keys だけに絞ったが、
        その線引きの根拠が持たなかったので戻した:
        「転送業者はSMS認証を越えられない」は config に evidence を持たない
        推論で、しかも転送業者は日本の会社なので日本の番号を持っている。
        実際に効いているのは「抽選は1アカウント1口」という制約のほうで、
        それはこちらにも同じく効く。根拠の無い線引きで売り先を捨てない。

        関門の種類は値付けの材料として `exclusive_keys` で参照する
        (需要と希少性で手数料を変える)。売る/売らないの判定には使わない。

        **例外は現地受取**。これは「越えられるか」ではなく「引き受けられるか」の
        問題で、誰かが会場や店舗に行かないと物が動かない。移動時間と交通費が
        通常の見積に入っていないので、既定では出さない。出向くと決めたなら
        個別見積の話になる。
        """
        return bool(self.keys) and not self.needs_travel


def evaluate(item: Item, source_gates: list[SourceGate]) -> GateVerdict:
    """アイテムとソース宣言からゲートを決める。

    アイテム由来の根拠は現状ひとつだけ ―― 抽選販売アイコン。
    プレミアムバンダイの抽選は会員登録が前提で、会員登録には日本の携帯番号での
    SMS認証と国内決済が要る。したがって抽選販売アイコンは G1 と G3 の証拠になる。
    ここを増やすときは必ず実データで観測できるフラグに紐づけること。
    """
    evidence: dict[str, str] = {}

    for sg in source_gates:
        evidence[sg.key] = f"{sg.evidence}（確認 {sg.verified_on}）"

    # 販売元が商品名に明示している現地受取。**推測はしない**: ここに置くのは
    # 「会場で受け取る」以外に読みようがない文字列だけ。NFKC 正規化済みの
    # タイトルに対して照合する(全角括弧が半角になっている)。
    # 実測(2026-08-18, 531件)では該当1件。少ないが、当てても送れない商品を
    # 通常商品として見積もると1件目で信用を落とす。
    for phrase in _IN_STORE_PHRASES:
        if phrase in item.title:
            evidence[G4_IN_STORE] = f"商品名に「{phrase}」。現地で受け取る以外の経路が無い。"
            break

    if ICON_LOT_SALES in item.icons:
        note = "販売ページに「抽選販売」アイコン。応募には会員登録が必須。"
        # ソース宣言のほうが具体的なら上書きしない（確認日が入っているため）。
        evidence.setdefault(G1_JP_PHONE, note)
        evidence.setdefault(G3_JP_PAYMENT, note)

    keys = tuple(sorted(evidence))
    return GateVerdict(keys=keys, unknown=not keys, evidence=evidence)


def badges_en(verdict: GateVerdict) -> list[str]:
    """通知・Webページに出すバッジ文字列。"""
    if verdict.unknown:
        return ["⚠️ Access requirements unverified"]
    return [f"🚫 {GATE_DEFS[k].label_en} required" for k in verdict.keys]
