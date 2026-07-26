from pathlib import Path

from quant_system.universe.config import (
    load_benchmark_config,
    load_raw_candidate_pool_config,
    load_watchlist_config,
)


def test_ai_watchlist_is_forward_only_and_has_required_size() -> None:
    watchlist = load_watchlist_config(Path("configs/universe/ai_watchlist.yaml"))

    assert watchlist.universe_type == "CURRENT_SNAPSHOT_FORWARD_ONLY"
    assert 30 <= len(watchlist.symbols) <= 100
    assert len(watchlist.member_symbols) == len(set(watchlist.member_symbols))
    assert all(member.start_date.isoformat() == "2026-07-26" for member in watchlist.symbols)


def test_ai_watchlist_benchmarks_are_declared() -> None:
    watchlist = load_watchlist_config(Path("configs/universe/ai_watchlist.yaml"))
    benchmarks = load_benchmark_config(Path("configs/universe/benchmarks.yaml"))

    declared = {benchmark.symbol for benchmark in benchmarks.benchmarks}

    assert set(watchlist.benchmark_symbols).issubset(declared)
    assert {member.benchmark_etf for member in watchlist.symbols}.issubset(declared)


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
