"""公開Webページと X 投稿文の生成。

ページは DB からの**表示**であって保存先ではない。毎回まるごと作り直す。
壊れても `jpgate publish` で再生成できる。

X は API が有料なので自動投稿しない。投稿文を `site/x_queue.txt` に吐いて
手で貼る。「自動化できていない」ことを隠さずキューとして持つほうが、
投稿が止まったときに気づける。
"""

from __future__ import annotations

import html
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .affiliate import DISCLOSURE_EN, links_for
from .config import Config
from .gates import GATE_DEFS, GateVerdict, SourceGate, evaluate
from .lines import Classifier
from .models import (
    ICON_LOT_SALES,
    STATUS_CLOSED,
    STATUS_LOTTERY,
    STATUS_RESERVATION,
    STATUS_SALE_END,
    STATUS_SOLD_OUT,
    Item,
)
from .translate import Glossary

_STATUS_LABEL = {
    STATUS_LOTTERY: ("Lottery open", "lot"),
    STATUS_RESERVATION: ("Pre-order open", "pre"),
    STATUS_CLOSED: ("Closed", "shut"),
    STATUS_SALE_END: ("Ended", "shut"),
    STATUS_SOLD_OUT: ("Sold out", "shut"),
}


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        source=row["source"],
        shop=row["shop"],
        item_id=row["item_id"],
        title=row["title"],
        url=row["url"],
        price_jpy=row["price_jpy"],
        image=row["image"],
        summary=row["summary"] or "",
        icons=tuple(json.loads(row["icons"])),
        ship_month=row["ship_month"],
    )


def render_site(
    rows: list[sqlite3.Row],
    gates_by_source: dict[str, list[SourceGate]],
    glossary: Glossary,
    cfg: Config,
    closed_rows: list[sqlite3.Row] | None = None,
    classifier: Classifier | None = None,
) -> str:
    classifier = classifier or Classifier({})
    by_month: dict[str, list[tuple[sqlite3.Row, GateVerdict]]] = defaultdict(list)
    gated = 0
    cat_counts: Counter[str] = Counter()
    for row in rows:
        item = _row_to_item(row)
        verdict = evaluate(item, gates_by_source.get(row["source"], []))
        if verdict.sellable:
            gated += 1
        cat_counts[classifier.classify(row["title"]).category] += 1
        by_month[row["ship_month"] or "TBA"].append((row, verdict))

    months = sorted(by_month, key=lambda m: (m == "TBA", m))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parts: list[str] = [
        f"<title>{html.escape(cfg.brand_name)} — Japan-exclusive release tracker</title>",
        _head_meta(cfg, len(rows), gated),
        _analytics(cfg),
        _CSS,
        "<header>",
        f"<h1>{html.escape(cfg.brand_name)}</h1>",
        "<p class=\"lede\">Japan-only figure and hobby releases, tracked as they open. "
        "Every entry says <strong>which barrier</strong> stops an overseas buyer — "
        "a Japanese phone number, a domestic address, a local payment method.</p>",
        f"<p class=\"meta\">{len(rows)} open items · {gated} with a verified Japan-only "
        f"barrier · updated {generated}</p>",
        f"<p><a class=\"cta\" href=\"{html.escape(cfg.contact_url)}\">"
        "Get alerts &amp; ask us to enter for you</a></p>",
        _filter_bar(cat_counts, gated),
        "</header>",
        "<main>",
    ]

    for month in months:
        entries = by_month[month]
        label = "Ship date TBA" if month == "TBA" else _month_label(month)
        parts.append(
            f'<section><h2>{html.escape(label)} <span>{len(entries)}</span></h2>'
        )
        parts.append('<ul class="grid">')
        for row, verdict in entries:
            parts.append(_card(row, verdict, glossary, classifier=classifier))
        parts.append("</ul></section>")

    if closed_rows:
        parts.append(
            _closed_section(closed_rows, gates_by_source, glossary, cfg, classifier)
        )

    parts.append(_FILTER_JS)

    parts.append("</main>")

    footer = [
        "<footer>",
        "<p>Titles are rendered from a hand-built glossary, not machine translation; "
        "untranslated parts are left in Japanese on purpose so they stay searchable. "
        "Items marked <em>unverified</em> are ones we have not yet confirmed are "
        "impossible to order from abroad — we do not sell a proxy for those.</p>",
    ]
    if cfg.affiliate.any_enabled:
        footer.append(f'<p class="disclosure">{html.escape(DISCLOSURE_EN)}</p>')
    footer.append("</footer>")
    parts.extend(footer)
    return "\n".join(parts)


