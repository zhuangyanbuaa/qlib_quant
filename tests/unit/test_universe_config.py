from pathlib import Path

from quant_system.universe.config import load_benchmark_config, load_watchlist_config


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
