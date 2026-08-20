"""TikTok 用の縦動画（1080x1920）を作る。

告知文と同じ材料から、同じ規則で作る。**動画のために新しい主張を作らない**——
台本を生成AIに書かせないのはそのためで、文言は `publish.build_x_posts` と
同じ経路（用語集＋ゲート定義）から来る。検証できない主張を混ぜた瞬間、
「関門を名指しできる」という商売の根拠そのものが薄まる。

## この形にした理由

- **主役は商品画像**。ストック映像に商品名を乗せた動画は、探している人の
  検索結果に出ても「その商品ではない」と判断されて終わる。画像は走査時に
  既に取れている（`items.image`）ので、新しい取得経路を作る必要もない。
- **1商品1本ではなく、数件を1本にまとめる**。1本あたりの尺が伸びて保持率が
  上がるうえ、投稿頻度が現実的な回数に収まる。
- **無音で出す**。TikTok は動画に埋め込んだBGMより、アプリ側で付けた曲の
  ほうがリーチが出る。手動アップロードなのでそれができる。ただし音声
  トラック自体は無音で入れておく（音声トラックの無い mp4 を弾く経路があるため）。
- **ゲートが確定した商品しか入れない**。サイトや Discord と同じ規則。
  UNKNOWN の商品に代行のCTAを出さないという不変条件は媒体を問わない。
"""

from __future__ import annotations

import base64
import html as html_mod
import os
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from string import Template
from tempfile import TemporaryDirectory

from .config import ROOT, Config
from .gates import GATE_DEFS, SourceGate, evaluate
from .lines import Classifier
from .publish import SOURCE_HASHTAG, STATUS_LABEL, row_to_item, select_diverse
from .translate import Glossary

#: TikTok / Reels / Shorts の共通仕様。
WIDTH, HEIGHT = 1080, 1920
FPS = 30

#: カードは2倍の解像度で描いてから縮める。zoompan は入力が出力と同じ大きさだと
#: 拡大時に画素が踊る。
SCALE = 2

INTRO_SEC = 2.5
ITEM_SEC = 5.5
OUTRO_SEC = 3.5

#: 出力の目安サイズ。Discord の添付上限（notify.FILE_LIMIT = 10MB）より
#: 下に置く。上限ちょうどを狙うと、伸び縮みの分で越える。
_SIZE_TARGET = 8 * 1024 * 1024

#: ブランド色。`brand/make_icon.py` と同じ値（あちらは単体スクリプトなので
#: import できない。変えるときは両方直す）。
VERMILION = "#C43D2A"
INK = "#16150F"
PAPER = "#FBFBFA"
MUTED = "#A5A096"

#: 状態ごとのチップの色。Discord の embed（`notify._HEADLINE`）と揃える。
_CHIP_COLOR = {
    "lot": VERMILION,
    "pre": "#3498DB",
    "listed": "#7F8C8D",
    "on": "#2ECC71",
}

#: TikTok にだけ付ける汎用タグ。**X の規則（汎用タグは出さない）とは意図的に
#: 変えている**。X はフォロー関係で届くので汎用タグは雑音だが、TikTok は
#: タグが探索面そのもので、ライン固有のタグだけでは母数が小さすぎる。
#: 増やすならここ一箇所。
_TIKTOK_TAGS = ("#japanexclusive", "#preorder")

#: 商品名に出る語 → 足すタグ。**その語が実際に入っている商品にだけ付く**
#: （`SOURCE_HASHTAG` と同じ規律。推測でタグを付けない）。
#:
#: 用語集は1つの綴りしか選べないが、タグは複数出せる。この差を使って
#: 役割を分けている——商品名は**理解**のために読める綴り（Capsule Toy）、
#: タグは**探索**のために実際に検索される綴り（Gashapon）を両方出す。
#: X 側は2つ上限を守るのでここは使わない（`publish.build_x_posts`）。
_TOPIC_TAGS = {
    "カプセルトイ": ("#CapsuleToy", "#Gashapon"),
}

_UA = "JPGate/0.1 (+https://github.com/tknjnsk/jpgate)"