def _filter_bar(cat_counts: "Counter[str]", gated: int) -> str:
    """カテゴリの絞り込みチップ。

    発送月の見出しは「買うと決めたあと」の情報で、探している段階の人には
    役に立たない。S.H.Figuarts を集めている人はラインで探す。
    見出しは月のまま残し、**絞り込みの軸をラインにする**のが両立点。

    JavaScript が動かなくても全件表示のままなので、壊れても情報は失われない。
    """
    chips = [
        '<button class="chip on" data-cat="*">All '
        f'<span>{sum(cat_counts.values())}</span></button>'
    ]
    for cat, n in cat_counts.most_common():
        chips.append(
            f'<button class="chip" data-cat="{html.escape(cat)}">'
            f"{html.escape(cat)} <span>{n}</span></button>"
        )
    # 抽選だけを見たい層が確実にいる。ここが代行の対象そのもの。
    chips.append(
        f'<button class="chip gate" data-gate="1">🚫 Japan-only only <span>{gated}</span></button>'
    )
    return '<div class="filters">' + "".join(chips) + "</div>"


def _closed_section(
    rows: list[sqlite3.Row],
    gates_by_source: dict[str, list[SourceGate]],
    glossary: Glossary,
    cfg: Config,
    classifier: Classifier | None = None,
) -> str:
    """終了した商品。海外の客に残る経路は二次流通だけなので、そこへ繋ぐ。

    「もう買えない」という事実そのものが、この関門の存在を一番よく説明する。
    抽選だったものには代行のCTAを出す（次回がある）。
    """
    out = [
        '<section class="closed"><h2>Recently closed '
        f"<span>{len(rows)}</span></h2>",
        '<p class="note">These are gone from the Japanese store. For an overseas '
        "buyer the only route left is the secondary market.</p>",
        '<ul class="grid">',
    ]
    for row in rows:
        item = _row_to_item(row)
        verdict = evaluate(item, gates_by_source.get(row["source"], []))
        lottery = ICON_LOT_SALES in item.icons
        links = links_for(
            status=row["status"],
            title_en=glossary.render(row["title"]),
            gate_sellable=verdict.sellable,
            lottery=lottery,
            cfg=cfg.affiliate,
        )
        out.append(
            _card(
                row, verdict, glossary, links=links, lottery=lottery, cfg=cfg,
                classifier=classifier,
            )
        )
    out.append("</ul></section>")
    return "\n".join(out)


def _head_meta(cfg: Config, open_count: int, gated: int) -> str:
    """OGP と検索用のメタタグ。

    これが無いと X / Discord / Reddit にURLを貼っても素のリンクになり、
    プレビューが出ない。共有がクリックに繋がらないので、集客の前提設備。
    og:image は**絶対URL**でないと無視される（相対パスは動かない）。
    """
    base = cfg.site_url.rstrip("/")
    desc = (
        f"{open_count} Japan-only figure and hobby releases open right now, "
        f"{gated} of them impossible to order from abroad. "
        "Pre-orders, lotteries and restocks tracked as they open — "
        "each one says which barrier stops an overseas buyer."
    )
    title = f"{cfg.brand_name} — Japan-exclusive release tracker"
    tags = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta name="description" content="{html.escape(desc)}">',
        f'<link rel="canonical" href="{base}/">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{html.escape(cfg.brand_name)}">',
        f'<meta property="og:title" content="{html.escape(title)}">',
        f'<meta property="og:description" content="{html.escape(desc)}">',
        f'<meta property="og:url" content="{base}/">',
        f'<meta property="og:image" content="{base}/og.png">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{html.escape(title)}">',
        f'<meta name="twitter:description" content="{html.escape(desc)}">',
        f'<meta name="twitter:image" content="{base}/og.png">',
    ]
    return "\n".join(tags)


