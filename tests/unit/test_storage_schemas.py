import pyarrow as pa

from quant_system.storage.schemas import DAILY_PRICE_SCHEMA, SCHEMA_VERSION


def test_daily_price_arrow_schema_is_versioned_and_timezone_aware() -> None:
    assert DAILY_PRICE_SCHEMA.metadata[b"schema_version"] == SCHEMA_VERSION.encode()
    assert DAILY_PRICE_SCHEMA.field("timestamp_utc").type == pa.timestamp("us", tz="UTC")
    assert DAILY_PRICE_SCHEMA.field("available_at_utc").type == pa.timestamp("us", tz="UTC")
    assert DAILY_PRICE_SCHEMA.field("volume").nullable is False
