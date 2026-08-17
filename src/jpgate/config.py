"""設定の読み込み。

Webhook URL のような秘密は `config.yaml` に書かず環境変数から取る
（config.yaml はリポジトリに入るため）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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
    #: 代行の申し込み先。通知とWebページのCTAに出る。
    contact_url: str
    brand_name: str
    affiliate: AffiliateConfig
    #: 独自ドメイン。GitHub Pages 用の CNAME を書き出す。空なら書き出さない。
    custom_domain: str = ""
    #: 「終了」欄に載せる件数。二次流通の受け皿。
    closed_limit: int = 60
    #: 1回の通知でDiscordに送る上限。事故ったときの被害を切る。
    max_notify_per_run: int = 20
    #: 訳出率がこれ未満なら「原題のまま」注記を付ける。
    min_translation_coverage: float = 0.5

    @property
    def discord_webhook(self) -> str | None:
        return os.environ.get("JPGATE_DISCORD_WEBHOOK")


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
        closed_limit=out.get("closed_limit", 60),
        max_notify_per_run=raw.get("notify", {}).get("max_per_run", 20),
        min_translation_coverage=raw.get("notify", {}).get("min_translation_coverage", 0.5),
    )
