"""Immutable Parquet repository for raw daily prices."""

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

from quant_system.domain.models import DailyPrice
from quant_system.storage.schemas import DAILY_PRICE_SCHEMA

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


class ParquetRepository:
    """Write immutable partitioned datasets beneath a local data root."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.daily_prices_root = self.data_root / "raw" / "prices"

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

        canonical = self._canonicalize_daily_prices(table)
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

    @staticmethod
    def _canonicalize_daily_prices(table: pa.Table) -> pa.Table:
        missing = set(DAILY_PRICE_SCHEMA.names) - set(table.column_names)
        if missing:
            raise ValueError(f"daily-price table is missing columns: {sorted(missing)}")
        canonical = table.select(DAILY_PRICE_SCHEMA.names).cast(DAILY_PRICE_SCHEMA)
        null_columns = [
            name for name in canonical.column_names if canonical[name].null_count > 0
        ]
        if null_columns:
            raise ValueError(f"non-null daily-price columns contain nulls: {null_columns}")
        return canonical.replace_schema_metadata(DAILY_PRICE_SCHEMA.metadata)
