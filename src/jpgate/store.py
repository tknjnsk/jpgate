"""SQLite の正本。

Web ページや通知は表示先であって保存先ではない。壊れたらこのDBから作り直す。

## 通知を出してよい条件（ここが事故の防波堤）

1. `result.ok` が False の走査は**一切反映しない**。落ちた走査を反映すると、
   取れなかった商品が「消えた」ように見える。
2. そのショップの**初回成功走査は seed 扱いで、イベントを1件も出さない**。
   でないと初回に数万件の「予約開始」を撒くことになる。
3. **イベントは「在ること」からしか作らない。「無いこと」からは作らない。**
   一覧の先頭数ページしか見ないので、載っていない＝終了ではない。
   この規則があるので、走査範囲を変えても誤通知が増えない。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    DISPLAY_STATUSES,
    EVENT_DEADLINE,
    EVENT_LOTTERY_OPEN,
    EVENT_RESERVATION_OPEN,
    EVENT_NEW_LISTING,
    EVENT_RESTOCK,
    OPEN_STATUSES,
    SHUT_STATUSES,
    STATUS_LISTED,
    STATUS_LOTTERY,
    STATUS_RESERVATION,
    CrawlResult,
    Event,
    Item,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    shop        TEXT NOT NULL,
    at          TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    item_count  INTEGER NOT NULL,
    pages       INTEGER NOT NULL,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS items (
    source       TEXT NOT NULL,
    item_id      TEXT NOT NULL,
    shop         TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    price_jpy    INTEGER,
    image        TEXT,
    summary      TEXT,
    icons        TEXT NOT NULL,
    ship_month   TEXT,
    status       TEXT NOT NULL,
    deadline     INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    shop         TEXT NOT NULL,
    item_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    at           TEXT NOT NULL,
    notified_at  TEXT
);

-- X の投稿下書きを Discord に送った記録。
--
-- 掲載中の商品は毎時の走査で何度でも出てくるので、これが無いと同じ文面が
-- 1時間ごとに飛び、真っ先にミュートされる。**商品ごとに一度だけ**送る。
-- events と分けているのは、通知が「状態が変わった瞬間」なのに対して
-- こちらは「まだ貼っていない商品」という別の軸だから。
CREATE TABLE IF NOT EXISTS x_posts_sent (
    source   TEXT NOT NULL,
    item_id  TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);

-- 動画に入れた商品の記録。x_posts_sent と同じ理由（毎回同じ商品で動画を
-- 作ると、同じ5件が並んだ動画を延々と出すことになる）。
-- x_posts_sent と分けているのは媒体が別だから。X に貼った商品を動画に
-- 入れてはいけない理由は無く、共有すると片方の消費でもう片方が痩せる。
CREATE TABLE IF NOT EXISTS clip_items_used (
    source   TEXT NOT NULL,
    item_id  TEXT NOT NULL,
    used_at  TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);

CREATE INDEX IF NOT EXISTS idx_events_pending ON events (notified_at, at);
CREATE INDEX IF NOT EXISTS idx_items_status ON items (status, ship_month);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------
    # 走査の反映
    # ------------------------------------------------------------------
    def has_successful_crawl(self, source: str, shop: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM crawls WHERE source=? AND shop=? AND ok=1 LIMIT 1",
            (source, shop),
        ).fetchone()
        return row is not None

    def apply(self, result: CrawlResult) -> list[Event]:
        """走査を反映し、生成したイベントを返す。

        `ok=False` なら走査ログだけ残して**何も反映しない**（空を
        「全部消えた」と読まないため）。
        """
        at = _now()
        # seed 判定は走査ログを書く**前**に取る。後に回すと、いま挿入した
        # 自分自身を「過去の成功」として数えてしまい、初回走査が seed に
        # ならず全件を新着として通知する。
        seeding = not self.has_successful_crawl(result.source, result.shop)

        self.db.execute(
            "INSERT INTO crawls (source, shop, at, ok, item_count, pages, error) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                result.source,
                result.shop,
                at,
                int(result.ok),
                len(result.items),
                result.pages_fetched,
                result.error,
            ),
        )
        if not result.ok:
            self.db.commit()
            return []

        events: list[Event] = []

        for item in result.items:
            prev = self.db.execute(
                "SELECT status, deadline FROM items WHERE source=? AND item_id=?",
                (item.source, item.item_id),
            ).fetchone()
            prev_status = prev["status"] if prev else None
            prev_deadline = bool(prev["deadline"]) if prev else False

            if not seeding:
                events.extend(_events_for(item, prev_status, prev_deadline))

            self._upsert(item, at, first_time=prev is None)

        for ev in events:
            self.db.execute(
                "INSERT INTO events (source, shop, item_id, kind, from_status, to_status, at) "
                "VALUES (?,?,?,?,?,?,?)",
                (ev.source, ev.shop, ev.item_id, ev.kind, ev.from_status, ev.to_status, at),
            )

        self.db.commit()
        return events

    def _upsert(self, item: Item, at: str, *, first_time: bool) -> None:
        self.db.execute(
            """
            INSERT INTO items (source, item_id, shop, title, url, price_jpy, image,
                               summary, icons, ship_month, status, deadline,
                               first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (source, item_id) DO UPDATE SET
                shop=excluded.shop, title=excluded.title, url=excluded.url,
                price_jpy=excluded.price_jpy, image=excluded.image,
                summary=excluded.summary, icons=excluded.icons,
                ship_month=excluded.ship_month, status=excluded.status,
                deadline=excluded.deadline, last_seen=excluded.last_seen
            """,
            (
                item.source,
                item.item_id,
                item.shop,
                item.title,
                item.url,
                item.price_jpy,
                item.image,
                item.summary,
                json.dumps(list(item.icons)),
                item.ship_month,
                item.status,
                int(item.deadline_soon),
                at,
                at,
            ),
        )

    # ------------------------------------------------------------------
    # 参照
    # ------------------------------------------------------------------
    def pending_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT e.*, i.title, i.url, i.price_jpy, i.image, i.summary,
                   i.icons, i.ship_month
            FROM events e JOIN items i
              ON i.source = e.source AND i.item_id = e.item_id
            WHERE e.notified_at IS NULL
            ORDER BY e.at ASC, e.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def mark_notified(self, event_ids: list[int]) -> None:
        at = _now()
        self.db.executemany(
            "UPDATE events SET notified_at=? WHERE id=?", [(at, i) for i in event_ids]
        )
        self.db.commit()

    def x_sent_keys(self) -> set[tuple[str, str]]:
        """既に X の下書きを送った商品の (source, item_id)。"""
        return {
            (r["source"], r["item_id"])
            for r in self.db.execute("SELECT source, item_id FROM x_posts_sent")
        }

    def x_sent_today(self) -> int:
        """今日すでに送った X 下書きの数。

        1回あたりの上限だけでは1日の総量が縛れない（毎時走らせれば24倍になる）。
        投稿の総量を決めるのは「1日に何回 X に貼るか」なので、そちらで数える。
        """
        return self.db.execute(
            "SELECT COUNT(*) FROM x_posts_sent WHERE substr(sent_at, 1, 10) = ?",
            (_now()[:10],),
        ).fetchone()[0]

    def last_x_sent_title(self) -> str | None:
        """直近に X 下書きを送った商品のタイトル。無ければ None。

        カテゴリを直接持たないのは、分類規則(`lines.yaml`)を変えたときに
        過去の記録と食い違うため。**タイトルは事実、カテゴリは解釈**なので、
        事実のほうを保存して毎回引き直す。
        """
        row = self.db.execute(
            "SELECT i.title FROM x_posts_sent x JOIN items i"
            "  ON i.source = x.source AND i.item_id = x.item_id"
            " ORDER BY x.sent_at DESC LIMIT 1"
        ).fetchone()
        return row["title"] if row else None

    def mark_x_sent(self, keys: list[tuple[str, str]]) -> None:
        """送信済みにする. **送信が成功してから呼ぶこと**.

        先に記録すると、Discord が落ちたときにその商品が二度と出てこなくなる。
        逆順(送ってから記録)なら最悪もう一度届くだけで、失うものが無い。
        """
        at = _now()
        self.db.executemany(
            "INSERT OR IGNORE INTO x_posts_sent (source, item_id, sent_at) "
            "VALUES (?, ?, ?)",
            [(s, i, at) for s, i in keys],
        )
        self.db.commit()

    def clip_used_keys(self) -> set[tuple[str, str]]:
        """既に動画に入れた商品の (source, item_id)。"""
        return {
            (r["source"], r["item_id"])
            for r in self.db.execute("SELECT source, item_id FROM clip_items_used")
        }

    def mark_clip_used(self, keys: list[tuple[str, str]]) -> None:
        """動画に入れた商品を記録する. **送信が成功してから呼ぶこと**.

        mark_x_sent と同じ順序の理由。先に記録すると、送信に失敗したときに
        その商品が二度と動画に出てこなくなる。
        """
        at = _now()
        self.db.executemany(
            "INSERT OR IGNORE INTO clip_items_used (source, item_id, used_at) "
            "VALUES (?, ?, ?)",
            [(s, i) + (at,) for s, i in keys],
        )
        self.db.commit()

    def seed_at(self) -> dict[tuple[str, str], str]:
        """ショップごとの初回成功走査の時刻。

        `items.first_seen` は「最初に観測した時刻」であって「販売が始まった
        時刻」ではない。初回走査（seed）で入った商品は、何ヶ月も前から並んで
        いたものが全部その日の first_seen を持つ。これを開始日として出すと、
        **数百件が同じ日に一斉に始まったという嘘**になる。

        seed より後に現れた商品に限れば、first_seen は開始日と1時間以内で
        一致する（毎時走査しているため）。その判定に使う。
        """
        return {
            (r["source"], r["shop"]): r["at"]
            for r in self.db.execute(
                "SELECT source, shop, MIN(at) AS at FROM crawls "
                "WHERE ok = 1 GROUP BY source, shop"
            )
        }

    def open_items(self) -> list[sqlite3.Row]:
        """Web ページ用。今なら申し込める／買えるもの。"""
        marks = ",".join("?" for _ in DISPLAY_STATUSES)
        return self.db.execute(
            f"SELECT * FROM items WHERE status IN ({marks}) "
            "ORDER BY (ship_month IS NULL), ship_month, last_seen DESC",
            tuple(sorted(DISPLAY_STATUSES)),
        ).fetchall()

    def recently_closed(self, limit: int = 60) -> list[sqlite3.Row]:
        """終了/売り切れになった商品。

        「買えなかった」ページに価値がある。海外の客に残る経路は二次流通しか
        無いので、ここがアフィリエイトの受け皿になる。

        直近に観測したものだけを出す。古いものを載せ続けると、二次流通でも
        もう流通していない商品を並べることになる。
        """
        marks = ",".join("?" for _ in SHUT_STATUSES)
        return self.db.execute(
            f"SELECT * FROM items WHERE status IN ({marks}) "
            "ORDER BY last_seen DESC, first_seen DESC LIMIT ?",
            (*sorted(SHUT_STATUSES), limit),
        ).fetchall()

    def last_crawls(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT source, shop, MAX(at) AS at, ok, item_count, error
            FROM crawls GROUP BY source, shop ORDER BY source, shop
            """
        ).fetchall()