#: マジックバイト → MIME。**拡張子では判定しない**（CDN の URL に拡張子が
#: 無いことがある）。判別できない画像は使わない＝その商品を落とす。
#: 推測して data URI を作ると Chromium が黙って空の枠を描く。
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
)


@dataclass(frozen=True)
class Card:
    """動画に入れる商品1件。文言はすべて確定済みの値で、ここでは作らない。"""

    source: str
    item_id: str
    status_label: str
    status_cls: str
    title: str
    price: str
    gate_label: str
    gate_why: str
    shop: str
    url: str
    hashtags: tuple[str, ...]
    image_data_uri: str
    #: 「この日に始まった」と言える日付。言えないときは空
    #: （`Store.seed_at` の説明を参照）。
    opened_on: str = ""
    #: 発送月。予約商品はここが一番効く情報。
    ships: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.item_id)


# --------------------------------------------------------------------------
# 材料集め
# --------------------------------------------------------------------------
#: 一覧の画像URL → 同じ画像の大判URL。(元の断片, 大判の断片)。
#:
#: 一覧が返すのは p-bandai が200px、ポケモンセンターが500px で、そのまま
#: 1080幅に伸ばすと商品がぼやける。大判は**存在を確認してから使う**
#: （下の fetch_image が実際に取れたときだけ差し替える）。無ければ元のまま。
#: 推測でURLを組み立てているように見えるが、外れたら元に戻るだけで、
#: 「取れていないのに取れたことにする」経路は無い。
_BIGGER = {
    "p-bandai": ("/bc/img/model/m/", "/bc/img/model/xl/"),  # 200px -> 1200px
    "pokemon-center": ("/M/", "/L/"),  # 500px -> 1500px
}


def _download(url: str, timeout: int) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def fetch_image(url: str, source: str = "", timeout: int = 20) -> str | None:
    """商品画像を data URI にして返す。取れなければ None。

    HTML から CDN の URL を直接参照させない。参照させると読み込みに失敗した
    ときに枠だけが描かれ、**中身の無い動画が黙って完成する**。ここで
    バイト列を確かめておけば、失敗はその商品を落とすという形で表に出る。
    """
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url

    candidates = []
    small, big = _BIGGER.get(source, ("", ""))
    if small and small in url:
        candidates.append(url.replace(small, big))
    candidates.append(url)

    for candidate in candidates:
        raw = _download(candidate, timeout)
        if raw is None:
            continue
        for magic, mime in _MAGIC:
            if raw.startswith(magic):
                return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    return None


def build_cards(
    rows: list[sqlite3.Row],
    gates_by_source: dict[str, list[SourceGate]],
    glossary: Glossary,
    cfg: Config,
    limit: int = 5,
    classifier: Classifier | None = None,
    exclude: set[tuple[str, str]] | None = None,
    seed_at: dict[tuple[str, str], str] | None = None,
) -> list[Card]:
    """動画に入れる商品を選ぶ。並べ替えと除外は X 下書きと同じ規則。

    `seed_at` を渡すと「この日に始まった」と言える商品にだけ日付が付く。
    渡さなければどの商品にも日付を出さない（言えないことは書かない）。
    """
    classifier = classifier or Classifier({})
    seed_at = seed_at or {}

    # 開始日を言える商品を先に回す。動画で一番強い一行は「この日に始まった」で、
    # それが言えるのは seed 後に現れた商品だけ（掲載439件中43件）。sorted は
    # 安定なので、カテゴリ持ち回りの並びはこの中で保たれる。
    rows = sorted(
        rows, key=lambda r: not _opened_on(r, seed_at.get((r["source"], r["shop"])))
    )

    out: list[Card] = []
    for row in select_diverse(rows, classifier, limit, exclude):
        if len(out) >= limit:
            break
        verdict = evaluate(row_to_item(row), gates_by_source.get(row["source"], []))
        if not verdict.sellable:
            continue
        data_uri = fetch_image(row["image"] or "", row["source"])
        if data_uri is None:
            # 画像が主役なので、無い商品は動画に入れない（サイトや Discord には
            # 引き続き出る。落ちるのは動画からだけ）。
            continue
        gate = GATE_DEFS[verdict.keys[0]]
        label, cls = STATUS_LABEL.get(row["status"], ("On sale", "on"))
        line = classifier.classify(row["title"], row["source"])
        topic = tuple(
            tag
            for word, tags in _TOPIC_TAGS.items()
            if word in row["title"]
            for tag in tags
        )
        tags = tuple(
            dict.fromkeys(
                t
                for t in (line.hashtag, SOURCE_HASHTAG.get(row["source"], ""), *topic)
                if t
            )
        )
        out.append(
            Card(
                source=row["source"],
                item_id=row["item_id"],
                status_label=label,
                status_cls=cls,
                title=glossary.render(row["title"]),
                price=f"¥{row['price_jpy']:,}" if row["price_jpy"] else "",
                gate_label=gate.label_en,
                gate_why=gate.why_en,
                shop=row["shop"],
                url=row["url"],
                hashtags=tags,
                image_data_uri=data_uri,
                opened_on=_opened_on(row, seed_at.get((row["source"], row["shop"]))),
                ships=_ships(row["ship_month"]),
            )
        )
    return out


