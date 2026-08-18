"""JPGate CLI。

    jpgate doctor    各経路が生きているか検査する（スクレイパーは静かに嘘を返すので必須）
    jpgate scan      走査してイベントをDBに貯める
    jpgate notify    未通知イベントを Discord に流す
    jpgate publish   docs/index.html と data/x_queue.txt を作り直す
    jpgate xqueue    未送信の X 下書きを Discord へ（既定は dry-run）
    jpgate readiness アフィリエイト審査に出せる状態か判定する
    jpgate run       scan → notify → publish → xqueue（定期実行用）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from . import config as config_mod
from . import readiness
from .config import Config
from .gates import evaluate
from .lines import Classifier
from .models import Item
from .notify import build_embed, post, post_text
from .publish import build_x_posts, render_site, render_x_posts, write
from .sources import pbandai, pokecen
from .store import Store
from .translate import Glossary

#: このソース名のときにどのクローラを使うか。
_CRAWLERS = {
    "p-bandai": pbandai.crawl_shop,
    "pokemon-center": pokecen.crawl_shop,
}

#: ゲート宣言の確認日がこれより古いと doctor が警告する。
#: サイトのポリシーは黙って変わるので、宣言を放置しないための仕掛け。
_GATE_STALE_DAYS = 180


def _fix_console() -> None:
    """cp932 コンソールで日本語の一部が落ちるのを防ぐ。

    encoding は変えない（変えると別の出力先で壊れる）。表示できない文字を
    置換するだけ。KujiRadar で走査完了直後に落ちた事故と同じ対策。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def _gates_by_source(cfg: Config) -> dict[str, list]:
    return {s.name: s.gates for s in cfg.sources}


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    failures = 0

    print("[1] 設定")
    print(f"    sources={len(cfg.sources)} db={cfg.db_path}")
    if "CHANGEME" in cfg.contact_url:
        print("    ! contact_url が既定のままです。通知のCTAが死にリンクになります")
        failures += 1

    print("[2] ゲート宣言の鮮度")
    for src in cfg.sources:
        if not src.gates:
            print(f"    - {src.name}: ソース宣言なし（抽選アイコンのある商品のみ売れる）")
        for gate in src.gates:
            try:
                age = (date.today() - date.fromisoformat(gate.verified_on)).days
            except ValueError:
                print(f"    ! {src.name}/{gate.key}: verified_on が日付として読めません")
                failures += 1
                continue
            mark = "!" if age > _GATE_STALE_DAYS else "-"
            print(f"    {mark} {src.name}/{gate.key}: 確認から {age} 日")
            if age > _GATE_STALE_DAYS:
                print("      サイトのポリシーを再確認してください")
                failures += 1

    print("[3] 一覧ページの疎通とパース")
    for src in cfg.sources:
        crawler = _CRAWLERS.get(src.name)
        if crawler is None:
            print(f"    ! {src.name}: 対応するクローラがありません")
            failures += 1
            continue
        for shop in src.shops:
            result = crawler(
                shop.shop,
                max_pages=1,
                per_page=src.per_page,
                delay_sec=src.delay_sec,
                timeout=src.timeout,
            )
            if not result.ok:
                print(f"    ! {src.name}/{shop.shop}: {result.error}")
                failures += 1
                continue
            statuses = {}
            for item in result.items:
                statuses[item.status] = statuses.get(item.status, 0) + 1
            print(f"    - {src.name}/{shop.shop}: {len(result.items)}件 {statuses}")
            if result.unknown_icons:
                # 未知アイコンは「新しい状態を状態なしと読んでいる」可能性。
                print(f"      ! 未知のアイコン: {sorted(result.unknown_icons)}")
                failures += 1

    print("[4] 用語集")
    glossary = Glossary.load(cfg.glossary_path)
    print(f"    - {cfg.glossary_path.name}: {len(glossary._pairs)} 語")

    print("[5] Discord Webhook")
    if cfg.discord_webhook:
        print("    - JPGATE_DISCORD_WEBHOOK 設定済み（送信テストは notify --dry-run=false で）")
    else:
        print("    ! JPGATE_DISCORD_WEBHOOK が未設定。通知は出ません")
        failures += 1

    print()
    print("doctor: OK" if failures == 0 else f"doctor: {failures} 件の問題")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------
def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    total_events = 0
    failed = 0
    try:
        for src in cfg.sources:
            crawler = _CRAWLERS.get(src.name)
            if crawler is None:
                print(f"! {src.name}: クローラ未対応。飛ばします")
                failed += 1
                continue
            for shop in src.shops:
                seeding = not store.has_successful_crawl(src.name, shop.shop)
                result = crawler(
                    shop.shop,
                    max_pages=args.max_pages or shop.max_pages,
                    per_page=src.per_page,
                    delay_sec=src.delay_sec,
                    timeout=src.timeout,
                )
                events = store.apply(result)
                if not result.ok:
                    print(f"! {src.name}/{shop.shop}: 反映せず — {result.error}")
                    failed += 1
                    continue
                note = " (初回=seed、通知なし)" if seeding else ""
                print(
                    f"- {src.name}/{shop.shop}: {len(result.items)}件 "
                    f"→ イベント {len(events)}件{note}"
                )
                if result.unknown_icons:
                    print(f"  ! 未知のアイコン: {sorted(result.unknown_icons)}")
                total_events += len(events)
        print(f"\nイベント計 {total_events} 件、失敗 {failed} ショップ")
    finally:
        store.close()
    return 1 if failed else 0