def _analytics(cfg: Config) -> str:
    """アクセス解析のタグ。

    未設定なら**何も出さない**。入っていないのに入っているつもりになるのが
    一番まずいので、`readiness` はこのタグの実在を見て判定している。

    Cookie を使わない選択肢に限っている（同意バナーが要らない）。
    """
    if cfg.analytics_cf_token:
        return (
            '<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
            f"data-cf-beacon='{{\"token\": \"{cfg.analytics_cf_token}\"}}'></script>"
        )
    if cfg.analytics_goatcounter:
        return (
            f'<script data-goatcounter="https://{cfg.analytics_goatcounter}'
            '.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>'
        )
    return ""


def _month_label(month: str) -> str:
    year, mon = month.split("-")
    name = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ][int(mon) - 1]
    return f"Ships {name} {year}"


def _card(
    row: sqlite3.Row,
    verdict: GateVerdict,
    glossary: Glossary,
    links: list | None = None,
    lottery: bool = False,
    cfg: Config | None = None,
    classifier: Classifier | None = None,
) -> str:
    title_en = html.escape(glossary.render(row["title"]))
    status_label, status_cls = _STATUS_LABEL.get(row["status"], ("On sale", "on"))
    price = f"¥{row['price_jpy']:,}" if row["price_jpy"] else "—"

    if verdict.unknown:
        badges = '<li class="unknown">Access requirements unverified</li>'
    else:
        badges = "".join(
            f'<li title="{html.escape(GATE_DEFS[k].why_en)}">'
            f"{html.escape(GATE_DEFS[k].label_en)} required</li>"
            for k in verdict.keys
        )

    image = row["image"] or ""
    if image.startswith("//"):
        image = "https:" + image
    img = (
        f'<img src="{html.escape(image)}" alt="" loading="lazy">' if image else ""
    )

    extra = ""
    if lottery and cfg is not None:
        # 抽選は転送代行では原理的に代行できない。ここは自社に引く。
        extra = (
            f'<p class="act"><a href="{html.escape(cfg.contact_url)}">'
            "Ask us to enter the next round →</a></p>"
        )
    elif links:
        # rel="sponsored nofollow" は各プログラムの規約とSEOの両方で必要。
        anchors = " · ".join(
            f'<a href="{html.escape(l.url)}" rel="sponsored nofollow" '
            f'target="_blank">{html.escape(l.label)}</a>'
            for l in links
        )
        extra = f'<p class="act aff">{anchors}<span class="ad">ad</span></p>'

    verdict_line = (classifier or Classifier({})).classify(row["title"])
    # ライン名はカテゴリより具体的で、コレクターが実際に使う単位。
    # 絞り込みはカテゴリ、目視はラインという二段構えにしている。
    line_badge = (
        f'<span class="line">{html.escape(verdict_line.line)}</span>'
        if verdict_line.line
        else ""
    )
    return (
        f'<li class="card" data-cat="{html.escape(verdict_line.category)}"'
        f' data-gate="{1 if verdict.sellable else 0}">{img}'
        f'<div><span class="tag {status_cls}">{status_label}</span>{line_badge}'
        f'<a class="name" href="{html.escape(row["url"])}">{title_en}</a>'
        f'<p class="price">{price} · {html.escape(row["shop"])}</p>'
        f'<ul class="gates">{badges}</ul>{extra}</div></li>'
    )


