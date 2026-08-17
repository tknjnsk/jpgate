"""リンク共有用の OG 画像を作る（docs/og.png）。

X・Discord・Reddit にURLを貼ったときに出るカード画像。無いとURLが
そのまま並ぶだけになり、クリック率が大きく変わる。集客の前提設備。

1200x630 は各社共通の推奨比率。ただし**Discordのモバイルでは上下が
切られる**ので、文字は中央の帯に収める。
"""

from __future__ import annotations

import base64
import pathlib

from make_icon import INK, PAPER, VERMILION, svg

W, H = 1200, 630

_HTML = """
<body style="margin:0;width:{w}px;height:{h}px;background:{ink};
             display:flex;align-items:center;gap:56px;padding:0 80px;
             box-sizing:border-box;
             font-family:'Segoe UI',system-ui,-apple-system,sans-serif">
  <img src="data:image/svg+xml;base64,{icon}"
       style="width:260px;height:260px;border-radius:28px;flex:none">
  <div style="color:{paper};min-width:0">
    <div style="font-size:76px;font-weight:800;letter-spacing:-.02em;
                line-height:1">JPGate</div>
    <div style="font-size:34px;font-weight:600;color:{verm};margin-top:14px;
                line-height:1.25">Japan-only releases,<br>tracked as they open</div>
    <div style="font-size:23px;color:#a5a096;margin-top:22px;line-height:1.5">
      Pre-orders, lotteries and restocks that an overseas<br>
      buyer physically cannot access — and why.
    </div>
  </div>
</body>
"""


def main() -> None:
    from playwright.sync_api import sync_playwright

    out = pathlib.Path(__file__).resolve().parents[1] / "docs" / "og.png"
    icon = base64.b64encode(svg(PAPER, VERMILION).encode()).decode()
    html = _HTML.format(w=W, h=H, ink=INK, paper=PAPER, verm=VERMILION, icon=icon)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H})
        page.set_content(html)
        page.wait_for_timeout(300)
        page.screenshot(path=str(out))
        browser.close()
    print(f"- {out}")


if __name__ == "__main__":
    main()