def _opened_on(row: sqlite3.Row, seed: str | None) -> str:
    """「この日に始まった」と書ける日付。書けなければ空文字。

    seed 走査で入った商品の first_seen は「並んでいたものを最初に見た日」で
    あって開始日ではない。そこを混ぜると、数百件が同じ日に一斉に始まったと
    主張する動画ができる。**言えないときは何も書かない**。
    """
    first_seen = row["first_seen"]
    if not seed or not first_seen or first_seen <= seed:
        return ""
    try:
        return datetime.fromisoformat(first_seen).strftime("%d %b")
    except ValueError:
        return ""


def _ships(ship_month: str | None) -> str:
    if not ship_month:
        return ""
    try:
        year, month = ship_month.split("-")
        return date(int(year), int(month), 1).strftime("%b %Y")
    except (ValueError, TypeError):
        return ""


def caption(cards: list[Card], cfg: Config, today: date | None = None) -> str:
    """TikTok の説明欄に貼る文。

    TikTok の説明欄でURLは押せないので、商品URLは載せない（押せないリンクを
    並べても「読んでいない」ことが露見するだけ）。導線はプロフィールに一本化する。
    """
    today = today or date.today()
    domain = cfg.site_url.replace("https://", "").replace("http://", "").rstrip("/")
    lines = [f"Japan-only drops — {today.strftime('%d %b %Y')}", ""]
    for card in cards:
        price = f" {card.price}" if card.price else ""
        opened = f" (opened {card.opened_on})" if card.opened_on else ""
        lines.append(f"· {card.title}{price}{opened} — needs a {card.gate_label}")
    tags = dict.fromkeys([t for c in cards for t in c.hashtags] + list(_TIKTOK_TAGS))
    lines += [
        "",
        # 説明欄でURLは押せないので、押せる場所（プロフィール）を名指しする。
        # ドメインも併記して、打つ人と押す人の両方を拾う。
        f"We're in Japan and we order these for you → {domain} (link in bio)",
        "",
        " ".join(tags),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# カードを描く
# --------------------------------------------------------------------------
_CSS = Template(
    """
* { margin:0; padding:0; box-sizing:border-box }
body { background:$ink }
.card {
  position:relative; width:${w}px; height:${h}px; overflow:hidden;
  background:$ink; color:$paper;
  font-family:'Segoe UI','Yu Gothic UI','Yu Gothic',Meiryo,system-ui,sans-serif;
}
.bg {
  position:absolute; inset:-10%;
  background-size:cover; background-position:center;
  filter:blur(70px) saturate(1.3) brightness(.42);
}
/* 商品をできるだけ大きく出す。3秒で「その商品だ」と分かることが最優先で、
   説明はそのあと。読ませる文字は減らして大きくする。 */
.photo {
  position:absolute; top:210px; left:70px; right:70px; height:840px;
}
.photo img {
  /* width/height を指定しないと、元画像が小さいときに実寸で出る
     （200pxのサムネイルが画面の中央に切手のように貼られた）。 */
  width:100%; height:100%; object-fit:contain;
  border-radius:26px; box-shadow:0 46px 90px rgba(0,0,0,.6);
}
.head { position:absolute; top:70px; left:70px; right:70px;
        display:flex; align-items:center; gap:26px }
.chip {
  padding:16px 34px; border-radius:999px;
  font-size:34px; font-weight:800; letter-spacing:.10em; text-transform:uppercase;
}
.when { font-size:38px; font-weight:800; letter-spacing:.04em }
.count { margin-left:auto; font-size:34px; font-weight:700; color:$muted }
/* TikTok は下 ~330px にキャプションと音源、右 ~190px にボタン列を重ねる。
   そこに文字を置くと本番でだけ読めなくなるので、余白として空けておく。 */
.panel {
  position:absolute; left:0; right:0; bottom:434px;
  padding:190px 190px 40px 70px;
  background:linear-gradient(to bottom, rgba(22,21,15,0) 0, $ink 160px, $ink 100%);
}
.title {
  font-size:54px; font-weight:800; line-height:1.14; letter-spacing:-.015em;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
}
.meta { display:flex; align-items:baseline; gap:28px; margin-top:22px }
.price { font-size:56px; font-weight:800; color:$verm }
.shop { font-size:30px; color:$muted }
/* 関門は一文だけ。5秒のカードで読ませられるのは1行が限界で、根拠の詳細
   （GateDef.why_en）はサイトと Discord が持っている。ここは導線であって
   説明ではない。 */
.gate {
  margin-top:28px; padding-left:26px; border-left:8px solid $verm;
  font-size:44px; font-weight:800; line-height:1.22;
}

/* どのカードにも同じ帯を出す。動画の目的はサイトに来てもらうことなので、
   最後の1枚だけにCTAを置くと、途中で離脱した人には何も残らない。 */
.band {
  position:absolute; left:0; right:0; bottom:330px; height:104px;
  background:$verm; color:$paper;
  display:flex; align-items:center; justify-content:center; gap:22px;
  font-size:40px; font-weight:800; letter-spacing:.01em;
}
.band .dom { letter-spacing:.06em }

.mid { display:flex; flex-direction:column; align-items:center; justify-content:center;
       height:100%; text-align:center; padding:0 110px 230px; gap:34px }
.mid img { width:280px; height:280px; border-radius:52px }
.kicker { font-size:36px; font-weight:700; color:$verm; letter-spacing:.16em;
          text-transform:uppercase }
.big { font-size:96px; font-weight:800; line-height:1.06; letter-spacing:-.02em }
.sub { font-size:40px; color:$muted; line-height:1.35 }
.cta { font-size:44px; font-weight:800; color:$paper; background:$verm;
       padding:22px 44px; border-radius:22px }
/* ドメインの添え物。同じ大きさにすると主役が2つになって、どちらも読まれない。 */
.bio { font-size:32px; color:$muted; margin-top:-14px }
"""
).substitute(w=WIDTH, h=HEIGHT, ink=INK, paper=PAPER, verm=VERMILION, muted=MUTED)


def _icon_data_uri() -> str:
    """ブランドアイコン。無ければ空文字（動画自体は作れる）。"""
    path = ROOT / "brand" / "icon_a_white_on_vermilion.svg"
    if not path.exists():
        return ""
    raw = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/svg+xml;base64,{raw}"


def _intro_html(cfg: Config, today: date) -> str:
    icon = _icon_data_uri()
    img = f'<img src="{icon}">' if icon else ""
    return (
        f'<div class="card"><div class="mid">{img}'
        f'<div class="kicker">{today.strftime("%d %b %Y")}</div>'
        f'<div class="big">Japan-only<br>drops</div>'
        '<div class="sub">Open now &mdash; and blocked to overseas buyers.</div>'
        "</div></div>"
    )


def _band(cfg: Config) -> str:
    """どのカードにも出す導線。ドメインは裸で出す（押せる必要はない、
    覚えて打てればいい）。"""
    domain = cfg.site_url.replace("https://", "").replace("http://", "").rstrip("/")
    return (
        '<div class="band"><span>We order it for you</span>'
        f'<span class="dom">{html_mod.escape(domain)}</span></div>'
    )


def _item_html(card: Card, index: int, total: int, cfg: Config) -> str:
    price = f'<div class="price">{html_mod.escape(card.price)}</div>' if card.price else ""
    ships = f'<div class="shop">Ships {html_mod.escape(card.ships)}</div>' if card.ships else ""
    when = f'<div class="when">{html_mod.escape(card.opened_on)}</div>' if card.opened_on else ""
    chip = _CHIP_COLOR.get(card.status_cls, MUTED)
    return (
        '<div class="card">'
        f'<div class="bg" style="background-image:url({card.image_data_uri})"></div>'
        '<div class="head">'
        f'<div class="chip" style="background:{chip}">'
        f"{html_mod.escape(card.status_label)}</div>{when}"
        f'<div class="count">{index} / {total}</div></div>'
        f'<div class="photo"><img src="{card.image_data_uri}"></div>'
        '<div class="panel">'
        f'<div class="title">{html_mod.escape(card.title)}</div>'
        f'<div class="meta">{price}{ships}</div>'
        f'<div class="gate">Japan only &mdash; needs a '
        f"{html_mod.escape(card.gate_label)}.</div>"
        "</div>"
        f"{_band(cfg)}</div>"
    )


def _outro_html(cfg: Config) -> str:
    """最後の1枚はドメインを大きく、その下に「Link in bio」。

    主役はドメインのまま。「Link in bio」を主役にするとプロフィールを開かせる
    一手間が要るうえ、覚えて打てるドメインのほうが後から効く。ただし
    **TikTok の説明欄でURLは押せない**ので、押せる唯一の場所がプロフィール
    リンクだという事実は添えておく。打つ人と押す人の両方を拾う。
    """
    icon = _icon_data_uri()
    img = f'<img src="{icon}">' if icon else ""
    domain = cfg.site_url.replace("https://", "").replace("http://", "").rstrip("/")
    return (
        f'<div class="card"><div class="mid">{img}'
        '<div class="sub">You cannot order these from outside Japan.</div>'
        "<div class=\"big\">We're in Japan.<br>We order<br>it for you.</div>"
        f'<div class="cta">{html_mod.escape(domain)}</div>'
        '<div class="bio">— or tap the link in our bio</div>'
        "</div></div>"
    )


def deck_html(cards: list[Card], cfg: Config, today: date) -> str:
    """全カードを1ページに縦に並べた HTML。1枚ずつ要素として撮る。"""
    parts = [_intro_html(cfg, today)]
    parts += [_item_html(c, i + 1, len(cards), cfg) for i, c in enumerate(cards)]
    parts.append(_outro_html(cfg))
    return f"<!doctype html><meta charset='utf-8'><style>{_CSS}</style>" + "".join(parts)


def render_cards(html: str, out_dir: Path) -> list[Path]:
    """カードを PNG にする。Playwright（`brand/make_og.py` と同じ経路）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 環境依存
        raise RuntimeError(
            "playwright が入っていません: "
            "pip install playwright && playwright install chromium"
        ) from exc

    paths: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=SCALE
        )
        page.set_content(html)
        page.wait_for_timeout(400)  # 画像は data URI だが描画の落ち着きを待つ
        for i, el in enumerate(page.locator(".card").all()):
            path = out_dir / f"card{i:02d}.png"
            el.screenshot(path=str(path))
            paths.append(path)
        browser.close()
    return paths


# --------------------------------------------------------------------------
# 動画にする
# --------------------------------------------------------------------------
def ffmpeg_path() -> str:
    """ffmpeg の場所。見つからなければ落とす。

    **Playwright 同梱の ffmpeg は使えない**（VP8/WebM 専用ビルドで、libx264 も
    zoompan も入っていない）。PATH に本物が無いのに動いたように見えるのが
    一番困るので、ここで止める。
    """
    override = os.environ.get("JPGATE_FFMPEG")
    if override:
        if not Path(override).exists():
            raise RuntimeError(f"JPGATE_FFMPEG が指す実行ファイルがありません: {override}")
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "ffmpeg が見つかりません。`winget install Gyan.FFmpeg` で入れるか、"
        "JPGATE_FFMPEG に実行ファイルのパスを設定してください"
    )


def _zoompan(frames: int, zoom_in: bool, zmax: float = 1.12) -> str:
    """静止画にゆっくりした寄り引きを付ける。

    向きを1枚ごとに入れ替える。全部同じ向きだと、並べたとき機械が作ったものに
    しか見えない。

    **`d=1` で、倍率は出力コマ数 `on` から直に決める。** `d` に総コマ数を
    入れる書き方もあるが、あれは入力が1コマのときの書き方で、`-loop 1` で
    与えた入力にそのまま使うと**入力コマ数 × d コマ**が出る。実際に35秒の
    つもりが150MBを超え、いつまでも終わらなかった。`d=1` なら入力1コマに
    出力1コマで、尺は入力の長さがそのまま決める。
    """
    step = (zmax - 1.0) / max(1, frames - 1)
    if zoom_in:
        z = f"min(1+{step:.6f}*on,{zmax})"
    else:
        z = f"max({zmax}-{step:.6f}*on,1.0)"
    return (
        f"zoompan=z='{z}':d=1"
        ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={WIDTH}x{HEIGHT}:fps={FPS}"
    )


def compose_args(pngs: list[Path], durations: list[float], out: Path, ffmpeg: str) -> list[str]:
    """ffmpeg のコマンドライン。組み立てだけを切り出してある（尺の検査のため）。"""
    if len(pngs) != len(durations):
        raise ValueError(f"画像 {len(pngs)} 枚に対して尺が {len(durations)} 件です")
    args: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for png, sec in zip(pngs, durations):
        # -framerate を FPS に固定する。既定は 25 なので、指定しないと
        # 入力コマ数と出力コマ数がずれて尺が狂う。
        args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{sec}", "-i", str(png)]
    args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    chains = []
    for i, sec in enumerate(durations):
        frames = max(1, round(sec * FPS))
        chains.append(
            f"[{i}:v]scale={WIDTH * SCALE}:{HEIGHT * SCALE},setsar=1,"
            f"{_zoompan(frames, zoom_in=(i % 2 == 0))}[v{i}]"
        )
    joined = "".join(f"[v{i}]" for i in range(len(pngs)))
    chains.append(f"{joined}concat=n={len(pngs)}:v=1:a=0,format=yuv420p[v]")

    # Discord の添付上限に収まる上限ビットレートを尺から決める。CRF だけだと
    # 商品数に比例してファイルが伸び、5件で8.0MB まで来た（上限10MB）。
    # 送る前に ValueError で落とすことはできるが、**動画を作り終えてから
    # 落ちる**ので、先に収まる形で作る。
    budget_bits = _SIZE_TARGET * 8
    maxrate = int(budget_bits / max(1.0, sum(durations))) - 96_000
    args += [
        "-filter_complex", ";".join(chains),
        "-map", "[v]",
        "-map", f"{len(pngs)}:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-maxrate", str(max(600_000, maxrate)), "-bufsize", str(max(1_200_000, maxrate * 2)),
        "-profile:v", "high", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "96k",
        "-shortest", "-movflags", "+faststart",
        str(out),
    ]
    return args


def compose(pngs: list[Path], durations: list[float], out: Path, ffmpeg: str) -> Path:
    """PNG を並べて mp4 にする。1プロセスで完結させる（中間ファイルを残さない）。"""
    args = compose_args(pngs, durations, out, ffmpeg)
    proc = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg が失敗しました:\n{proc.stderr.strip()[-2000:]}")
    return out


def render(cards: list[Card], cfg: Config, out: Path, today: date | None = None) -> Path:
    """カード群から mp4 を1本作る。"""
    if not cards:
        raise ValueError("カードが0件です。動画は作りません")
    today = today or date.today()
    ffmpeg = ffmpeg_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    durations = [INTRO_SEC] + [ITEM_SEC] * len(cards) + [OUTRO_SEC]
    with TemporaryDirectory() as tmp:
        pngs = render_cards(deck_html(cards, cfg, today), Path(tmp))
        if len(pngs) != len(durations):
            # 撮れた枚数が合わないのに尺だけ合わせると、無関係な画像が
            # 無関係な長さで出る。作らずに落とす。
            raise RuntimeError(
                f"カード {len(durations)} 枚のはずが {len(pngs)} 枚しか撮れていません"
            )
        compose(pngs, durations, out, ffmpeg)
    return out
