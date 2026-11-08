"""DuckDB analytical views over immutable Parquet datasets."""

from __future__ import annotations

from datetime import UTC, date, datetime
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
            company_events_root.resolve() if company_events_root else raw_root / "company_events"
        )
        self.security_lifecycle_root = raw_root / "security_lifecycle"
        self.intraday_snapshots_root = raw_root / "intraday_snapshots"
        self.market_observations_root = raw_root / "market_observations"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(self.database_path))

    def refresh_views(self) -> None:
        """Create raw and deduplicated analytical views."""
        self._refresh_daily_price_views()
        self._refresh_news_views()
        self._refresh_company_event_views()
        self._refresh_lifecycle_views()
        self._refresh_intraday_views()
        self._refresh_market_context_views()

    def _refresh_market_context_views(self) -> None:
        if list(self.market_observations_root.rglob("*.parquet")):
            self._create_raw_view_from_parquet(
                "raw_market_observations", self.market_observations_root
            )
        else:
            self.connection.execute("""
                CREATE OR REPLACE VIEW raw_market_observations AS SELECT
                CAST(NULL AS VARCHAR) AS series_id,
                CAST(NULL AS TIMESTAMPTZ) AS observed_at_utc,
                CAST(NULL AS DATE) AS observation_date, CAST(NULL AS DOUBLE) AS value,
                CAST(NULL AS VARCHAR) AS units, CAST(NULL AS VARCHAR) AS label,
                CAST(NULL AS VARCHAR) AS source_url, CAST(NULL AS VARCHAR) AS input_reference,
                CAST(NULL AS VARCHAR) AS source, CAST(NULL AS VARCHAR) AS source_version,
                CAST(NULL AS TIMESTAMPTZ) AS fetched_at_utc,
                CAST(NULL AS TIMESTAMPTZ) AS available_at_utc,
                CAST(NULL AS VARCHAR) AS ingestion_run_id, CAST(NULL AS BOOLEAN) AS is_stale,
                CAST([] AS VARCHAR[]) AS quality_flags, CAST(NULL AS VARCHAR) AS filename
                WHERE false
            """)

    def query_market_observations(self, *, cutoff_utc: datetime) -> pd.DataFrame:
        return self.connection.execute(
            """
            SELECT * EXCLUDE (_row_number, filename) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY series_id, observed_at_utc
                    ORDER BY available_at_utc DESC, fetched_at_utc DESC, filename DESC
                ) AS _row_number FROM raw_market_observations
                WHERE available_at_utc<=? AND fetched_at_utc<=? AND observed_at_utc<=?
            ) WHERE _row_number=1 ORDER BY observed_at_utc, available_at_utc
        """,
            [self._utc_cutoff(cutoff_utc)] * 3,
        ).fetchdf()

    def query_vix_context(self, *, cutoff_utc: datetime, session: date) -> pd.DataFrame:
        """Context requires actual local observation; never backdate a newly fetched bar."""
        return self.connection.execute(
            """
            SELECT * EXCLUDE (_row_number) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY session_date_ny ORDER BY fetched_at_utc DESC, filename DESC
                ) AS _row_number FROM raw_daily_prices
                WHERE symbol='^VIX' AND session_date_ny<=?
                AND available_at_utc<=? AND fetched_at_utc<=? AND timestamp_utc<=?
            ) WHERE _row_number=1 ORDER BY session_date_ny
        """,
            [session, *[self._utc_cutoff(cutoff_utc)] * 3],
        ).fetchdf()

    def _refresh_intraday_views(self) -> None:
        if list(self.intraday_snapshots_root.rglob("*.parquet")):
            self._create_raw_view_from_parquet(
                "raw_intraday_snapshots", self.intraday_snapshots_root
            )
        else:
            self.connection.execute("""
                CREATE OR REPLACE VIEW raw_intraday_snapshots AS SELECT
                CAST(NULL AS VARCHAR) AS symbol, CAST(NULL AS DOUBLE) AS last_price,
                CAST(NULL AS TIMESTAMPTZ) AS quote_at_utc,
                CAST(NULL AS DATE) AS session_date_ny,
                CAST(NULL AS VARCHAR) AS news_risk, CAST(NULL AS VARCHAR) AS liquidity_status,
                CAST(NULL AS VARCHAR) AS note, CAST(NULL AS VARCHAR) AS source,
                CAST(NULL AS VARCHAR) AS source_version,
                CAST(NULL AS TIMESTAMPTZ) AS fetched_at_utc,
                CAST(NULL AS TIMESTAMPTZ) AS available_at_utc,
                CAST(NULL AS VARCHAR) AS ingestion_run_id, CAST(NULL AS BOOLEAN) AS is_stale,
                CAST([] AS VARCHAR[]) AS quality_flags, CAST(NULL AS VARCHAR) AS filename
                WHERE false
            """)

    def query_intraday_snapshots(self, *, session: date, cutoff_utc: datetime) -> pd.DataFrame:
        return self.connection.execute(
            """
            SELECT * EXCLUDE (_row_number) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY symbol ORDER BY quote_at_utc DESC,
                    available_at_utc DESC, fetched_at_utc DESC, filename DESC
                ) AS _row_number
                FROM raw_intraday_snapshots WHERE session_date_ny=?
                AND available_at_utc<=? AND fetched_at_utc<=? AND quote_at_utc<=?
            ) WHERE _row_number=1
        """,
            [session, *[self._utc_cutoff(cutoff_utc)] * 3],
        ).fetchdf()

    def query_security_lifecycle(self, *, cutoff_utc: datetime) -> pd.DataFrame:
        """Filter availability before resolving the newest visible event revision."""
        return self.connection.execute(
            """
            SELECT * EXCLUDE (_row_number) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY event_id ORDER BY fetched_at_utc DESC, filename DESC
                ) AS _row_number
                FROM raw_security_lifecycle WHERE available_at_utc <= ?
            ) WHERE _row_number = 1
            """,
            [self._utc_cutoff(cutoff_utc)],
        ).fetchdf()

    def _refresh_lifecycle_views(self) -> None:
        if list(self.security_lifecycle_root.rglob("*.parquet")):
            self._create_raw_view_from_parquet(
                "raw_security_lifecycle", self.security_lifecycle_root
            )
        else:
            self.connection.execute("""
                CREATE OR REPLACE VIEW raw_security_lifecycle AS SELECT
                CAST(NULL AS VARCHAR) AS event_id, CAST(NULL AS VARCHAR) AS security_id,
                CAST(NULL AS VARCHAR) AS symbol, CAST(NULL AS VARCHAR) AS event_type,
                CAST(NULL AS VARCHAR) AS successor_symbol,
                CAST(NULL AS VARCHAR) AS successor_security_id,
                CAST(NULL AS VARCHAR) AS source_url, CAST(NULL AS DATE) AS effective_session,
                CAST(NULL AS VARCHAR) AS source, CAST(NULL AS VARCHAR) AS source_version,
                CAST(NULL AS TIMESTAMPTZ) AS fetched_at_utc,
                CAST(NULL AS TIMESTAMPTZ) AS available_at_utc,
                CAST(NULL AS VARCHAR) AS ingestion_run_id, CAST(NULL AS BOOLEAN) AS is_stale,
                CAST([] AS VARCHAR[]) AS quality_flags, CAST(NULL AS VARCHAR) AS filename
                WHERE false
            """)

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
        cutoff_utc: datetime | None = None,
    ) -> pd.DataFrame:
        """Inspect latest prices, or explicitly query a historical snapshot."""
        if cutoff_utc is not None:
            return self._query_price_snapshot(
                (symbol.upper(),),
                start_date=start_date,
                end_date=end_date,
                cutoff_utc=cutoff_utc,
            )
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
        cutoff_utc: datetime,
    ) -> pd.DataFrame:
        """Read a historical snapshot, filtering availability before deduplication."""
        return self._query_price_snapshot(
            symbols,
            start_date=start_date,
            end_date=end_date,
            cutoff_utc=cutoff_utc,
        )

    def _query_price_snapshot(
        self,
        symbols: tuple[str, ...],
        *,
        start_date: date | str | None,
        end_date: date | str | None,
        cutoff_utc: datetime,
    ) -> pd.DataFrame:
        cutoff_utc = self._utc_cutoff(cutoff_utc)
        if not symbols:
            return pd.DataFrame()
        return self.connection.execute(
            """
            SELECT * EXCLUDE (_row_number, filename)
            FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY symbol, session_date_ny
                    ORDER BY fetched_at_utc DESC, filename DESC
                ) AS _row_number
                FROM raw_daily_prices
                WHERE symbol IN (SELECT unnest(?))
                  AND (? IS NULL OR session_date_ny >= CAST(? AS DATE))
                  AND (? IS NULL OR session_date_ny <= CAST(? AS DATE))
                  AND available_at_utc <= ?
            )
            WHERE _row_number = 1
            ORDER BY session_date_ny, symbol
            """,
            [list(symbols), start_date, start_date, end_date, end_date, cutoff_utc],
        ).fetchdf()

    def query_price_versions(
        self,
        symbols: tuple[str, ...],
        *,
        start_date: date,
        end_date: date,
        cutoff_utc: datetime,
    ) -> pd.DataFrame:
        """Load raw versions once for per-decision causal feature construction."""
        cutoff_utc = self._utc_cutoff(cutoff_utc)
        if not symbols:
            return pd.DataFrame()
        return self.connection.execute(
            """
            SELECT * FROM raw_daily_prices
            WHERE symbol IN (SELECT unnest(?))
              AND session_date_ny BETWEEN ? AND ?
              AND available_at_utc <= ?
            ORDER BY session_date_ny, symbol, fetched_at_utc, filename
            """,
            [list(symbols), start_date, end_date, cutoff_utc],
        ).fetchdf()

    @staticmethod
    def _utc_cutoff(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cutoff_utc must be timezone-aware")
        return value.astimezone(UTC)

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
