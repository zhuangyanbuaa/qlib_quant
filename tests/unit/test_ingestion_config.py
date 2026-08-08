from pathlib import Path

import pytest
from pydantic import ValidationError

from quant_system.ingestion.config import load_news_source_settings, load_price_source_settings


def test_repository_price_source_config_is_valid() -> None:
    settings = load_price_source_settings(Path("configs/sources/prices.yaml"))

    assert settings.provider == "yahoo_finance"
    assert settings.core_symbols == ("SPY", "QQQ")
    assert settings.to_update_config().batch_size == 25


def test_unknown_config_keys_fail_fast(tmp_path) -> None:
    config = tmp_path / "prices.yaml"
    config.write_text(
        "provider: yahoo_finance\nbootstrap_start: 2021-01-01\nmystery: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="mystery"):
        load_price_source_settings(config)


def test_news_source_config_includes_finbert_runtime_guards() -> None:
    settings = load_news_source_settings(Path("configs/sources/news.yaml"))

    assert settings.sentiment.finbert.cache_enabled is True
    assert settings.sentiment.finbert.inference_timeout_seconds == 20
