"""商品名の英語化。

海外コレクター向けなので英語で出す必要があるが、機械翻訳は使わない。
この界隈の商品名はブランド名・シリーズ名・キャラ名の連結で、翻訳すると
かえって検索できなくなる（`METAL BUILD` を「金属製ビルド」にする類の事故）。

やるのは**用語集による置換だけ**で、置換できなかった部分は日本語のまま残す。
中途半端でも原語が残っているほうが、海外コレクターには通じる。
訳せたふりをしないことが要件。用語集は `data/glossary.yaml` で育てる。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import yaml

#: 用語集に無くても英字はそのまま通るので、全体が英字なら訳出済みとみなす。
_RE_JA = re.compile(r"[぀-ヿ一-鿿]")
#: 商品名に頻出する「YYYY年M月」。NFKC 後（半角化後）に当てる。
_RE_YM = re.compile(r"(\d{4})年(\d{1,2})月")
#: 「3次」= 3回目の生産ロット。語順が変わるので用語集では扱えない。
_RE_BATCH = re.compile(r"(\d+)次")


class Glossary:
    def __init__(self, mapping: dict[str, str]):
        # 長いキーから当てる。「ガンダム」を先に当てると
        # 「機動戦士ガンダム」が壊れるため。
        self._pairs = sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True)

    @classmethod
    def load(cls, path: Path | str) -> "Glossary":
        path = Path(path)
        if not path.exists():
            return cls({})
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls({str(k): str(v) for k, v in raw.items()})

    def render(self, title: str) -> str:
        # 全角ラテン/数字を半角に潰してから当てる。プレミアムバンダイの
        # 商品名は `ＨＧ 1/144` `ＲＥ/100` のように型番が全角で入ることがあり、
        # そのままでは海外の検索に当たらない（`ＨＧ` では eBay で0件になる）。
        # NFKC は日本語部分を壊さないので、置換の前段に置いて安全。
        out = unicodedata.normalize("NFKC", title)
        # 「2026年11月」→「2026-11」。商品名に発送月が入る慣習があり、
        # 年月の漢字が残ると英語話者には日付として読めない。
        out = _RE_YM.sub(lambda m: f"{m.group(1)}-{int(m.group(2)):02d}", out)
        # 「3次」→「batch 3」。生産ロットの回次はコレクターが気にする情報で、
        # 落とせない。語順が入れ替わるので用語集では表現できない。
        out = _RE_BATCH.sub(lambda m: f"batch {m.group(1)}", out)

        # 日本語は語を空白で区切らないため、隣り合う語をそのまま置換すると
        # 英単語が連結する（`ジョニー・ライデン専用ゲルググ` →
        # `Johnny Ridden'sGelgoog`）。用語ごとに空白を足して回るのではなく、
        # 置換時に必ず空白で囲み、あとで余分を畳む。
        for ja, en in self._pairs:
            out = out.replace(ja, f" {en} ")

        out = re.sub(r"\s{2,}", " ", out)
        # `&` の前後を揃える。置換で英語になった語の隣に `&` が来ると
        # 片側にだけ空白が残る（`パッケージ&ゲームカード` →
        # `Package &ゲームカード`）。日本語のままなら詰まっていて読めるが、
        # 英語になった瞬間に語の切れ目が消える。
        out = re.sub(r"\s*&\s*", " & ", out)
        # 括弧と句読点の内側に入った空白を戻す（`【 Restock 】` を避ける）。
        out = re.sub(r"([【（\[(])\s+", r"\1", out)
        out = re.sub(r"\s+([】）\])：:,、])", r"\1", out)
        return out.strip()

    def coverage(self, title: str) -> float:
        """日本語文字がどれだけ消えたか。0.0〜1.0。

        Discord に出すときに、訳が薄いものへ注記を付けるために使う。
        """
        before = len(_RE_JA.findall(title))
        if before == 0:
            return 1.0
        after = len(_RE_JA.findall(self.render(title)))
        return 1.0 - (after / before)
