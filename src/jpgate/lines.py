"""商品ラインの分類。

発送月でグループ化しても、探している人には役に立たない。S.H.Figuarts を
集めている人は「12月発送」では探さず、ラインで探す。発送月は買うと
決めたあとに要る情報なので、絞り込みの軸はラインが正しい。

## 照合の規則（どちらも実データで踏んでから入れた）

- **NFKC 正規化してから当てる。** しないと全角の `ＨＧ` が `HG` に
  当たらず、285件中116件(41%)が未分類になった。
- **パターンは長い順。** しないと `MG ` が `MGEX` `MGSD` を食う。
  KujiRadar で『ポケモンカードゲーム』が『ゲーム』に誤爆したのと同じ構図。

どのパターンにも当たらなければ `Other`。**推測で寄せない**（間違った
カテゴリに入れるくらいなら Other のほうが害が小さい）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

OTHER = "Other"


@dataclass(frozen=True)
class Verdict:
    category: str
    line: str | None
    hashtag: str | None

    @property
    def known(self) -> bool:
        return self.line is not None


class Classifier:
    def __init__(self, raw: dict):
        # (パターン, カテゴリ, ライン) を長い順に平坦化しておく。
        # カテゴリごとに探すと、カテゴリをまたいだ長さ優先が効かない。
        flat: list[tuple[str, str, str]] = []
        self._hashtags: dict[str, str] = {}
        for category, body in raw.items():
            self._hashtags[category] = body.get("hashtag", "")
            for line, patterns in (body.get("lines") or {}).items():
                for pattern in patterns:
                    flat.append((pattern, category, line))
        self._patterns = sorted(flat, key=lambda t: len(t[0]), reverse=True)

    @classmethod
    def load(cls, path: Path | str) -> "Classifier":
        path = Path(path)
        if not path.exists():
            return cls({})
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    @property
    def categories(self) -> list[str]:
        return list(self._hashtags)

    def classify(self, title: str) -> Verdict:
        norm = unicodedata.normalize("NFKC", title).lower()
        for pattern, category, line in self._patterns:
            if unicodedata.normalize("NFKC", pattern).lower() in norm:
                return Verdict(category, line, self._hashtags.get(category) or None)
        return Verdict(OTHER, None, None)
