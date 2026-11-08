from quant_system.decision.manual_research import render_manual_prompt_batches


def test_prompt_batches_cover_every_candidate_without_global_cap():
    symbols = [f"TEST{i:02}" for i in range(45)]
    package = {
        "package_id": "11111111-1111-4111-8111-111111111111",
        "package_sha256": "a" * 64,
        "run_id": "11111111-1111-4111-8111-111111111111",
        "signal_session": "2026-06-30",
        "price_cutoff_utc": "2026-06-30T20:30:00+00:00",
        "generated_at_utc": "2026-07-01T12:00:00+00:00",
        "earliest_order_time_utc": "2026-07-01T13:30:00+00:00",
        "research_window_start_utc": "2026-06-24T12:00:00+00:00",
        "prompt_version": "manual-news-prompt-v2",
        "quant_context": {
            "symbols": symbols,
            "by_symbol": {s: {"raw_score": 0.12} for s in symbols},
        },
    }
    batches = render_manual_prompt_batches(package)
    assert [len(b["symbols"]) for b in batches] == [20, 20, 5]
    assert [s for b in batches for s in b["symbols"]] == symbols
    assert "TEST20" not in batches[0]["text"]
    assert all("实际联网" in b["text"] and "不能" in b["text"] for b in batches)
    assert all(package["package_sha256"] in b["text"] for b in batches)
