from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from quant_system.decision.digest import DigestValue, resolve_digest_universe
from quant_system.domain.securities import SecurityLifecycleEvent
from quant_system.universe.config import load_daily_digest_config
from quant_system.universe.lifecycle import LifecycleResolver


def watchlist(tmp_path, name, symbols, **overrides):
    payload = yaml.safe_load(Path("configs/universe/ai_watchlist.yaml").read_text())
    member = payload["symbols"][0]
    payload["selection_policy"].update(minimum_size=1, maximum_size=100)
    payload["symbols"] = [
        member | {"symbol": symbol, "start_date": "2025-01-01", **overrides} for symbol in symbols
    ]
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload))
    return path


def test_default_digest_covers_all_watchlists_without_research_cap():
    config = load_daily_digest_config()
    scope = resolve_digest_universe(config, as_of=date(2026, 11, 4))
    assert len(scope.universe_paths) == 7
    assert len(scope.scan_symbols) > 20
    assert len({row.symbol for row in scope.coverage}) == len(scope.coverage)
    assert {p.symbol for p in config.proxies if p.group == "sector"} == {
        "XLK",
        "XLF",
        "XLV",
        "XLY",
        "XLP",
        "XLE",
        "XLI",
        "XLU",
        "XLB",
        "XLRE",
        "XLC",
    }
    assert "^VIX" in scope.context_symbols
    assert "^VIX" not in scope.scan_symbols
    assert any(row.status == "PENDING_VERIFICATION" for row in scope.coverage)


def test_deduplicates_keeps_labels_and_never_promotes_raw_review(tmp_path):
    active = watchlist(tmp_path, "active.yaml", ["AAA", "BBB"])
    duplicate = watchlist(tmp_path, "duplicate.yaml", ["AAA"])
    review = watchlist(tmp_path, "review.yaml", ["AAA", "ZZZ"])
    config = load_daily_digest_config().model_copy(
        update={"universe_paths": (active, duplicate), "review_paths": (review,)}
    )
    scope = resolve_digest_universe(config, as_of=date(2026, 11, 4))
    rows = {row.symbol: row for row in scope.coverage}
    assert rows["AAA"].status == "IN_SCOPE" and len(rows["AAA"].source_lists) == 3
    assert rows["ZZZ"].status == "PENDING_VERIFICATION"
    assert "ZZZ" not in scope.scan_symbols
    assert set(scope.scan_symbols) == {"AAA", "BBB"}


def test_membership_and_lifecycle_cutoffs_are_applied_before_coverage(tmp_path):
    active = watchlist(tmp_path, "active.yaml", ["AAA", "BBB"])
    future = watchlist(tmp_path, "future.yaml", ["FUT"], start_date="2027-01-01")
    ended = watchlist(tmp_path, "ended.yaml", ["OLD"], end_date="2026-01-01")
    config = load_daily_digest_config().model_copy(
        update={"universe_paths": (active, future, ended), "review_paths": ()}
    )
    event = SecurityLifecycleEvent(
        event_id="delisted",
        security_id="AAA",
        symbol="AAA",
        event_type="DELISTED",
        effective_session=date(2026, 11, 3),
        source_url="https://example.com/delist",
        source="fixture",
        source_version="1",
        ingestion_run_id=uuid4(),
        available_at_utc=datetime(2026, 11, 3, 12, tzinfo=UTC),
        fetched_at_utc=datetime(2026, 11, 3, 12, tzinfo=UTC),
    )
    scope = resolve_digest_universe(
        config, as_of=date(2026, 11, 4), resolver=LifecycleResolver((event,))
    )
    rows = {row.symbol: row for row in scope.coverage}
    assert scope.scan_symbols == ("BBB",)
    assert rows["AAA"].reason == "DELISTED"
    assert rows["FUT"].reason == "membership_not_started"
    assert rows["OLD"].reason == "membership_ended"
    # A lifecycle fact only learned after the signal cutoff cannot remove a stock.
    later = event.model_copy(
        update={
            "available_at_utc": datetime(2026, 11, 5, 12, tzinfo=UTC),
            "fetched_at_utc": datetime(2026, 11, 5, 12, tzinfo=UTC),
        }
    )
    assert (
        "AAA"
        in resolve_digest_universe(
            config, as_of=date(2026, 11, 4), resolver=LifecycleResolver((later,))
        ).scan_symbols
    )


def test_unavailable_digest_value_requires_a_reason_and_cannot_be_fake_zero():
    assert DigestValue(status="UNAVAILABLE", reason="not_observed").value is None
    with pytest.raises(ValidationError):
        DigestValue(status="UNAVAILABLE", value=0)
    with pytest.raises(ValidationError):
        DigestValue(status="AVAILABLE", value=float("nan"))


def test_raw_review_cannot_be_configured_as_a_production_universe(tmp_path):
    path = watchlist(tmp_path, "raw.yaml", ["ZZZ"])
    payload = yaml.safe_load(path.read_text())
    payload["universe_type"] = "RAW_REVIEW"
    path.write_text(yaml.safe_dump(payload))
    config = load_daily_digest_config().model_copy(
        update={"universe_paths": (path,), "review_paths": ()}
    )
    with pytest.raises(ValueError, match="review-only"):
        resolve_digest_universe(config, as_of=date(2026, 11, 4))
