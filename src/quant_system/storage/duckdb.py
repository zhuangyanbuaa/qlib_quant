"""DuckDB analytical views over immutable Parquet datasets."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import TracebackType

import duckdb
import pandas as pd


class DuckDBAnalytics:
    """Own the local analytical database and its rebuildable views."""

    def __init__(self, database_path: Path, daily_prices_root: Path) -> None:
        self.database_path = database_path.resolve()
        self.daily_prices_root = daily_prices_root.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(self.database_path))

    def refresh_views(self) -> None:
        """Create raw and deduplicated daily-price views."""
        parquet_files = list(self.daily_prices_root.rglob("*.parquet"))
        if parquet_files:
            parquet_glob = self._sql_literal(
                str(self.daily_prices_root / "**" / "*.parquet")
            )
            self.connection.execute(
                f"""
                CREATE OR REPLACE VIEW raw_daily_prices AS
                SELECT *
                FROM read_parquet(
                    {parquet_glob},
                    hive_partitioning = true,
                    union_by_name = true,
                    filename = true
                )
                """
            )
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

    @staticmethod
    def _sql_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
