from datetime import UTC, datetime

import pytest

from quant_system.ingestion.alpha_vantage import AlphaVantageNewsAdapter
from quant_system.ingestion.base import ProviderError, ProviderUnavailableError


def test_intersection_filter_cannot_be_used_as_union_coverage():
    calls = []
    adapter = AlphaVantageNewsAdapter(
        api_key="fixture",
        timeout_seconds=1,
        limit_per_call=50,
        transport=lambda *args: calls.append(args) or {"feed": []},
    )
    with pytest.raises(ProviderError, match="one ticker"):
        adapter.fetch_news(
            ("AAPL", "MSFT"),
            start_utc=datetime(2026, 1, 1, tzinfo=UTC),
            end_utc=datetime(2026, 1, 2, tzinfo=UTC),
        )
    assert not calls


@pytest.mark.parametrize(
    "message", ["This is a premium endpoint", "Invalid API key secret-fixture"]
)
def test_entitlement_failure_is_permanent_and_redacted(message):
    adapter = AlphaVantageNewsAdapter(
        api_key="secret-fixture",
        timeout_seconds=1,
        limit_per_call=50,
        transport=lambda *_: {"Information": message},
    )
    with pytest.raises(ProviderUnavailableError) as caught:
        adapter.fetch_news(
            ("AAPL",),
            start_utc=datetime(2026, 1, 1, tzinfo=UTC),
            end_utc=datetime(2026, 1, 2, tzinfo=UTC),
        )
    assert "secret-fixture" not in str(caught.value)
