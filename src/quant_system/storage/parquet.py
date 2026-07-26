"""Immutable Parquet repository for raw point-in-time facts."""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds

from quant_system.domain.models import CompanyEvent, DailyPrice, NewsArticle
from quant_system.storage.schemas import (
    COMPANY_EVENT_SCHEMA,
    DAILY_PRICE_SCHEMA,
    NEWS_ARTICLE_SCHEMA,
)

_SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def daily_prices_to_table(records: Iterable[DailyPrice]) -> pa.Table:
    """Convert validated domain records to the canonical Arrow schema."""
    rows: list[dict[str, object]] = []
    for record in records:
        row = record.model_dump(mode="python")
        row["ingestion_run_id"] = str(row["ingestion_run_id"])
        row["quality_flags"] = list(row["quality_flags"])
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=DAILY_PRICE_SCHEMA)


def news_articles_to_table(records: Iterable[NewsArticle]) -> pa.Table:
    """Convert validated news records to the canonical Arrow schema."""
    rows: list[dict[str, object]] = []
    for record in records:
        row = record.model_dump(mode="python")
        row["url"] = str(row["url"])
        row["raw_tickers"] = list(row["raw_tickers"])
        row["raw_topics"] = list(row["raw_topics"])
        row["matched_symbols"] = list(row["matched_symbols"])
        row["event_type"] = str(row["event_type"])
        row["severity"] = str(row["severity"])
        row["ingestion_run_id"] = str(row["ingestion_run_id"])
        row["quality_flags"] = list(row["quality_flags"])
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=NEWS_ARTICLE_SCHEMA)


def company_events_to_table(records: Iterable[CompanyEvent]) -> pa.Table:
    """Convert validated SEC/company-event records to the canonical Arrow schema."""
    rows: list[dict[str, object]] = []
    for record in records:
        row = record.model_dump(mode="python")
        row["filing_url"] = str(row["filing_url"])
        row["event_type"] = str(row["event_type"])
        row["severity"] = str(row["severity"])
        row["ingestion_run_id"] = str(row["ingestion_run_id"])
        row["quality_flags"] = list(row["quality_flags"])
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=COMPANY_EVENT_SCHEMA)


