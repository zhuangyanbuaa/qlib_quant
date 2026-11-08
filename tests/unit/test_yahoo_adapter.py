from datetime import date

import pandas as pd
import pytest

from quant_system.ingestion.yahoo import YahooFinancePriceAdapter


def make_download_frame() -> pd.DataFrame:
    index = pd.to_datetime(["2026-06-26", "2026-06-29"])
    columns = pd.MultiIndex.from_product(
        [["AAPL", "MSFT"], ["Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"]],
        names=["Ticker", "Price"],
    )
    frame = pd.DataFrame(index=index, columns=columns, dtype=float)
    for symbol in ("AAPL", "MSFT"):
        frame[(symbol, "Open")] = [100.0, 101.0]
        frame[(symbol, "High")] = [103.0, 104.0]
        frame[(symbol, "Low")] = [99.0, 100.0]
        frame[(symbol, "Close")] = [102.0, 103.0]
        frame[(symbol, "Volume")] = [1_000, 2_000]
        frame[(symbol, "Dividends")] = [0.0, 0.2]
        frame[(symbol, "Stock Splits")] = [0.0, 2.0]
    return frame


def test_adapter_normalizes_multi_ticker_response_and_explicit_options() -> None:
    captured: dict[str, object] = {}

    def download(symbols, **kwargs):
        captured["symbols"] = symbols
        captured.update(kwargs)
        return make_download_frame()

    adapter = YahooFinancePriceAdapter(download=download)
    result = adapter.fetch_daily(
        ("aapl", "MSFT"),
        start=date(2026, 6, 26),
        end_exclusive=date(2026, 6, 30),
    )

    assert len(result.bars) == 4
    assert result.empty_symbols == ()
    assert result.errors == {}
    assert captured["symbols"] == ["AAPL", "MSFT"]
    assert captured["auto_adjust"] is True
    assert captured["repair"] is False
    assert captured["threads"] is False
    aapl_latest = next(
        bar for bar in result.bars if bar.symbol == "AAPL" and bar.session_date.day == 29
    )
    assert aapl_latest.dividend == 0.2
    assert aapl_latest.split_factor == 2.0


def test_adapter_reports_empty_and_invalid_symbols_without_losing_good_data() -> None:
    frame = make_download_frame().drop(columns="MSFT", level=0)
    frame.loc[pd.Timestamp("2026-06-29"), ("AAPL", "Low")] = 105.0
    adapter = YahooFinancePriceAdapter(download=lambda *_args, **_kwargs: frame)

    result = adapter.fetch_daily(
        ("AAPL", "MSFT"),
        start=date(2026, 6, 26),
        end_exclusive=date(2026, 6, 30),
    )

    assert len(result.bars) == 1
    assert result.empty_symbols == ("MSFT",)
    assert result.warnings == {"AAPL": "dropped_invalid_rows:1"}


@pytest.mark.parametrize("column,value", [("Low", 100.0 + 1e-9), ("High", 102.0 - 1e-9)])
def test_adapter_rejects_tiny_ohlc_violations_before_domain_validation(column, value) -> None:
    frame = make_download_frame()
    frame.loc[pd.Timestamp("2026-06-26"), ("AAPL", column)] = value
    adapter = YahooFinancePriceAdapter(download=lambda *_args, **_kwargs: frame)

    result = adapter.fetch_daily(
        ("AAPL", "MSFT"),
        start=date(2026, 6, 26),
        end_exclusive=date(2026, 6, 30),
    )

    assert len(result.bars) == 3
    assert result.warnings == {"AAPL": "dropped_invalid_rows:1"}
    assert result.errors == {}
    for bar in result.bars:
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= max(bar.open, bar.close)


def test_adapter_version_records_normalization_revision(monkeypatch) -> None:
    monkeypatch.setattr("quant_system.ingestion.yahoo.version", lambda _package: "1.2.3")

    assert YahooFinancePriceAdapter().version == "yfinance-1.2.3/adapter-4"


def test_padded_symbol_is_empty_and_action_only_is_separate():
    frame = make_download_frame()
    frame["MSFT"] = float("nan")
    frame.loc[pd.Timestamp("2026-06-29"), ("MSFT", "Volume")] = 0
    frame.loc[pd.Timestamp("2026-06-29"), ("MSFT", "Dividends")] = 0.7025
    result = YahooFinancePriceAdapter(download=lambda *_args, **_kwargs: frame).fetch_daily(
        ("AAPL", "MSFT"), start=date(2026, 6, 26), end_exclusive=date(2026, 6, 30)
    )
    assert len(result.bars) == 2
    assert result.empty_symbols == ("MSFT",)
    assert not result.errors
    assert result.warnings == {"MSFT": "padded_rows:1;action_only_rows:1"}


@pytest.mark.parametrize(
    "column,value",
    [("Open", 0), ("High", float("inf")), ("Volume", float("inf")), ("Open", float("nan"))],
)
def test_genuine_invalid_ohlcv_remains_failure(column, value):
    frame = make_download_frame().iloc[:1].astype(float).copy()
    frame.loc[:, ("AAPL", column)] = value
    result = YahooFinancePriceAdapter(download=lambda *_args, **_kwargs: frame).fetch_daily(
        ("AAPL", "MSFT"), start=date(2026, 6, 26), end_exclusive=date(2026, 6, 30)
    )
    assert "AAPL" in result.errors
    assert result.empty_symbols == ()
    assert len(result.bars) == 1


def test_missing_columns_isolated_and_zero_volume_flagged():
    frame = make_download_frame().drop(columns=[("MSFT", "Open")])
    frame.loc[:, ("AAPL", "Volume")] = 0
    result = YahooFinancePriceAdapter(download=lambda *_args, **_kwargs: frame).fetch_daily(
        ("AAPL", "MSFT"), start=date(2026, 6, 26), end_exclusive=date(2026, 6, 30)
    )
    assert result.errors == {"MSFT": "missing Yahoo columns: ['Open']"}
    assert all("zero_volume_review" in bar.quality_flags for bar in result.bars)


def test_index_non_nyse_session_does_not_abort_other_symbols():
    # Cboe index history can include dates outside the application's NYSE clock.
    dates = pd.to_datetime(["2026-04-02", "2026-04-03", "2026-04-06"])
    vix = pd.DataFrame(
        {
            "Open": [20, 21, 22],
            "High": [21, 22, 23],
            "Low": [19, 20, 21],
            "Close": [20, 21, 22],
            "Volume": [0, 0, 0],
        },
        index=dates,
    )
    equity = vix.drop(pd.Timestamp("2026-04-03")).copy()
    equity["Volume"] = 1000
    response = pd.concat({"^VIX": vix, "XLK": equity}, axis=1)
    adapter = YahooFinancePriceAdapter(download=lambda *a, **k: response)
    result = adapter.fetch_daily(
        ("^VIX", "XLK"), start=date(2026, 4, 2), end_exclusive=date(2026, 4, 7)
    )
    assert {(bar.symbol, bar.session_date) for bar in result.bars} == {
        (symbol, day) for symbol in ("^VIX", "XLK") for day in (date(2026, 4, 2), date(2026, 4, 6))
    }
    assert "dropped_non_nyse_session_rows:1" in result.warnings["^VIX"]