def _events_for(item: Item, prev_status: str | None, prev_deadline: bool) -> list[Event]:
    """1商品ぶんの遷移をイベントに落とす。

    `prev_status is None` は「初めて見た」。seed 済みショップで初見なら、
    それは本当に新しく載った商品なので通知対象になる。
    """
    events: list[Event] = []
    now = item.status

    def add(kind: str) -> None:
        events.append(
            Event(
                source=item.source,
                shop=item.shop,
                item_id=item.item_id,
                kind=kind,
                from_status=prev_status,
                to_status=now,
            )
        )

    if prev_status is None:
        if now == STATUS_LISTED:
            # 在庫状態が観測できないソース。初見＝掲載が現れた、しか言えない。
            add(EVENT_NEW_LISTING)
        elif now == STATUS_LOTTERY:
            add(EVENT_LOTTERY_OPEN)
        elif now == STATUS_RESERVATION:
            add(EVENT_RESERVATION_OPEN)
        # ON_SALE の初見は通知しない。既に売っていたものが一覧の見える範囲に
        # 入ってきただけのことが多く、客にとって新情報ではない。
    elif prev_status != now:
        # 再販を先に見る。「予約終了 → 抽選受付中」は抽選の再実施であって、
        # 客にとっては初回開始より価値の高い情報。開始として出すと
        # 「もう一度チャンスが来た」という一番効く事実が消える。
        if prev_status in SHUT_STATUSES and now in OPEN_STATUSES:
            add(EVENT_RESTOCK)
        elif now == STATUS_LOTTERY:
            add(EVENT_LOTTERY_OPEN)
        elif now == STATUS_RESERVATION:
            add(EVENT_RESERVATION_OPEN)

    if item.deadline_soon and not prev_deadline:
        events.append(
            Event(
                source=item.source,
                shop=item.shop,
                item_id=item.item_id,
                kind=EVENT_DEADLINE,
                from_status=prev_status,
                to_status=now,
            )
        )

    return events
