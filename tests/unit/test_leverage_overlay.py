import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import pandas as pd

from quant_system.decision.leverage import (
    render_leverage_overlay_prompt,
    run_leverage_overlay_universe_workflow,
    write_leverage_overlay_artifacts,
)
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.leverage_overlay import (
    evaluate_leverage_overlay,
    load_leverage_overlay_config,
    load_leverage_overlay_universe_config,
)
from quant_system.universe.config import load_watchlist_config


def _features(*, market_risk_on: bool = True) -> pd.DataFrame:
    dates = pd.to_datetime(
        [
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
            "2026-08-03",
            "2026-08-04",
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
        ]
    )
    rows = []
    mu_closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 105.0, 108.0]
    for dt, close in zip(dates, mu_closes, strict=True):
        rows.append(
            {
                "symbol": "MU",
                "session_date_ny": dt,
                "open": close - 1,
                "high": close + 2,
                "low": close - 3,
                "close": close,
                "volume": 1_000_000,
                "ma20": 104.0,
                "ma50": 100.0,
                "ma200": 90.0,
                "ma50_slope20": 0.04,
                "rolling_high20": 112.0,
                "drawdown_from_high20": 1 - close / 112.0,
                "avg_dollar_volume20": 100_000_000.0,
                "rsi14": 55.0,
                "atr20": 4.0,
                "adx14": 25.0,
                "return60": 0.20,
                "benchmark_return60": 0.08,
                "relative_return60": 0.12,
                "previous_close": 105.0 if dt.date() == date(2026, 8, 7) else close - 1,
                "previous_high": 110.0,
                "previous_ma5": 104.0,
                "feature_ready": True,
            }
        )
    for symbol, close, ma20, ma50, slope, relative in [
        ("QQQ", 500.0 if market_risk_on else 460.0, 480.0, 470.0, 0.02, 0.0),
        ("SOXX", 300.0, 290.0, 285.0, 0.03, 0.04),
    ]:
        rows.append(
            {
                "symbol": symbol,
                "session_date_ny": pd.Timestamp("2026-08-07"),
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": 1_000_000,
                "ma20": ma20,
                "ma50": ma50,
                "ma200": 400.0,
                "ma50_slope20": slope,
                "rolling_high20": close + 5,
                "drawdown_from_high20": 0.01,
                "avg_dollar_volume20": 100_000_000.0,
                "rsi14": 55.0,
                "atr20": 5.0,
                "adx14": 25.0,
                "return60": 0.10,
                "benchmark_return60": 0.08,
                "relative_return60": relative,
                "previous_close": close - 1,
                "previous_high": close - 0.5,
                "previous_ma5": close - 2,
                "feature_ready": True,
            }
        )
    return pd.DataFrame(rows)


def _config():
    return load_leverage_overlay_config(Path("configs/strategy/leverage_overlay.yaml"))


def test_leverage_overlay_requires_catalyst_review_before_allowing_2x() -> None:
    assessment = evaluate_leverage_overlay(
        features=_features(),
        signal_session=date(2026, 8, 7),
        underlying_symbol="MU",
        leveraged_etf_symbol="MUU",
        sector_etf="SOXX",
        market_symbol="QQQ",
        config=_config(),
        catalyst_confirmed=False,
    )

    assert assessment.action == "NEED_CATALYST_REVIEW"
    assert assessment.score == 7
    assert assessment.setup_type == "FIRST_PULLBACK"
    assert assessment.stop_pct is not None
    assert assessment.stop_pct <= _config().strategy.maximum_stop_pct


def test_leverage_overlay_allows_manual_review_after_catalyst_confirmation() -> None:
    assessment = evaluate_leverage_overlay(
        features=_features(),
        signal_session=date(2026, 8, 7),
        underlying_symbol="MU",
        leveraged_etf_symbol="MUU",
        sector_etf="SOXX",
        market_symbol="QQQ",
        config=_config(),
        catalyst_confirmed=True,
    )

    assert assessment.action == "ALLOW_MANUAL_REVIEW"
    assert assessment.score == 8
    assert assessment.risk_reward_estimate == 2.0


def test_leverage_overlay_accepts_tiny_risk_reward_rounding_drift() -> None:
    config = _config()
    tolerant_config = config.model_copy(
        update={
            "strategy": config.strategy.model_copy(update={"target_risk_reward": 2.000005})
        }
    )

    assessment = evaluate_leverage_overlay(
        features=_features(),
        signal_session=date(2026, 8, 7),
        underlying_symbol="MU",
        leveraged_etf_symbol="MUU",
        sector_etf="SOXX",
        market_symbol="QQQ",
        config=tolerant_config,
        catalyst_confirmed=True,
    )

    assert assessment.action == "ALLOW_MANUAL_REVIEW"
    assert assessment.risk_reward_estimate is not None
    assert assessment.risk_reward_estimate < tolerant_config.strategy.target_risk_reward