# --------------------------------------------------------------------------
# notify
# --------------------------------------------------------------------------
def cmd_notify(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    glossary = Glossary.load(cfg.glossary_path)
    gates = _gates_by_source(cfg)
    try:
        rows = store.pending_events(limit=cfg.max_notify_per_run)
        if not rows:
            print("未通知イベントなし")
            return 0

        embeds, ids = [], []
        for row in rows:
            item = Item(
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
            verdict = evaluate(item, gates.get(row["source"], []))
            embeds.append(build_embed(row, verdict, glossary, cfg))
            ids.append(row["id"])

        if args.dry_run:
            for embed in embeds:
                print(f"--- {embed['title']}")
                print(embed["description"])
            print(f"\n(dry-run: {len(embeds)} 件。送信も既読化もしていません)")
            return 0

        if not cfg.discord_webhook:
            print("! JPGATE_DISCORD_WEBHOOK が未設定。送信できません")
            return 1

        post(cfg.discord_webhook, embeds)
        # 送信が成功してから既読にする。逆にすると落ちたときに黙って消える。
        store.mark_notified(ids)
        print(f"{len(embeds)} 件送信")
    finally:
        store.close()
    return 0


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------
def cmd_publish(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    glossary = Glossary.load(cfg.glossary_path)
    gates = _gates_by_source(cfg)
    try:
        rows = store.open_items()
        if not rows:
            # ここで空ページを書くと、走査が壊れているときに
            # 「在庫ゼロ」に見える公開ページを自分で作ることになる。
            print("! 掲載できる商品が0件。ページを書き換えずに終了します")
            return 1
        closed = store.recently_closed(limit=cfg.closed_limit)
        classifier = Classifier.load(cfg.lines_path)
        index, queue = write(
            cfg,
            render_site(rows, gates, glossary, cfg, closed_rows=closed, classifier=classifier),
            render_x_posts(rows, gates, glossary, cfg, classifier=classifier),
        )
        print(f"- {index} (掲載 {len(rows)}件 / 終了 {len(closed)}件)")
        print(f"- {queue}")
        if not cfg.affiliate.any_enabled:
            print("  - アフィリエイトID未設定のためリンクは生成していません")
    finally:
        store.close()
    return 0


def cmd_readiness(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    try:
        print(readiness.report(cfg, store))
    finally:
        store.close()
    return 0


def cmd_xqueue(cfg: Config, args: argparse.Namespace) -> int:
    """まだ送っていない X 下書きを Discord へ流す（コピペ用）。

    掲載中の商品は毎時の走査で何度でも出てくるので、**商品ごとに一度だけ**
    送る。既読は DB に持つ（x_posts_sent）。
    """
    store = Store(cfg.db_path)
    glossary = Glossary.load(cfg.glossary_path)
    gates = _gates_by_source(cfg)
    try:
        rows = store.open_items()
        if not rows:
            # publish と同じ理由。走査が壊れているときに「何も無い」と
            # 振る舞うと、静かに止まったことに気づけない。
            print("! 掲載できる商品が0件。走査が壊れている可能性があります")
            return 1

        posts = build_x_posts(
            rows,
            gates,
            glossary,
            cfg,
            limit=cfg.max_x_posts_per_run,
            classifier=Classifier.load(cfg.lines_path),
            exclude=store.x_sent_keys(),
        )
        if not posts:
            print("未送信の下書きなし")
            return 0

        if args.dry_run:
            for p in posts:
                print("---")
                print(p.text)
            print(f"\n(dry-run: {len(posts)} 件。送信も既読化もしていません)")
            return 0

        if not cfg.x_queue_webhook:
            print(
                "! JPGATE_X_QUEUE_WEBHOOK が未設定。送信できません\n"
                "  #drops とは別のチャンネルの webhook を設定してください"
            )
            return 1

        post_text(cfg.x_queue_webhook, [p.text for p in posts])
        # 送信が成功してから既読にする。逆にすると落ちたときに黙って消える。
        store.mark_x_sent([p.key for p in posts])
        print(f"{len(posts)} 件を Discord へ送信しました")
    finally:
        store.close()
    return 0


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    rc = cmd_scan(cfg, args)
    cmd_notify(cfg, args)
    cmd_publish(cfg, args)
    cmd_xqueue(cfg, args)
    return rc


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    # 共通オプションはトップレベルとサブコマンドの両方に付ける。
    # トップレベルだけに置くと `jpgate notify --no-dry-run` が
    # 「unrecognized arguments」で落ちる（argparse はサブコマンドより前の
    # 位置しか見ない）。自然な語順で書けないCLIは、定期実行の設定を
    # 書くときに必ず間違える。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None)
    common.add_argument("--max-pages", type=int, default=None)
    common.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="notify: 既定は送信しない。実際に送るには --no-dry-run",
    )

    parser = argparse.ArgumentParser(prog="jpgate", description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (
        ("doctor", cmd_doctor),
        ("scan", cmd_scan),
        ("notify", cmd_notify),
        ("publish", cmd_publish),
        ("xqueue", cmd_xqueue),
        ("readiness", cmd_readiness),
        ("run", cmd_run),
    ):
        sub.add_parser(name, parents=[common]).set_defaults(func=fn)

    args = parser.parse_args(argv)
    cfg = config_mod.load(args.config)
    return args.func(cfg, args)
