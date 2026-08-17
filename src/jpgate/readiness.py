"""アフィリエイト審査に出せる状態かの判定。

## なぜ「そろそろ申請どうですか」ではいけないか

リマインダーは判断材料を持たないので、受け取った側が結局自分で調べ直すことになる。
ここでは**審査側が実際に見るもの**を数えて、足りないものを名指しする。

## 分からないことは分からないと言う

流入数は解析タグを入れない限り**原理的に測れない**。推測値を出すと、
それを根拠に申請時期を決めてしまう。よって流入は `UNKNOWN` を返し、
「解析を入れるまで判断できない」と明示する。

これは Amazon で特に効く。Amazon アソシエイトは**承認後180日以内に
適格販売3件が無いとアカウントが閉じる**。流入が無いうちに申請するのは
自分で締切を起動するだけの行為なので、UNKNOWN のあいだは勧めない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from .config import Config
from .store import Store

#: 解析タグの検出に使う断片。ここに無いものは検出できない（＝UNKNOWNになる）。
_ANALYTICS_HINTS = (
    "googletagmanager.com",
    "google-analytics.com",
    "static.cloudflareinsights.com",
    "gc.zgo.at",  # GoatCounter
    "plausible.io",
    "umami",
)

READY = "READY"
NOT_YET = "NOT_YET"
UNKNOWN = "UNKNOWN"
BLOCKED = "BLOCKED"


@dataclass
class Check:
    name: str
    state: str
    detail: str


@dataclass
class Readiness:
    days_live: int
    update_days: int
    items_open: int
    items_affiliatable: int
    analytics: bool
    contact_ok: bool
    checks: list[Check]

    @property
    def blocked(self) -> bool:
        return any(c.state == BLOCKED for c in self.checks)


def measure(cfg: Config, store: Store) -> Readiness:
    rows = store.db.execute(
        "SELECT MIN(at) AS first, COUNT(DISTINCT substr(at,1,10)) AS days "
        "FROM crawls WHERE ok=1"
    ).fetchone()

    first_at = rows["first"]
    if first_at:
        started = datetime.fromisoformat(first_at).date()
        days_live = (date.today() - started).days
    else:
        days_live = 0
    update_days = rows["days"] or 0

    items_open = len(store.open_items())
    items_affiliatable = len(store.recently_closed(limit=10_000))

    index = cfg.site_dir / "index.html"
    html = index.read_text(encoding="utf-8") if index.exists() else ""
    analytics = any(h in html for h in _ANALYTICS_HINTS)
    contact_ok = "CHANGEME" not in cfg.contact_url and cfg.contact_url.startswith("http")

    checks: list[Check] = []

    if contact_ok:
        checks.append(Check("申し込み導線", READY, cfg.contact_url))
    else:
        checks.append(
            Check(
                "申し込み導線",
                BLOCKED,
                "contact_url が未設定。CTAが死にリンクのサイトは審査で落ちるし、"
                "通ったとしても客を取りこぼす。ここが直るまで申請しない",
            )
        )

    if items_open + items_affiliatable >= 100:
        checks.append(
            Check(
                "掲載量",
                READY,
                f"公開 {items_open}件 / 終了 {items_affiliatable}件",
            )
        )
    else:
        checks.append(
            Check(
                "掲載量",
                NOT_YET,
                f"合計 {items_open + items_affiliatable}件。"
                "審査は「中身のあるサイト」を見るので100件は欲しい",
            )
        )

    if update_days >= 14:
        checks.append(Check("更新の継続", READY, f"{update_days}日ぶんの更新履歴"))
    else:
        checks.append(
            Check(
                "更新の継続",
                NOT_YET,
                f"{update_days}日ぶん。放置サイトでないことを示すのに2週間は要る",
            )
        )

    if analytics:
        checks.append(Check("流入の把握", READY, "解析タグを検出"))
    else:
        checks.append(
            Check(
                "流入の把握",
                UNKNOWN,
                "解析タグが無いので流入数は測れない。"
                "Amazon は承認後180日以内に適格販売3件が無いと閉じるので、"
                "流入が見えないまま申請するのは締切を自分で起動するだけ",
            )
        )

    return Readiness(
        days_live=days_live,
        update_days=update_days,
        items_open=items_open,
        items_affiliatable=items_affiliatable,
        analytics=analytics,
        contact_ok=contact_ok,
        checks=checks,
    )


def verdict_ebay(r: Readiness) -> tuple[str, str]:
    """eBay Partner Network の申請可否。

    EPN は流入ゼロでも申請自体は通ることがあり、承認後の締切も無い。
    よって「中身があること」と「導線が生きていること」で判断できる。
    """
    if not r.contact_ok:
        return BLOCKED, "contact_url を直すのが先"
    if r.items_open + r.items_affiliatable < 100:
        return NOT_YET, "掲載量が足りない"
    if r.update_days < 14:
        return NOT_YET, f"更新履歴が {r.update_days}日ぶん。14日欲しい"
    return READY, "申請してよい"


def verdict_amazon(r: Readiness) -> tuple[str, str]:
    """Amazon アソシエイト（US）の申請可否。

    EPN と違い**承認後180日の締切がある**ので、基準を厳しくしている。
    流入が測れないうちは READY を返さない。
    """
    ok, why = verdict_ebay(r)
    if ok != READY:
        return ok, why
    if not r.analytics:
        return UNKNOWN, "解析タグが無く流入が測れない。180日の締切を起動すべきでない"
    return READY, "申請してよい（流入の実数を確認してから）"


def report(cfg: Config, store: Store) -> str:
    r = measure(cfg, store)
    lines = [
        f"サイト稼働 {r.days_live}日 / 更新 {r.update_days}日ぶん / "
        f"公開 {r.items_open}件・終了 {r.items_affiliatable}件",
        "",
    ]
    for c in r.checks:
        mark = {READY: "OK", NOT_YET: "--", UNKNOWN: "??", BLOCKED: "!!"}[c.state]
        lines.append(f"  [{mark}] {c.name}: {c.detail}")

    lines.append("")
    for label, (state, why) in (
        ("eBay Partner Network", verdict_ebay(r)),
        ("Amazon アソシエイト(US)", verdict_amazon(r)),
    ):
        lines.append(f"  {label}: {state} — {why}")
    return "\n".join(lines)
