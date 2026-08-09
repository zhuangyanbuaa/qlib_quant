from pathlib import Path

from quant_system.universe.config import (
    load_benchmark_config,
    load_raw_candidate_pool_config,
    load_watchlist_config,
)

THEMATIC_SATELLITE_PATHS = (
    Path("configs/universe/internet_platform_watchlist.yaml"),
    Path("configs/universe/space_satellite_watchlist.yaml"),
    Path("configs/universe/crypto_compute_watchlist.yaml"),
    Path("configs/universe/power_energy_satellite.yaml"),
    Path("configs/universe/raw_review_watchlist.yaml"),
)

RAW_CANDIDATE_SYMBOL_EXCLUSIONS = {
    # Futu concept label, not a Yahoo/US tradable ticker.
    "LIST2152",
}


def test_ai_watchlist_is_forward_only_and_has_required_size() -> None:
    watchlist = load_watchlist_config(Path("configs/universe/ai_watchlist.yaml"))

    assert watchlist.universe_type == "CURRENT_SNAPSHOT_FORWARD_ONLY"
    assert 70 <= len(watchlist.symbols) <= 100
    assert len(watchlist.member_symbols) == len(set(watchlist.member_symbols))
    assert all(member.start_date.isoformat() == "2026-07-26" for member in watchlist.symbols)
    assert {"NVDA", "CRDO", "VRT", "CEG"}.issubset(set(watchlist.member_symbols))
    assert "COST" not in watchlist.member_symbols


def test_ai_watchlist_benchmarks_are_declared() -> None:
    watchlist = load_watchlist_config(Path("configs/universe/ai_watchlist.yaml"))
    benchmarks = load_benchmark_config(Path("configs/universe/benchmarks.yaml"))

    declared = {benchmark.symbol for benchmark in benchmarks.benchmarks}

    assert set(watchlist.benchmark_symbols).issubset(declared)
    assert {member.benchmark_etf for member in watchlist.symbols}.issubset(declared)


def test_ai_satellite_watchlist_is_separate_forward_only_layer() -> None:
    satellite = load_watchlist_config(Path("configs/universe/ai_satellite_watchlist.yaml"))
    ai_watchlist = load_watchlist_config(Path("configs/universe/ai_watchlist.yaml"))
    hedge_overlay = load_watchlist_config(Path("configs/universe/hedge_overlay.yaml"))
    benchmarks = load_benchmark_config(Path("configs/universe/benchmarks.yaml"))
    declared = {benchmark.symbol for benchmark in benchmarks.benchmarks}

    assert satellite.universe_type == "AI_SATELLITE"
    assert 40 <= len(satellite.symbols) <= 120
    assert {"MXL", "NVTS", "CDNS", "SNPS", "ONTO", "CAMT"}.issubset(
        set(satellite.member_symbols)
    )
    assert set(satellite.benchmark_symbols).issubset(declared)
    assert {member.benchmark_etf for member in satellite.symbols}.issubset(declared)
    assert set(satellite.member_symbols).isdisjoint(set(ai_watchlist.member_symbols))
    assert set(satellite.member_symbols).isdisjoint(set(hedge_overlay.member_symbols))


def test_thematic_satellite_watchlists_cover_remaining_raw_candidates() -> None:
    pool = load_raw_candidate_pool_config(Path("configs/universe/futu_candidates_raw.yaml"))
    ai_watchlist = load_watchlist_config(Path("configs/universe/ai_watchlist.yaml"))
    ai_satellite = load_watchlist_config(Path("configs/universe/ai_satellite_watchlist.yaml"))
    hedge_overlay = load_watchlist_config(Path("configs/universe/hedge_overlay.yaml"))
    benchmarks = load_benchmark_config(Path("configs/universe/benchmarks.yaml"))
    declared = {benchmark.symbol for benchmark in benchmarks.benchmarks}

    thematic_symbols: set[str] = set()
    thematic_types: set[str] = set()
    for path in THEMATIC_SATELLITE_PATHS:
        watchlist = load_watchlist_config(path)
        thematic_symbols.update(watchlist.member_symbols)
        thematic_types.add(watchlist.universe_type)
        assert set(watchlist.benchmark_symbols).issubset(declared)
        assert {member.benchmark_etf for member in watchlist.symbols}.issubset(declared)

    assert thematic_types == {
        "INTERNET_PLATFORM_SATELLITE",
        "SPACE_SATELLITE",
        "CRYPTO_COMPUTE_SATELLITE",
        "POWER_ENERGY_SATELLITE",
        "RAW_REVIEW",
    }
    formal_symbols = (
        set(ai_watchlist.member_symbols)
        | set(ai_satellite.member_symbols)
        | set(hedge_overlay.member_symbols)
        | thematic_symbols
        | declared
    )
    assert set(pool.member_symbols).issubset(formal_symbols | RAW_CANDIDATE_SYMBOL_EXCLUSIONS)
    assert RAW_CANDIDATE_SYMBOL_EXCLUSIONS.isdisjoint(formal_symbols)
    assert thematic_symbols.isdisjoint(set(ai_watchlist.member_symbols))
    assert thematic_symbols.isdisjoint(set(ai_satellite.member_symbols))
    assert thematic_symbols.isdisjoint(set(hedge_overlay.member_symbols))


def test_hedge_overlay_is_separate_from_ai_alpha_watchlist() -> None:
    ai_watchlist = load_watchlist_config(Path("configs/universe/ai_watchlist.yaml"))
    hedge_overlay = load_watchlist_config(Path("configs/universe/hedge_overlay.yaml"))
    benchmarks = load_benchmark_config(Path("configs/universe/benchmarks.yaml"))
    declared = {benchmark.symbol for benchmark in benchmarks.benchmarks}

    assert hedge_overlay.universe_type == "HEDGE_OVERLAY"
    assert 20 <= len(hedge_overlay.symbols) <= 60
    assert set(hedge_overlay.benchmark_symbols).issubset(declared)
    assert {member.benchmark_etf for member in hedge_overlay.symbols}.issubset(declared)
    assert {"COST", "WMT", "PG", "JNJ", "SO", "DUK"}.issubset(
        set(hedge_overlay.member_symbols)
    )
    assert set(ai_watchlist.member_symbols).isdisjoint(set(hedge_overlay.member_symbols))


def test_futu_raw_candidate_pool_contains_futu_and_supplemental_symbols() -> None:
    pool = load_raw_candidate_pool_config(Path("configs/universe/futu_candidates_raw.yaml"))

    assert pool.universe_type == "RAW_RESEARCH_CANDIDATES"
    assert len(pool.symbols) > 200
    assert len(pool.member_symbols) == len(set(pool.member_symbols))
    assert "NVDA" in pool.member_symbols
    assert "CRDO" in pool.member_symbols
    assert "COST" in pool.member_symbols
    assert "BRK-B" in pool.member_symbols
    assert {member.added_by for member in pool.symbols} == {"futu", "codex"}


def test_futu_raw_candidate_pool_separates_ai_alpha_from_hedges() -> None:
    pool = load_raw_candidate_pool_config(Path("configs/universe/futu_candidates_raw.yaml"))
    by_symbol = {member.symbol: member for member in pool.symbols}

    assert by_symbol["NVDA"].suggested_action == "promote_to_ai_watchlist"
    assert by_symbol["CRDO"].candidate_bucket == "ai_hardware"
    assert by_symbol["COST"].suggested_action == "use_as_hedge_overlay"
    assert by_symbol["UNH"].watchlist_role == "hedge_overlay"
    assert by_symbol["DRAM"].suggested_action == "exclude_or_manual_review"
