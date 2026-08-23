"""設定の読み込み。

Webhook URL のような秘密は `config.yaml` に書かず環境変数から取る
（config.yaml はリポジトリに入るため）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import re

from .affiliate import AffiliateConfig
from .gates import SourceGate

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ShopConfig:
    shop: str
    label: str
    max_pages: int = 5


@dataclass
class SourceConfig:
    name: str
    shops: list[ShopConfig]
    gates: list[SourceGate] = field(default_factory=list)
    per_page: int = 20
    delay_sec: float = 1.5
    timeout: int = 60


@dataclass
class Config:
    sources: list[SourceConfig]
    db_path: Path
    site_dir: Path
    glossary_path: Path
    lines_path: Path
    #: 代行の申し込み先。通知とWebページのCTAに出る。
    contact_url: str
    brand_name: str
    affiliate: AffiliateConfig
    #: 独自ドメイン。GitHub Pages 用の CNAME を書き出す。空なら書き出さない。
    custom_domain: str = ""
    #: 公開URL。og:image は絶対URLでないと無視されるので必須。
    site_url: str = "https://jpgate.net"
    #: アクセス解析。どちらも Cookie を使わない（同意バナー不要）。
    #: 空なら何も出さない。「入れたつもり」を防ぐため readiness は
    #: 生成物にタグが在るかで判定している。
    analytics_cf_token: str = ""
    analytics_goatcounter: str = ""
    #: 「終了」欄に載せる件数。二次流通の受け皿。
    closed_limit: int = 60
    #: 1回の通知でDiscordに送る上限。事故ったときの被害を切る。
    max_notify_per_run: int = 20
    #: 訳出率がこれ未満なら「原題のまま」注記を付ける。
    min_translation_coverage: float = 0.5

    #: 1回の実行で相場を引き直す件数。eBay の呼び出し枠を守るための上限。
    #: 相場は1時間で動くものではないので、少しずつ古い順に回せばよい。
    max_price_checks_per_run: int = 25
    #: 相場を引き直す間隔（日）。
    price_stale_days: int = 14
    #: 相場を引くときに要求する訳出率。**表示用より厳しくする**。
    #:
    #: `min_translation_coverage` は「読めるか」の敷居。相場に必要なのは
    #: 「特定できるか」で、別の要件。実測で "ガンダムアストレイ
    #: ゴールドフレーム" が訳出率54%で通り、未訳の「ゴールドフレーム」＝
    #: **まさに商品を区別する語**が落ちて、赤と金が同じ相場になった。
    price_min_coverage: float = 0.8

    #: 通知先がフォーラムチャンネルか。
    #:
    #: **間違えると通知が全部落ちる。** フォーラムに `thread_name` 無しで
    #: 投げると Discord が拒否し、逆に通常のテキストチャンネルへ
    #: `thread_name` を付けても拒否される。どちらも 400 が返るだけで
    #: 理由が読めないので、doctor で先に警告する。
    #:
    #: フォーラムにすると1件が1投稿になり、流れて消えない。ジャンルは
    #: チャンネルを分けずにタグで絞れる(件数の少ないジャンル用に空の
    #: チャンネルを作らずに済む)。
    notify_forum: bool = False
    #: ジャンル → フォーラムのタグID。空なら**タグ無しで投稿する**。
    #:
    #: タグIDが手に入らなくても投稿自体は始められるようにしてある。
    #: ID は Discord の開発者モードでタグを右クリックして取得する。
    notify_forum_tags: dict[str, str] = field(default_factory=dict)

    #: 1回に Discord へ送る X 下書きの上限。初回は掲載中の商品が丸ごと
    #: 対象になるので、これが無いと初回だけ大量に飛ぶ。
    max_x_posts_per_run: int = 1
    #: **1日あたり**の上限。1回あたりの上限だけでは総量が縛れない
    #: (毎時走らせれば24倍になる)。X に貼れるのは人の手なので、
    #: 生成量ではなく**貼れる量**に合わせる。
    max_x_posts_per_day: int = 1

    #: 生成した縦動画の置き場。Discord へ送ったあとも手元に残す
    #: （TikTok へは手で上げるので、送信に失敗しても貼れる必要がある）。
    clip_dir: Path = ROOT / "data" / "clips"
    #: 1本の動画に入れる商品数。5件×5.5秒＋前後で35秒前後になる。
    #: 増やすと尺が伸びて最後まで見られなくなる。
    max_clip_items: int = 5

    @property
    def discord_webhook(self) -> str | None:
        return os.environ.get("JPGATE_DISCORD_WEBHOOK")

    @property
    def ebay_keys(self) -> tuple[str, str] | None:
        """eBay の Production keyset。**リポジトリは公開なので環境変数のみ**。

        未設定なら None を返し、呼び出し側は相場の取得を諦める。
        価格が無くてもサイトは成立するので、ここで落とさない。
        """
        cid = os.environ.get("JPGATE_EBAY_CLIENT_ID", "")
        secret = os.environ.get("JPGATE_EBAY_CLIENT_SECRET", "")
        return (cid, secret) if cid and secret else None

    @property
    def x_queue_webhook(self) -> str | None:
        """X 下書きの送り先。**`#drops` とは別のチャンネルにすること**.

        下書きは自分専用の作業用メモで、メンバーに見せるものではない
        (同じ商品の告知が2回流れることになる)。別 webhook を要求することで
        取り違えを構造的に防ぐ。未設定なら送信そのものを行わない。
        """
        return os.environ.get("JPGATE_X_QUEUE_WEBHOOK")

    @property
    def clip_webhook(self) -> str | None:
        """動画の送り先。**`#drops` とは別のチャンネルにすること**.

        x_queue_webhook と同じ理由。動画は自分がTikTokへ手で上げるための
        受け渡しであって、メンバーに見せる告知ではない。
        """
        return os.environ.get("JPGATE_CLIP_WEBHOOK")


#: Cloudflare のビーコントークンは16進の文字列。ドキュメントの例
#: `$SITE_TOKEN` をそのまま貼ると「解析を入れたつもり」になるので弾く。
#: 空として扱えば、タグは出ず readiness も UNKNOWN のままになる＝気づける。
_RE_CF_TOKEN = re.compile(r"^[0-9a-f]{20,}$", re.I)


def _clean_cf_token(raw: str) -> str:
    raw = (raw or "").strip()
    return raw if _RE_CF_TOKEN.match(raw) else ""


def load(path: Path | str | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    sources = []
    for s in raw["sources"]:
        sources.append(
            SourceConfig(
                name=s["name"],
                shops=[
                    ShopConfig(
                        shop=c["shop"],
                        label=c.get("label", c["shop"]),
                        max_pages=c.get("max_pages", 5),
                    )
                    for c in s["shops"]
                ],
                gates=[SourceGate.from_config(g) for g in s.get("gates", [])],
                per_page=s.get("per_page", 20),
                delay_sec=s.get("delay_sec", 1.5),
                timeout=s.get("timeout", 60),
            )
        )

    out = raw.get("output", {})
    aff = raw.get("affiliate", {})
    return Config(
        sources=sources,
        db_path=ROOT / out.get("db", "data/jpgate.sqlite"),
        site_dir=ROOT / out.get("site_dir", "docs"),
        glossary_path=ROOT / out.get("glossary", "data/glossary.yaml"),
        lines_path=ROOT / out.get("lines", "data/lines.yaml"),
        contact_url=raw["business"]["contact_url"],
        brand_name=raw["business"].get("brand_name", "JPGate"),
        # ID は環境変数を優先する。審査に通ったら config を触らずに入れられる。
        affiliate=AffiliateConfig(
            ebay_campaign_id=os.environ.get(
                "JPGATE_EBAY_CAMPAIGN_ID", str(aff.get("ebay_campaign_id", "") or "")
            ),
            amazon_us_tag=os.environ.get(
                "JPGATE_AMAZON_US_TAG", str(aff.get("amazon_us_tag", "") or "")
            ),
        ),
        custom_domain=str(raw.get("business", {}).get("custom_domain", "") or ""),
        site_url=str(raw["business"].get("site_url") or "https://jpgate.net"),
        analytics_cf_token=_clean_cf_token(
            os.environ.get(
                "JPGATE_CF_BEACON",
                str(raw.get("analytics", {}).get("cloudflare_token", "") or ""),
            )
        ),
        analytics_goatcounter=str(raw.get("analytics", {}).get("goatcounter_code", "") or ""),
        closed_limit=out.get("closed_limit", 60),
        clip_dir=ROOT / out.get("clip_dir", "data/clips"),
        max_clip_items=raw.get("clip", {}).get("max_items", 5),
        max_notify_per_run=raw.get("notify", {}).get("max_per_run", 20),
        min_translation_coverage=raw.get("notify", {}).get("min_translation_coverage", 0.5),
        max_x_posts_per_day=raw.get("x", {}).get("max_per_day", 1),
        max_price_checks_per_run=raw.get("prices", {}).get("max_per_run", 25),
        price_stale_days=raw.get("prices", {}).get("stale_days", 14),
        price_min_coverage=raw.get("prices", {}).get("min_coverage", 0.8),
        notify_forum=bool(raw.get("notify", {}).get("forum", False)),
        notify_forum_tags={
            str(k): str(v)
            for k, v in (raw.get("notify", {}).get("forum_tags") or {}).items()
        },
    )