def test_leverage_overlay_blocks_when_market_is_not_risk_on() -> None:
    assessment = evaluate_leverage_overlay(
        features=_features(market_risk_on=False),
        signal_session=date(2026, 8, 7),
        underlying_symbol="MU",
        leveraged_etf_symbol="MUU",
        sector_etf="SOXX",
        market_symbol="QQQ",
        config=_config(),
        catalyst_confirmed=True,
    )

    assert assessment.action == "NO_2X_TRADE"
    assert any("Market proxy is not risk-on" in reason for reason in assessment.reasons)


def test_leverage_overlay_artifacts_include_catalyst_prompt(tmp_path: Path) -> None:
    assessment = evaluate_leverage_overlay(
        features=_features(),
        signal_session=date(2026, 8, 7),
        underlying_symbol="MU",
        leveraged_etf_symbol="MUU",
        sector_etf="SOXX",
        market_symbol="QQQ",
        config=_config(),
        catalyst_confirmed=False,
    )
    report = {
        "metadata": {
            "run_id": "run",
            "generated_at_utc": "2026-08-09T12:00:00+00:00",
            "signal_session": "2026-08-07",
            "earliest_order_session": "2026-08-10",
            "decision_scope": "MANUAL_2X_OVERLAY_ONLY_NOT_MAIN_STRATEGY",
        },
        "assessment": assessment.as_dict(),
    }

    artifacts = write_leverage_overlay_artifacts(
        report=report,
        report_root=tmp_path,
        signal_session=date(2026, 8, 7),
        run_id=uuid4(),
    )
    prompt = render_leverage_overlay_prompt(report)

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.prompt_path.exists()
    assert "MUU" in prompt
    assert "不要给自动下单建议" in prompt


def test_leverage_overlay_universe_config_covers_common_watchlist_pairs() -> None:
    universe = load_leverage_overlay_universe_config(
        Path("configs/universe/leverage_overlay_universe.yaml")
    )
    underlyings = {member.underlying_symbol for member in universe.pairs}

    assert {
        "AAPL",
        "AMD",
        "AMZN",
        "AVGO",
        "COIN",
        "DELL",
        "GOOGL",
        "META",
        "MRVL",
        "MSFT",
        "MSTR",
        "MU",
        "NBIS",
        "NVDA",
        "PLTR",
        "QQQ",
        "SMCI",
        "SOXX",
        "TSLA",
        "VRT",
    }.issubset(underlyings)

    watchlist_symbols = set()
    for path in (
        Path("configs/universe/ai_watchlist.yaml"),
        Path("configs/universe/ai_satellite_watchlist.yaml"),
        Path("configs/universe/internet_platform_watchlist.yaml"),
        Path("configs/universe/crypto_compute_watchlist.yaml"),
    ):
        watchlist_symbols.update(load_watchlist_config(path).member_symbols)
    benchmark_symbols = {"QQQ", "SOXX", "SPY"}
    assert underlyings.issubset(watchlist_symbols | benchmark_symbols)


def test_leverage_overlay_universe_workflow_writes_batch_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    loaded_universe = load_leverage_overlay_universe_config(
        Path("configs/universe/leverage_overlay_universe.yaml")
    )
    universe = loaded_universe.model_copy(update={"pairs": loaded_universe.pairs[:2]})

    def fake_load_feature_history(**_kwargs):
        features = _features()
        nvda = features.loc[features["symbol"] == "MU"].copy()
        nvda["symbol"] = "NVDA"
        return pd.concat([features, nvda], ignore_index=True)

    monkeypatch.setattr(
        "quant_system.decision.leverage.load_feature_history",
        fake_load_feature_history,
    )

    report, artifacts = run_leverage_overlay_universe_workflow(
        repository=ParquetRepository(tmp_path / "data"),
        database_path=tmp_path / "analytics.duckdb",
        report_root=tmp_path / "reports",
        signal_session=date(2026, 8, 7),
        config=_config(),
        universe=universe,
        catalyst_confirmed=False,
        run_id=uuid4(),
    )

    assert report["metadata"]["pair_count"] == 2
    json.dumps(report)
    assert artifacts.csv_path is not None
    assert artifacts.csv_path.exists()
    prompt = artifacts.prompt_path.read_text(encoding="utf-8")
    assert "2x ETF batch catalyst review prompt" in prompt
    assert "不要给自动下单建议" in prompt
