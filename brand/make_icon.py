"""Discord サーバーアイコンの生成。

## 制約から形を決めている

Discord はアイコンを**円形に切り抜いて48pxで表示**する。つまり:

- 角に置いたものは切り落とされる → 内接円の内側に収める
- 48px で潰れる細部は存在しないのと同じ → 線は太く、要素は少なく
- 文字は読めない → 入れない
- サイドバーには他サーバーの丸が縦に並ぶ → **シルエットで見分けがつくこと**が最重要

鳥居を選んだのは、「日本」と「門(gate)」を**ひとつの輪郭で同時に言える**形が
他に無いため。JPGate という名前の直訳でもある。

色は site の accent (#b8452f) に寄せた朱色。鳥居の実際の色でもあるので、
ブランドの都合と対象の実物が一致している。
"""

from __future__ import annotations

import base64
import pathlib

SIZE = 512

# --- 鳥居の各部材（512キャンバス、中心 x=256）--------------------------------
#
# 比率が命。最初に組んだとき横長すぎて「脚付きのテーブル」に見えたので、
# 柱を伸ばして縦横比を 0.56 → 0.94 に直した。鳥居らしさは装飾ではなく
# **柱の長さ**から来る。
#
# すべての角が内接円(中心256, 半径256)の内側に収まることを確認済み。
# 最遠点は左下 (86,432) で中心から245px。Discord の円形切り抜きで欠けない。

# 笠木: 一番上の梁。両端が上に反る(反り)のが鳥居の輪郭を決める特徴なので、
#       48px でも残るように大きく反らせている。
_KASAGI = "M 86,158 Q 256,112 426,158 L 426,192 Q 256,146 86,192 Z"
# 島木: 笠木のすぐ下の直線の梁。
_SHIMAGI = (110, 194, 292, 30)  # x, y, w, h
# 額束: 島木と貫のあいだの縦材。これが無いと「開」の字に見える。
# 細く長くすることで、横木の反復に縦の線が入り鳥居として読める。
_GAKUZUKA = (242, 224, 28, 60)
# 貫: 下側の横木。上寄りに置く(下1/3ではなく上1/3)のが鳥居の比率。
_NUKI = (130, 284, 252, 30)
# 柱: わずかに外開き(転び)。垂直だと硬く見える。
_PILLAR_L = "M 156,224 L 194,224 L 190,432 L 148,432 Z"
_PILLAR_R = "M 318,224 L 356,224 L 364,432 L 322,432 Z"


def svg(fg: str, bg: str) -> str:
    x, y, w, h = _SHIMAGI
    nx, ny, nw, nh = _NUKI
    gx, gy, gw, gh = _GAKUZUKA
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}"
     viewBox="0 0 {SIZE} {SIZE}">
  <rect width="{SIZE}" height="{SIZE}" fill="{bg}"/>
  <g fill="{fg}">
    <path d="{_KASAGI}"/>
    <rect x="{x}" y="{y}" width="{w}" height="{h}"/>
    <rect x="{gx}" y="{gy}" width="{gw}" height="{gh}"/>
    <rect x="{nx}" y="{ny}" width="{nw}" height="{nh}"/>
    <path d="{_PILLAR_L}"/>
    <path d="{_PILLAR_R}"/>
  </g>
</svg>"""


#: 朱色。site の --acc (#b8452f) を少し彩度を上げたもの。
VERMILION = "#C43D2A"
INK = "#16150F"  # site の dark 背景
PAPER = "#FBFBFA"

VARIANTS = {
    # サイドバーは暗いので、塗りつぶした暖色の円が一番目に入る。
    "a_white_on_vermilion": (PAPER, VERMILION),
    # site の dark テーマと同じ配色。落ち着くが埋もれやすい。
    "b_vermilion_on_ink": (VERMILION, INK),
}


def main() -> None:
    here = pathlib.Path(__file__).parent
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": SIZE, "height": SIZE})
        for name, (fg, bg) in VARIANTS.items():
            markup = svg(fg, bg)
            (here / f"icon_{name}.svg").write_text(markup, encoding="utf-8")
            data = base64.b64encode(markup.encode()).decode()
            page.goto(f"data:image/svg+xml;base64,{data}")
            page.screenshot(path=str(here / f"icon_{name}.png"))
            print(f"- icon_{name}.png")

        # 48px のプレビュー。実寸で潰れないかを目で確認するために作る。
        # 「512pxで綺麗」は何の保証にもならない。
        # 直前に SVG を開いているとドキュメントが HTML でなく set_content が失敗する。
        page.goto("about:blank")
        page.set_viewport_size({"width": 360, "height": 120})
        cards = "".join(
            f'<div style="text-align:center">'
            f'<img src="data:image/svg+xml;base64,'
            f'{base64.b64encode(svg(fg, bg).encode()).decode()}" '
            f'style="width:48px;height:48px;border-radius:50%">'
            f'<div style="font:10px sans-serif;color:#888;margin-top:4px">{name[0]}</div>'
            f"</div>"
            for name, (fg, bg) in VARIANTS.items()
        )
        page.set_content(
            f'<body style="margin:0;background:#2b2d31;display:flex;gap:28px;'
            f'align-items:center;justify-content:center;height:120px">{cards}</body>'
        )
        page.screenshot(path=str(here / "preview_48px.png"))
        print("- preview_48px.png (実寸・円形切り抜き)")
        browser.close()


if __name__ == "__main__":
    main()
