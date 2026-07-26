from datetime import UTC, datetime

import pytest

from quant_system.ingestion.sec import SecCompanyEventAdapter


def test_sec_adapter_parses_recent_filings_and_composes_url() -> None:
    def transport(url: str, headers: dict[str, str], timeout_seconds: float) -> dict[str, object]:
        assert url.endswith("CIK0001045810.json")
        assert headers["User-Agent"] == "qlib_quant test@openai.local"
        assert timeout_seconds == 5
        return {
            "filings": {
                "recent": {
                    "form": ["8-K", "4"],
                    "accessionNumber": ["0001045810-26-000001", "0001045810-26-000002"],
                    "filingDate": ["2026-07-01", "2026-07-01"],
                    "acceptanceDateTime": [
                        "2026-07-01T21:05:00.000Z",
                        "2026-07-01T22:05:00.000Z",
                    ],
                    "primaryDocument": ["nvda-20260701x8k.htm", "xslF345X05/doc.xml"],
                }
            }
        }

    adapter = SecCompanyEventAdapter(
        cik_by_symbol={"NVDA": "1045810"},
        user_agent="qlib_quant test@openai.local",
        timeout_seconds=5,
        forms=("8-K",),
        transport=transport,
    )

    result = adapter.fetch_events(
        ("NVDA",),
        start_utc=datetime(2026, 7, 1, 20, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, tzinfo=UTC),
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.symbol == "NVDA"
    assert event.form_type == "8-K"
    assert event.accepted_at_utc == datetime(2026, 7, 1, 21, 5, tzinfo=UTC)
    assert event.filing_url.endswith("/1045810/000104581026000001/nvda-20260701x8k.htm")


def test_sec_adapter_requires_contact_user_agent() -> None:
    with pytest.raises(ValueError, match="User-Agent"):
        SecCompanyEventAdapter(
            cik_by_symbol={"NVDA": "1045810"},
            user_agent="qlib_quant/0.1 contact@example.com",
            timeout_seconds=5,
            forms=("8-K",),
        )
