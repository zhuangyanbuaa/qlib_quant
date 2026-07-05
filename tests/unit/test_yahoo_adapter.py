from datetime import date

import pandas as pd

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
