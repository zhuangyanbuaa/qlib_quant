"""DuckDB analytical views over immutable Parquet datasets."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import TracebackType

import duckdb
import pandas as pd


class DuckDBAnalytics:
    """Own the local analytical database and its rebuildable views."""

    def __init__(
        self,
        database_path: Path,
        daily_prices_root: Path,
        news_articles_root: Path | None = None,
        company_events_root: Path | None = None,
    ) -> None:
        self.database_path = database_path.resolve()
        self.daily_prices_root = daily_prices_root.resolve()
        raw_root = self.daily_prices_root.parent
        self.news_articles_root = (
            news_articles_root.resolve() if news_articles_root else raw_root / "news"
        )
        self.company_events_root = (
            company_events_root.resolve()
            if company_events_root
            else raw_root / "company_events"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(self.database_path))

    def refresh_views(self) -> None:
        """Create raw and deduplicated analytical views."""
        self._refresh_daily_price_views()
        self._refresh_news_views()
        self._refresh_company_event_views()

    def symbol_date_range(self, symbol: str) -> tuple[date | None, date | None]:
        """Return the first and last available sessions for one symbol."""
        result = self.connection.execute(
            """
            SELECT min(session_date_ny), max(session_date_ny)
            FROM daily_prices
            WHERE symbol = ?
            """,
            [symbol.upper()],
        ).fetchone()
        if result is None:
            return None, None
        return result[0], result[1]

    def stored_symbols(self) -> list[str]:
        """Return all symbols currently represented in the curated view."""
        rows = self.connection.execute(
            "SELECT DISTINCT symbol FROM daily_prices ORDER BY symbol"
        ).fetchall()
        return [row[0] for row in rows]

    def latest_sessions(self, symbols: tuple[str, ...]) -> dict[str, date]:
        """Return the latest stored session for each requested symbol."""
        if not symbols:
            return {}
        rows = self.connection.execute(
            """
            SELECT symbol, max(session_date_ny)
            FROM daily_prices
            WHERE symbol IN (SELECT unnest(?))
            GROUP BY symbol
            """,
            [list(symbols)],
        ).fetchall()
        return {symbol: session for symbol, session in rows}

    def query_daily_prices(
        self,
        symbol: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """Query a symbol with optional inclusive date bounds."""
        return self.connection.execute(
            """
            SELECT *
            FROM daily_prices
            WHERE symbol = ?
              AND (? IS NULL OR session_date_ny >= CAST(? AS DATE))
              AND (? IS NULL OR session_date_ny <= CAST(? AS DATE))
            ORDER BY session_date_ny
            """,
            [symbol.upper(), start_date, start_date, end_date, end_date],
        ).fetchdf()

    def query_price_history(
        self,
        symbols: tuple[str, ...],
        *,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Load deduplicated OHLCV history for feature construction."""
        if not symbols:
            return pd.DataFrame()
        return self.connection.execute(
            """
            SELECT
                symbol,
                session_date_ny,
                open,
                high,
                low,
                close,
                volume
            FROM daily_prices
            WHERE symbol IN (SELECT unnest(?))
              AND session_date_ny BETWEEN ? AND ?
            ORDER BY session_date_ny, symbol
            """,
            [list(symbols), start_date, end_date],
        ).fetchdf()

    def query_news_articles(
        self,
        symbols: tuple[str, ...],
        *,
        cutoff_utc: datetime,
        lookback_hours: int,
    ) -> pd.DataFrame:
        """Load deduplicated point-in-time news visible at a historical cutoff."""
        if not symbols:
            return pd.DataFrame()
        requested = [symbol.upper() for symbol in symbols]
        return self.connection.execute(
            """
            SELECT *
            FROM news_articles
            WHERE available_at_utc <= ?
              AND published_at_utc >= ? - (? || ' hours')::INTERVAL
              AND list_has_any(matched_symbols, ?::VARCHAR[])
            ORDER BY severity DESC, published_at_utc DESC, article_id
            """,
            [cutoff_utc, cutoff_utc, lookback_hours, requested],
        ).fetchdf()

    def query_company_events(
        self,
        symbols: tuple[str, ...],
        *,
        cutoff_utc: datetime,
        lookback_hours: int,
    ) -> pd.DataFrame:
        """Load deduplicated SEC/company events visible at a historical cutoff."""
        if not symbols:
            return pd.DataFrame()
        requested = [symbol.upper() for symbol in symbols]
        return self.connection.execute(
            """
            SELECT *
            FROM company_events
            WHERE available_at_utc <= ?
              AND accepted_at_utc >= ? - (? || ' hours')::INTERVAL
              AND symbol IN (SELECT unnest(?))
            ORDER BY severity DESC, accepted_at_utc DESC, event_id
            """,
            [cutoff_utc, cutoff_utc, lookback_hours, requested],
        ).fetchdf()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> DuckDBAnalytics:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _refresh_daily_price_views(self) -> None:
        parquet_files = list(self.daily_prices_root.rglob("*.parquet"))
        if parquet_files:
            self._create_raw_view_from_parquet("raw_daily_prices", self.daily_prices_root)
        else:
            self.connection.execute(
                """
                CREATE OR REPLACE VIEW raw_daily_prices AS
                SELECT
                    CAST(NULL AS VARCHAR) AS symbol,
                    CAST(NULL AS TIMESTAMPTZ) AS timestamp_utc,
                    CAST(NULL AS DATE) AS session_date_ny,
                    CAST(NULL AS DOUBLE) AS open,
                    CAST(NULL AS DOUBLE) AS high,
                    CAST(NULL AS DOUBLE) AS low,
                    CAST(NULL AS DOUBLE) AS close,
                    CAST(NULL AS BIGINT) AS volume,
                    CAST(NULL AS BOOLEAN) AS adjusted,
                    CAST(NULL AS DOUBLE) AS split_factor,
                    CAST(NULL AS DOUBLE) AS dividend,
                    CAST(NULL AS VARCHAR) AS source,
                    CAST(NULL AS VARCHAR) AS source_version,
                    CAST(NULL AS TIMESTAMPTZ) AS fetched_at_utc,
                    CAST(NULL AS TIMESTAMPTZ) AS available_at_utc,
                    CAST(NULL AS VARCHAR) AS ingestion_run_id,
                    CAST(NULL AS BOOLEAN) AS is_stale,
                    CAST([] AS VARCHAR[]) AS quality_flags,
                    CAST(NULL AS BIGINT) AS year,
                    CAST(NULL AS BIGINT) AS month,
                    CAST(NULL AS VARCHAR) AS filename
                WHERE false
                """
            )

        self.connection.execute(
            """
            CREATE OR REPLACE VIEW daily_prices AS
            SELECT * EXCLUDE (_row_number, filename)
            FROM (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY symbol, session_date_ny
                        ORDER BY fetched_at_utc DESC, filename DESC
                    ) AS _row_number
                FROM raw_daily_prices
            )
            WHERE _row_number = 1
            """
        )

    def _refresh_news_views(self) -> None:
        parquet_files = list(self.news_articles_root.rglob("*.parquet"))
        if parquet_files:
            self._create_raw_view_from_parquet("raw_news_articles", self.news_articles_root)
        else:
            self.connection.execute(
                """
                CREATE OR REPLACE VIEW raw_news_articles AS
                SELECT
                    CAST(NULL AS VARCHAR) AS article_id,
                    CAST(NULL AS TIMESTAMPTZ) AS published_at_utc,
                    CAST(NULL AS VARCHAR) AS title,
                    CAST(NULL AS VARCHAR) AS summary,
                    CAST(NULL AS VARCHAR) AS url,
                    CAST(NULL AS VARCHAR) AS source_domain,
                    CAST(NULL AS VARCHAR) AS canonical_url,
                    CAST(NULL AS VARCHAR) AS dedupe_key,
                    CAST(NULL AS VARCHAR) AS semantic_key,
                    CAST(NULL AS VARCHAR) AS language,
                    CAST([] AS VARCHAR[]) AS raw_tickers,
                    CAST([] AS VARCHAR[]) AS raw_topics,
                    CAST([] AS VARCHAR[]) AS matched_symbols,
                    CAST(NULL AS VARCHAR) AS event_type,
                    CAST(NULL AS VARCHAR) AS severity,
                    CAST(NULL AS VARCHAR) AS sentiment_label,
                    CAST(NULL AS DOUBLE) AS sentiment_score,
                    CAST(NULL AS VARCHAR) AS source,
                    CAST(NULL AS VARCHAR) AS source_version,
                    CAST(NULL AS TIMESTAMPTZ) AS fetched_at_utc,
                    CAST(NULL AS TIMESTAMPTZ) AS available_at_utc,
                    CAST(NULL AS VARCHAR) AS ingestion_run_id,
                    CAST(NULL AS BOOLEAN) AS is_stale,
                    CAST([] AS VARCHAR[]) AS quality_flags,
                    CAST(NULL AS BIGINT) AS year,
                    CAST(NULL AS BIGINT) AS month,
                    CAST(NULL AS VARCHAR) AS filename
                WHERE false
                """
            )

        self.connection.execute(
            """
            CREATE OR REPLACE VIEW news_articles AS
            SELECT * EXCLUDE (_row_number, filename)
            FROM (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY dedupe_key
                        ORDER BY fetched_at_utc DESC, published_at_utc DESC, filename DESC
                    ) AS _row_number
                FROM raw_news_articles
            )
            WHERE _row_number = 1
            """
        )

    def _refresh_company_event_views(self) -> None:
        parquet_files = list(self.company_events_root.rglob("*.parquet"))
        if parquet_files:
            self._create_raw_view_from_parquet(
                "raw_company_events",
                self.company_events_root,
            )
        else:
            self.connection.execute(
                """
                CREATE OR REPLACE VIEW raw_company_events AS
                SELECT
                    CAST(NULL AS VARCHAR) AS event_id,
                    CAST(NULL AS VARCHAR) AS cik,
                    CAST(NULL AS VARCHAR) AS symbol,
                    CAST(NULL AS VARCHAR) AS form_type,
                    CAST(NULL AS VARCHAR) AS accession_number,
                    CAST(NULL AS TIMESTAMPTZ) AS filed_at_utc,
                    CAST(NULL AS TIMESTAMPTZ) AS accepted_at_utc,
                    CAST(NULL AS VARCHAR) AS filing_url,
                    CAST(NULL AS VARCHAR) AS event_type,
                    CAST(NULL AS VARCHAR) AS severity,
                    CAST(NULL AS VARCHAR) AS source,
                    CAST(NULL AS VARCHAR) AS source_version,
                    CAST(NULL AS TIMESTAMPTZ) AS fetched_at_utc,
                    CAST(NULL AS TIMESTAMPTZ) AS available_at_utc,
                    CAST(NULL AS VARCHAR) AS ingestion_run_id,
                    CAST(NULL AS BOOLEAN) AS is_stale,
                    CAST([] AS VARCHAR[]) AS quality_flags,
                    CAST(NULL AS BIGINT) AS year,
                    CAST(NULL AS BIGINT) AS month,
                    CAST(NULL AS VARCHAR) AS filename
                WHERE false
                """
            )

        self.connection.execute(
            """
            CREATE OR REPLACE VIEW company_events AS
            SELECT * EXCLUDE (_row_number, filename)
            FROM (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY event_id
                        ORDER BY fetched_at_utc DESC, accepted_at_utc DESC, filename DESC
                    ) AS _row_number
                FROM raw_company_events
            )
            WHERE _row_number = 1
            """
        )

    def _create_raw_view_from_parquet(self, view_name: str, root: Path) -> None:
        parquet_glob = self._sql_literal(str(root / "**" / "*.parquet"))
        self.connection.execute(
            f"""
            CREATE OR REPLACE VIEW {view_name} AS
            SELECT *
            FROM read_parquet(
                {parquet_glob},
                hive_partitioning = true,
                union_by_name = true,
                filename = true
            )
            """
        )

    @staticmethod
    def _sql_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