def render_x_posts(
    rows: list[sqlite3.Row],
    gates_by_source: dict[str, list[SourceGate]],
    glossary: Glossary,
    cfg: Config,
    limit: int = 10,
    classifier: Classifier | None = None,
) -> str:
    """X に手で貼る投稿文。ゲートが確定しているものだけ。

    **カテゴリを持ち回りで拾う。** 素直に上から10件取ると全部が同じ
    カテゴリになり（実際に10件すべて Trading Cards になった）、順に貼ると
    アカウントがBotに見える。1カテゴリ連投は最も安いフォロー解除の理由。
    """
    classifier = classifier or Classifier({})
    buckets: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        buckets[classifier.classify(row["title"]).category].append(row)

    ordered: list[sqlite3.Row] = []
    while any(buckets.values()) and len(ordered) < limit * 3:
        for cat in list(buckets):
            if buckets[cat]:
                ordered.append(buckets[cat].pop(0))

    out: list[str] = []
    for row in ordered:
        if len(out) >= limit:
            break
        item = _row_to_item(row)
        verdict = evaluate(item, gates_by_source.get(row["source"], []))
        if not verdict.sellable:
            continue
        title = glossary.render(row["title"])
        gate = GATE_DEFS[verdict.keys[0]].label_en
        price = f"¥{row['price_jpy']:,}" if row["price_jpy"] else ""
        # タグは2つまで。多いとスパムに見えて逆効果になる。
        # ライン固有のタグ(#Gunpla 等)は英語圏で実際に検索・フォローされているが、
        # 汎用タグ(#anime #figure)はノイズなので出さない。
        line = classifier.classify(row["title"])
        tags = " ".join(t for t in (line.hashtag, "#PBandai") if t)
        out.append(
            f"{_STATUS_LABEL.get(row['status'], ('On sale', ''))[0]}: {title} {price}\n"
            f"Japan only — needs a {gate}.\n{row['url']}\n"
            f"We're in Japan: {cfg.contact_url}\n{tags}"
        )
    return ("\n\n" + "-" * 60 + "\n\n").join(out)


def write(cfg: Config, site_html: str, x_posts: str) -> tuple[Path, Path]:
    cfg.site_dir.mkdir(parents=True, exist_ok=True)
    index = cfg.site_dir / "index.html"
    # X の投稿キューは手作業用の内部ファイル。公開ディレクトリに置くと
    # そのまま配信されてしまうので外に出す。
    queue = cfg.db_path.parent / "x_queue.txt"
    queue.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(site_html, encoding="utf-8")
    queue.write_text(x_posts, encoding="utf-8")

    # GitHub Pages はリポジトリ内の CNAME を見て独自ドメインを当てる。
    # 毎回書き出さないと、Pages の設定画面で入った値を publish が消してしまう。
    if cfg.custom_domain:
        (cfg.site_dir / "CNAME").write_text(cfg.custom_domain + "\n", encoding="utf-8")

    # Jekyll に処理させない。処理させると _ 始まりのファイルが消えるなど
    # 予期しない加工が入る。素の静的ファイルとして出したい。
    (cfg.site_dir / ".nojekyll").write_text("", encoding="utf-8")
    return index, queue


#: 絞り込み。外部ライブラリは使わない（CSPと読み込み速度の両方で不利になる）。
#: カテゴリと「関門あり」は AND で効く。JS が動かない環境では全件出たままになる。
_FILTER_JS = """<script>
(function () {
  var chips = document.querySelectorAll('.chip');
  var cards = document.querySelectorAll('.card');
  var cat = '*', gateOnly = false;

  function apply() {
    var shown = 0;
    cards.forEach(function (c) {
      var okCat = cat === '*' || c.dataset.cat === cat;
      var okGate = !gateOnly || c.dataset.gate === '1';
      var on = okCat && okGate;
      c.hidden = !on;
      if (on) shown++;
    });
    // 見出しごと空になった節は隠す。空の見出しだけが並ぶと壊れて見える。
    document.querySelectorAll('section').forEach(function (s) {
      var any = s.querySelector('.card:not([hidden])');
      s.hidden = !any;
    });
    document.querySelectorAll('.chip').forEach(function (b) {
      var isCat = b.dataset.cat !== undefined;
      b.classList.toggle('on', isCat ? b.dataset.cat === cat : gateOnly);
    });
  }

  chips.forEach(function (b) {
    b.addEventListener('click', function () {
      if (b.dataset.cat !== undefined) cat = b.dataset.cat;
      else gateOnly = !gateOnly;
      apply();
    });
  });
})();
</script>"""

