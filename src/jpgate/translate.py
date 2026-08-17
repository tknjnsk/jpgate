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
from pathlib import Path

import yaml

#: 用語集に無くても英字はそのまま通るので、全体が英字なら訳出済みとみなす。
_RE_JA = re.compile(r"[぀-ヿ一-鿿]")


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
        out = title
        for ja, en in self._pairs:
            out = out.replace(ja, en)
        return re.sub(r"\s{2,}", " ", out).strip()

    def coverage(self, title: str) -> float:
        """日本語文字がどれだけ消えたか。0.0〜1.0。

        Discord に出すときに、訳が薄いものへ注記を付けるために使う。
        """
        before = len(_RE_JA.findall(title))
        if before == 0:
            return 1.0
        after = len(_RE_JA.findall(self.render(title)))
        return 1.0 - (after / before)