class ParquetRepository:
    """Write immutable partitioned datasets beneath a local data root."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.daily_prices_root = self.data_root / "raw" / "prices"
        self.news_articles_root = self.data_root / "raw" / "news"
        self.company_events_root = self.data_root / "raw" / "company_events"

    def write_daily_prices(
        self,
        records: Iterable[DailyPrice],
        *,
        run_id: UUID,
        batch_id: str = "0000",
    ) -> list[Path]:
        """Validate and write daily-price records without replacing prior files."""
        return self.write_daily_price_table(
            daily_prices_to_table(records),
            run_id=run_id,
            batch_id=batch_id,
        )

    def write_daily_price_table(
        self,
        table: pa.Table,
        *,
        run_id: UUID,
        batch_id: str = "0000",
    ) -> list[Path]:
        """Atomically append a canonical table partitioned by NY session month."""
        if not _SAFE_BATCH_ID.fullmatch(batch_id):
            raise ValueError("batch_id may contain only letters, numbers, '_' and '-'")
        if table.num_rows == 0:
            return []

        canonical = self._canonicalize_table(table, DAILY_PRICE_SCHEMA, "daily-price")
        partitioned = canonical.append_column(
            "year",
            pc.year(canonical["session_date_ny"]),
        ).append_column(
            "month",
            pc.month(canonical["session_date_ny"]),
        )
        basename = f"part-{run_id}-{batch_id}-{{i}}.parquet"

        self.daily_prices_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".daily-prices-",
            dir=self.daily_prices_root,
        ) as temporary_directory:
            staging = Path(temporary_directory)
            ds.write_dataset(
                partitioned,
                base_dir=staging,
                format="parquet",
                partitioning=ds.partitioning(
                    pa.schema([pa.field("year", pa.int64()), pa.field("month", pa.int64())]),
                    flavor="hive",
                ),
                basename_template=basename,
                existing_data_behavior="error",
                max_rows_per_file=250_000,
                max_rows_per_group=64_000,
            )
            staged_files = sorted(staging.rglob("*.parquet"))
            destinations = [
                self.daily_prices_root / staged.relative_to(staging) for staged in staged_files
            ]
            collisions = [path for path in destinations if path.exists()]
            if collisions:
                raise FileExistsError(f"immutable Parquet file already exists: {collisions[0]}")

            for staged, destination in zip(staged_files, destinations, strict=True):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(staged, destination)

        return destinations

    def write_news_articles(
        self,
        records: Iterable[NewsArticle],
        *,
        run_id: UUID,
        batch_id: str = "0000",
    ) -> list[Path]:
        """Append raw news article records partitioned by publication month."""
        table = news_articles_to_table(records)
        return self.write_news_article_table(table, run_id=run_id, batch_id=batch_id)

    def write_news_article_table(
        self,
        table: pa.Table,
        *,
        run_id: UUID,
        batch_id: str = "0000",
    ) -> list[Path]:
        """Atomically append a canonical news table."""
        return self._write_partitioned_table(
            table,
            root=self.news_articles_root,
            schema=NEWS_ARTICLE_SCHEMA,
            partition_source_column="published_at_utc",
            dataset_name="news-article",
            run_id=run_id,
            batch_id=batch_id,
        )

    def write_company_events(
        self,
        records: Iterable[CompanyEvent],
        *,
        run_id: UUID,
        batch_id: str = "0000",
    ) -> list[Path]:
        """Append raw SEC/company events partitioned by acceptance month."""
        table = company_events_to_table(records)
        return self.write_company_event_table(table, run_id=run_id, batch_id=batch_id)

    def write_company_event_table(
        self,
        table: pa.Table,
        *,
        run_id: UUID,
        batch_id: str = "0000",
    ) -> list[Path]:
        """Atomically append a canonical company-event table."""
        return self._write_partitioned_table(
            table,
            root=self.company_events_root,
            schema=COMPANY_EVENT_SCHEMA,
            partition_source_column="accepted_at_utc",
            dataset_name="company-event",
            run_id=run_id,
            batch_id=batch_id,
        )

    def read_daily_prices(self) -> pa.Table:
        """Read all raw daily-price partitions using the canonical schema."""
        files = list(self.daily_prices_root.rglob("*.parquet"))
        if not files:
            return pa.Table.from_pylist([], schema=DAILY_PRICE_SCHEMA)
        dataset = ds.dataset(
            self.daily_prices_root,
            format="parquet",
            partitioning="hive",
            exclude_invalid_files=True,
        )
        return dataset.to_table(columns=DAILY_PRICE_SCHEMA.names).cast(DAILY_PRICE_SCHEMA)

    def read_news_articles(self) -> pa.Table:
        """Read all raw news partitions using the canonical schema."""
        return self._read_table(self.news_articles_root, NEWS_ARTICLE_SCHEMA)

    def read_company_events(self) -> pa.Table:
        """Read all raw company-event partitions using the canonical schema."""
        return self._read_table(self.company_events_root, COMPANY_EVENT_SCHEMA)

    @staticmethod
    def _canonicalize_table(
        table: pa.Table,
        schema: pa.Schema,
        dataset_name: str,
    ) -> pa.Table:
        missing = set(schema.names) - set(table.column_names)
        if missing:
            raise ValueError(f"{dataset_name} table is missing columns: {sorted(missing)}")
        canonical = table.select(schema.names).cast(schema)
        null_columns = [
            name for name in canonical.column_names if canonical[name].null_count > 0
        ]
        if null_columns:
            raise ValueError(f"non-null {dataset_name} columns contain nulls: {null_columns}")
        return canonical.replace_schema_metadata(schema.metadata)

    @staticmethod
    def _read_table(root: Path, schema: pa.Schema) -> pa.Table:
        files = list(root.rglob("*.parquet"))
        if not files:
            return pa.Table.from_pylist([], schema=schema)
        dataset = ds.dataset(
            root,
            format="parquet",
            partitioning="hive",
            exclude_invalid_files=True,
        )
        return dataset.to_table(columns=schema.names).cast(schema)

    def _write_partitioned_table(
        self,
        table: pa.Table,
        *,
        root: Path,
        schema: pa.Schema,
        partition_source_column: str,
        dataset_name: str,
        run_id: UUID,
        batch_id: str,
    ) -> list[Path]:
        if not _SAFE_BATCH_ID.fullmatch(batch_id):
            raise ValueError("batch_id may contain only letters, numbers, '_' and '-'")
        if table.num_rows == 0:
            return []

        canonical = self._canonicalize_table(table, schema, dataset_name)
        partitioned = canonical.append_column(
            "year",
            pc.year(canonical[partition_source_column]),
        ).append_column(
            "month",
            pc.month(canonical[partition_source_column]),
        )
        basename = f"part-{run_id}-{batch_id}-{{i}}.parquet"

        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{dataset_name}s-",
            dir=root,
        ) as temporary_directory:
            staging = Path(temporary_directory)
            ds.write_dataset(
                partitioned,
                base_dir=staging,
                format="parquet",
                partitioning=ds.partitioning(
                    pa.schema([pa.field("year", pa.int64()), pa.field("month", pa.int64())]),
                    flavor="hive",
                ),
                basename_template=basename,
                existing_data_behavior="error",
                max_rows_per_file=250_000,
                max_rows_per_group=64_000,
            )
            staged_files = sorted(staging.rglob("*.parquet"))
            destinations = [root / staged.relative_to(staging) for staged in staged_files]
            collisions = [path for path in destinations if path.exists()]
            if collisions:
                raise FileExistsError(f"immutable Parquet file already exists: {collisions[0]}")

            for staged, destination in zip(staged_files, destinations, strict=True):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(staged, destination)

        return destinations
