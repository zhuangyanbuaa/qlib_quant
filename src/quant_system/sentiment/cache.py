"""Persistent article-level sentiment cache."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from quant_system.sentiment.classifier import SentimentResult


@dataclass(frozen=True)
class CachedSentiment:
    """A cached model sentiment result."""

    label: str
    score: float
    model_key: str
    article_key: str
    created_at_utc: str

    def to_result(self) -> SentimentResult:
        return SentimentResult(label=self.label, score=self.score)


class SentimentCache:
    """Small file-backed cache keyed by article and model version."""

    def __init__(self, cache_directory: Path) -> None:
        self.cache_directory = cache_directory.resolve()

    def get(self, *, article_key: str, model_key: str) -> CachedSentiment | None:
        path = self._path(article_key=article_key, model_key=model_key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return CachedSentiment(
                label=str(payload["label"]),
                score=float(payload["score"]),
                model_key=str(payload["model_key"]),
                article_key=str(payload["article_key"]),
                created_at_utc=str(payload["created_at_utc"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(
        self,
        *,
        article_key: str,
        model_key: str,
        result: SentimentResult,
    ) -> Path:
        path = self._path(article_key=article_key, model_key=model_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = CachedSentiment(
            label=result.label,
            score=result.score,
            model_key=model_key,
            article_key=article_key,
            created_at_utc=datetime.now(UTC).isoformat(),
        )
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(record), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def _path(self, *, article_key: str, model_key: str) -> Path:
        digest = sha256(f"{model_key}|{article_key}".encode()).hexdigest()
        return self.cache_directory / digest[:2] / f"{digest}.json"
