"""Discord 通知（英語）。

通知1件の狙いは「この商品が始まった」ではなく
**「始まった。そして君にはこの関門がある」**を同時に伝えること。
関門バッジが営業そのものなので、ゲートが UNKNOWN の商品には CTA を出さない。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
import urllib.error
import urllib.request
from pathlib import Path

from .config import Config
from .gates import GATE_DEFS, GateVerdict, badges_en
from .models import (
    ICON_LOT_SALES,
    STATUS_LISTED,
    EVENT_DEADLINE,
    EVENT_NEW_LISTING,
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
    EVENT_NEW_LISTING: ("🆕 Just listed", 0x9B59B6),
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
        # 抽選と通常販売で提供する行為が違う。予約商品に "enter"(抽選に応募する)
        # と書くのは単に誤り。関門の種類に合わせて動詞を変える。
        lottery = ICON_LOT_SALES in tuple(json.loads(row["icons"]))
        if lottery:
            offer = "can enter the lottery for you"
        elif row["to_status"] == STATUS_LISTED:
            # 在庫が観測できないソース。「注文できる」と言い切ると、
            # 売り切れていたときにこちらが嘘をついたことになる。
            offer = "can check stock and order it for you"
        else:
            offer = "can order it and forward it to you"

        lines.append(f"**We are in Japan and {offer} → {cfg.contact_url}**")
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


#: Discord は User-Agent の無いリクエストを 403 で弾く
#: （urllib の既定 `Python-urllib/3.x` が該当。実測で踏んだ）。
_UA = "JPGate/0.1 (+https://github.com/tknjnsk/jpgate)"


#: Discord の1メッセージあたりの content 上限。
_CONTENT_LIMIT = 2000


def _send(webhook: str, payload: dict, timeout: int) -> None:
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": _UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise urllib.error.HTTPError(
                webhook, resp.status, "discord rejected", resp.headers, None
            )


def code_block(content: str) -> str:
    """コピペ用に整形する.

    素のテキストや embed ではなくコードブロックにするのは**コピペのため**。
    embed の本文は Discord のUIから選択しづらく、装飾記号も一緒に拾ってしまう。
    コードブロックならモバイルでもコピーボタンが出る。X の下書きも動画の
    説明文も、行き先（X / TikTok）に貼るための文なので扱いは同じ。

    投稿文にバッククォートは入らない設計だが、将来商品名に混ざるとコード
    ブロックが割れて残りが Markdown として解釈される。閉じ記号と衝突する
    3連バッククォートだけ潰しておく。
    """
    return f"```\n{content.replace('```', '` ` `')}\n```"


def post_text(webhook: str, contents: list[str], timeout: int = 30) -> None:
    """素のテキストを1件1メッセージで投げる。"""
    for content in contents:
        body = code_block(content)
        if len(body) > _CONTENT_LIMIT:
            # X の投稿は280字なので通常あり得ない。起きたら黙って切るより
            # 送らないほうがよい(切れた文面を貼られるのが最悪)。
            raise ValueError(f"Discord の2000字上限を超える下書き: {len(body)}字")
        _send(webhook, {"content": body}, timeout)


#: フォーラム投稿のタイトル上限。超えると Discord が拒否する。
_THREAD_NAME_LIMIT = 100
#: 1スレッドに付けられるタグ数の上限。
_APPLIED_TAGS_LIMIT = 5


def post_forum(
    webhook: str,
    embed: dict,
    thread_name: str,
    tag_ids: list[str] | None = None,
    timeout: int = 30,
) -> None:
    """フォーラムチャンネルへ**1件を1投稿として**投げる。

    通常のチャンネルと違い、まとめ送りをしない。1リクエストに embed を
    10件入れると「10商品が入った1つの投稿」になり、流れて消えないという
    フォーラムの利点が消えるため。

    呼び出し側は**1件送るごとに既読にする**こと。まとめて既読にすると、
    途中で落ちたときに送信済みのぶんが再送される。
    """
    name = thread_name.strip() or "Drop"
    payload: dict = {
        # Discord は空のタイトルを拒否する。切り詰めで空になる余地を潰す。
        "thread_name": name[:_THREAD_NAME_LIMIT],
        "embeds": [embed],
    }
    if tag_ids:
        payload["applied_tags"] = list(tag_ids)[:_APPLIED_TAGS_LIMIT]
    _send(webhook, payload, timeout)


def post(webhook: str, embeds: list[dict], timeout: int = 30) -> None:
    """Discord へ投げる。embed は1リクエスト10件が上限。"""
    for i in range(0, len(embeds), 10):
        payload = json.dumps({"embeds": embeds[i : i + 10]}).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": _UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                raise urllib.error.HTTPError(
                    webhook, resp.status, "discord rejected", resp.headers, None
                )


#: Discord の添付上限（ブースト無しのサーバー）。超えると 413 が返るだけで
#: 理由が分からないので、送る前に落とす。
FILE_LIMIT = 10 * 1024 * 1024


def post_file(
    webhook: str, path: Path, content: str = "", timeout: int = 120
) -> None:
    """ファイルを1件添付して投げる（動画の受け渡し用）。

    multipart を手で組んでいるのは、依存を増やさないため。Discord は
    `payload_json` と `files[0]` の2フィールドだけ見る。
    """
    data = path.read_bytes()
    if len(data) > FILE_LIMIT:
        raise ValueError(
            f"{path.name} は {len(data) / 1048576:.1f}MB あり、Discord の上限"
            f"（{FILE_LIMIT // 1048576}MB）を超えています"
        )
    if len(content) > _CONTENT_LIMIT:
        raise ValueError(f"Discord の2000字上限を超える本文: {len(content)}字")

    boundary = "----jpgate" + uuid.uuid4().hex
    crlf = "\r\n".encode()
    body = bytearray()
    body += b"--" + boundary.encode() + crlf
    body += b'Content-Disposition: form-data; name="payload_json"' + crlf
    body += b"Content-Type: application/json" + crlf + crlf
    body += json.dumps({"content": content}).encode("utf-8") + crlf
    body += b"--" + boundary.encode() + crlf
    body += (
        f'Content-Disposition: form-data; name="files[0]"; filename="{path.name}"'
    ).encode("utf-8") + crlf
    body += b"Content-Type: application/octet-stream" + crlf + crlf
    body += data + crlf
    body += b"--" + boundary.encode() + b"--" + crlf

    req = urllib.request.Request(
        webhook,
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": _UA,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise urllib.error.HTTPError(
                webhook, resp.status, "discord rejected", resp.headers, None
            )
