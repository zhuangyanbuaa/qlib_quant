"""Yahoo Finance daily-price adapter behind a provider-neutral contract."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from importlib.metadata import version

import pandas as pd
import yfinance as yf

from quant_system.ingestion.base import (
    ProviderBar,
    ProviderBatchResult,
    TransientProviderError,
)

DownloadFunction = Callable[..., pd.DataFrame]
_REQUIRED_PRICE_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


class YahooFinancePriceAdapter:
    """Fetch adjusted daily OHLCV in small batches using yfinance."""

    name = "yahoo_finance"

    def __init__(
        self,
        *,
        download: DownloadFunction = yf.download,
        timeout_seconds: float = 15,
    ) -> None:
        self._download = download
        self.timeout_seconds = timeout_seconds

    @property
    def version(self) -> str:
        return f"yfinance-{version('yfinance')}"

    def fetch_daily(
        self,
        symbols: tuple[str, ...],
        *,
        start: date,
        end_exclusive: date,
    ) -> ProviderBatchResult:
        normalized_symbols = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        if not normalized_symbols:
            return ProviderBatchResult()
        try:
            downloaded = self._download(
                list(normalized_symbols),
                start=start.isoformat(),
                end=end_exclusive.isoformat(),
                interval="1d",
                auto_adjust=True,
                actions=True,
                repair=False,
                progress=False,
                threads=False,
                group_by="ticker",
                multi_level_index=True,
                timeout=self.timeout_seconds,
            )
        except Exception as error:
            raise TransientProviderError(
                f"Yahoo Finance batch failed: {type(error).__name__}: {error}"
            ) from error

        bars: list[ProviderBar] = []
        empty_symbols: list[str] = []
        errors: dict[str, str] = {}
        warnings: dict[str, str] = {}
        for symbol in normalized_symbols:
            try:
                frame = self._symbol_frame(downloaded, symbol, len(normalized_symbols))
            except (KeyError, ValueError) as error:
                errors[symbol] = str(error)
                continue
            if frame.empty:
                empty_symbols.append(symbol)
                continue
            symbol_bars, invalid_count = self._normalize_symbol(symbol, frame, start, end_exclusive)
            if not symbol_bars:
                errors[symbol] = "all returned rows failed OHLCV validation"
                continue
            bars.extend(symbol_bars)
            if invalid_count:
                warnings[symbol] = f"dropped_invalid_rows:{invalid_count}"

        return ProviderBatchResult(
            bars=tuple(bars),
            empty_symbols=tuple(empty_symbols),
            errors=errors,
            warnings=warnings,
        )

    @staticmethod
    def _symbol_frame(
        downloaded: pd.DataFrame,
        symbol: str,
        requested_symbol_count: int,
    ) -> pd.DataFrame:
        if downloaded is None or downloaded.empty:
            return pd.DataFrame()
        if isinstance(downloaded.columns, pd.MultiIndex):
            level_zero = set(downloaded.columns.get_level_values(0))
            level_one = set(downloaded.columns.get_level_values(1))
            if symbol in level_zero:
                return downloaded[symbol].copy()
            if symbol in level_one:
                return downloaded.xs(symbol, axis=1, level=1).copy()
            return pd.DataFrame()
        if requested_symbol_count == 1:
            return downloaded.copy()
        raise ValueError("multi-symbol response did not use MultiIndex columns")

    @staticmethod
    def _normalize_symbol(
        symbol: str,
        frame: pd.DataFrame,
        start: date,
        end_exclusive: date,
    ) -> tuple[list[ProviderBar], int]:
        missing = set(_REQUIRED_PRICE_COLUMNS) - set(frame.columns)
        if missing:
            raise ValueError(f"missing Yahoo columns: {sorted(missing)}")

        bars: list[ProviderBar] = []
        invalid_count = 0
        for index, row in frame.iterrows():
            session = pd.Timestamp(index).date()
            if not start <= session < end_exclusive:
                continue
            try:
                open_price = float(row["Open"])
                high = float(row["High"])
                low = float(row["Low"])
                close = float(row["Close"])
                volume_value = float(row["Volume"])
                if not all(
                    pd.notna(value)
                    for value in (open_price, high, low, close, volume_value)
                ):
                    raise ValueError
                tolerance = max(abs(open_price), abs(close), abs(high), abs(low)) * 1e-8
                if (
                    min(open_price, high, low, close) <= 0
                    or volume_value < 0
                    or low > min(open_price, close) + tolerance
                    or high + tolerance < max(open_price, close)
                    or low > high + tolerance
                ):
                    raise ValueError
                dividend = YahooFinancePriceAdapter._optional_float(row, "Dividends", 0.0)
                split = YahooFinancePriceAdapter._optional_float(row, "Stock Splits", 0.0)
                bars.append(
                    ProviderBar(
                        symbol=symbol,
                        session_date=session,
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=round(volume_value),
                        adjusted=True,
                        split_factor=split if split > 0 else 1.0,
                        dividend=max(0.0, dividend),
                        quality_flags=("provider_adjusted_prices",),
                    )
                )
            except (TypeError, ValueError, OverflowError):
                invalid_count += 1
        return bars, invalid_count

    @staticmethod
    def _optional_float(row: pd.Series, column: str, default: float) -> float:
        if column not in row or pd.isna(row[column]):
            return default
        return float(row[column])