_CSS = """<style>
:root{--bg:#fbfbfa;--fg:#1c1b19;--mut:#6b6862;--line:#e3e0da;--card:#fff;
--acc:#b8452f;--lot:#c2701c;--pre:#2f6fb8;--on:#4b7a45;--warn:#8a7a3a}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#16150f;--fg:#eceae4;--mut:#a5a096;--line:#2f2d26;--card:#1e1c16;
--acc:#e0765c;--lot:#e0a04f;--pre:#6fa8e0;--on:#87b87f;--warn:#c4b06a}}
:root[data-theme=dark]{--bg:#16150f;--fg:#eceae4;--mut:#a5a096;--line:#2f2d26;
--card:#1e1c16;--acc:#e0765c;--lot:#e0a04f;--pre:#6fa8e0;--on:#87b87f;--warn:#c4b06a}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);margin:0;padding:2rem 1.25rem 4rem;
font:16px/1.6 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
header,main,footer{max-width:1080px;margin:0 auto}
h1{font-size:2rem;margin:0 0 .5rem;letter-spacing:-.02em}
.lede{font-size:1.05rem;max-width:60ch;color:var(--fg)}
.meta{color:var(--mut);font-size:.9rem}
.cta{display:inline-block;background:var(--acc);color:#fff;text-decoration:none;
padding:.7rem 1.2rem;border-radius:6px;font-weight:600;margin:.5rem 0 1.5rem}
h2{font-size:1.1rem;border-top:1px solid var(--line);padding-top:1.25rem;
margin:2.5rem 0 1rem;display:flex;gap:.6rem;align-items:baseline}
h2 span{color:var(--mut);font-weight:400;font-size:.85rem}
.grid{list-style:none;padding:0;margin:0;display:grid;gap:1rem;
grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:.9rem;display:flex;gap:.85rem;align-items:flex-start}
.card img{width:76px;height:76px;object-fit:cover;border-radius:5px;flex:none}
.card>div{min-width:0}
.tag{display:inline-block;font-size:.7rem;font-weight:700;letter-spacing:.04em;
text-transform:uppercase;padding:.15rem .45rem;border-radius:3px;color:#fff;
margin-bottom:.4rem}
.tag.lot{background:var(--lot)}.tag.pre{background:var(--pre)}.tag.on{background:var(--on)}
.tag.shut{background:var(--mut)}
.filters{display:flex;flex-wrap:wrap;gap:.5rem;margin:1.25rem 0 .5rem}
.chip{font:inherit;font-size:.85rem;background:var(--card);color:var(--fg);
border:1px solid var(--line);border-radius:999px;padding:.4rem .85rem;cursor:pointer;
display:inline-flex;gap:.4rem;align-items:center}
.chip span{color:var(--mut);font-size:.78rem}
.chip:hover{border-color:var(--acc)}
.chip.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.chip.on span{color:rgba(255,255,255,.75)}
.line{display:inline-block;font-size:.68rem;font-weight:700;letter-spacing:.03em;
color:var(--mut);border:1px solid var(--line);border-radius:3px;padding:.1rem .35rem;
margin:0 0 .4rem .35rem;vertical-align:top}
[hidden]{display:none !important}
section.closed .card{opacity:.92}
.note{color:var(--mut);font-size:.9rem;margin:-.5rem 0 1rem;max-width:60ch}
.act{margin:.5rem 0 0;font-size:.82rem}
.act a{color:var(--acc);font-weight:600;text-decoration:none}
.act a:hover{text-decoration:underline}
.act.aff a{color:var(--pre)}
.ad{display:inline-block;margin-left:.4rem;padding:0 .3rem;border:1px solid var(--line);
border-radius:3px;color:var(--mut);font-size:.65rem;text-transform:uppercase;
letter-spacing:.06em;vertical-align:middle}
.disclosure{font-size:.8rem}
.name{display:block;color:var(--fg);text-decoration:none;font-weight:600;
line-height:1.35;overflow-wrap:anywhere}
.name:hover{color:var(--acc)}
.price{color:var(--mut);font-size:.85rem;margin:.3rem 0}
.gates{list-style:none;padding:0;margin:.4rem 0 0;font-size:.78rem}
.gates li{color:var(--acc);font-weight:600}
.gates li:before{content:"\\1F6AB  "}
.gates li.unknown{color:var(--warn);font-weight:400;font-style:italic}
.gates li.unknown:before{content:"\\26A0\\FE0F  "}
footer p{color:var(--mut);font-size:.85rem;max-width:70ch;
border-top:1px solid var(--line);padding-top:1.25rem;margin-top:3rem}
</style>"""
