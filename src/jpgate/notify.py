"""Discord 通知（英語）。

通知1件の狙いは「この商品が始まった」ではなく
**「始まった。そして君にはこの関門がある」**を同時に伝えること。
関門バッジが営業そのものなので、ゲートが UNKNOWN の商品には CTA を出さない。
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request

from .config import Config
from .gates import GATE_DEFS, GateVerdict, badges_en
from .models import (
    EVENT_DEADLINE,
    EVENT_LOTTERY_OPEN,
    EVENT_RESERVATION_OPEN,
    EVENT_RESTOCK,
)
from .translate import Glossary

_HEADLINE = {
    EVENT_LOTTERY_OPEN: ("🎲 Lottery now open", 0xE67E22),
    EVENT_RESERVATION_OPEN: ("🆕 Pre-order now open", 0x3498DB),
    EVENT_RESTOCK: ("♻️ Back on sale", 0x2ECC71),
    EVENT_DEADLINE: ("⏳ Closing soon", 0xE74C3C),
}


def build_embed(
    row: sqlite3.Row,
    verdict: GateVerdict,
    glossary: Glossary,
    cfg: Config,
) -> dict:
    title_ja = row["title"]
    title_en = glossary.render(title_ja)
    coverage = glossary.coverage(title_ja)

    headline, color = _HEADLINE.get(row["kind"], ("Update", 0x95A5A6))

    lines: list[str] = []
    lines.extend(badges_en(verdict))

    if verdict.sellable:
        lines.append("")
        # ゲートごとの「なぜ越えられないか」がそのまま需要の説明になる。
        for key in verdict.keys:
            lines.append(f"• {GATE_DEFS[key].why_en}")
        lines.append("")
        lines.append(f"**We are in Japan and can enter for you → {cfg.contact_url}**")
    else:
        lines.append("")
        lines.append(
            "_We have not verified whether this item can be ordered from outside "
            "Japan. No proxy offer until we check._"
        )

    fields = []
    if row["price_jpy"]:
        fields.append({"name": "Price", "value": f"¥{row['price_jpy']:,}", "inline": True})
    if row["ship_month"]:
        year, month = row["ship_month"].split("-")
        fields.append({"name": "Ships", "value": f"{year}-{month}", "inline": True})
    fields.append({"name": "Shop", "value": row["shop"], "inline": True})

    embed: dict = {
        "title": f"{headline} — {title_en}"[:250],
        "url": row["url"],
        "color": color,
        "description": "\n".join(lines)[:4000],
        "fields": fields,
        "footer": {"text": f"{cfg.brand_name} · {row['source']}"},
    }
    if row["image"]:
        image = row["image"]
        embed["thumbnail"] = {"url": image if image.startswith("http") else f"https:{image}"}
    if coverage < cfg.min_translation_coverage:
        embed["description"] = (
            f"_Original title: {title_ja}_\n\n{embed['description']}"
        )[:4000]
    return embed


def post(webhook: str, embeds: list[dict], timeout: int = 30) -> None:
    """Discord へ投げる。embed は1リクエスト10件が上限。"""
    for i in range(0, len(embeds), 10):
        payload = json.dumps({"embeds": embeds[i : i + 10]}).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                raise urllib.error.HTTPError(
                    webhook, resp.status, "discord rejected", resp.headers, None
                )
