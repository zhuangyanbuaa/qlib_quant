import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from quant_system.decision.digest import DailyDigest, DigestStock
from quant_system.decision.digest_reports import publish_digest, render_digest_markdown


def digest(day="2026-06-30", generated="2026-07-01T12:00:00+00:00", **updates):
    from quant_system.domain.clocks import NyseSessionClock

    clock = NyseSessionClock()
    session = date.fromisoformat(day)
    meta = {
        "run_id": str(uuid4()),
        "signal_session": day,
        "generated_at_utc": generated,
        "evaluated_at_utc": generated,
        "canonical_generated_at_utc": generated,
        "price_cutoff_utc": clock.available_at_utc(session).isoformat(),
        "earliest_order_session": clock.next_session(session).isoformat(),
        "market_context_cutoff_utc": generated,
        "score_version": "technical-repair-v1",
    }
    return DailyDigest(
        schema_version="2", phase="SCORED_DECISION_SUPPORT", metadata=meta, coverage=(), **updates
    )


def test_daily_archives_and_latest_preserve_prior_runs(tmp_path):
    first = digest()
    result = publish_digest(first, report_root=tmp_path, mode="live")
    original = (tmp_path / "2026-06-30.md").read_text()
    run_bytes = Path(result["markdown"]).read_bytes()
    second = digest("2026-07-01", "2026-07-02T12:00:00+00:00")
    publish_digest(second, report_root=tmp_path, mode="live")
    assert (tmp_path / "2026-06-30.md").read_text() == original
    assert "2026-07-01" in (tmp_path / "latest.md").read_text()
    assert Path(result["markdown"]).read_bytes() == run_bytes
    assert (tmp_path / "2026-07-01.md").exists()


def test_replay_and_late_old_parent_cannot_replace_new_live_report(tmp_path):
    new = digest(generated="2026-07-01T12:30:00+00:00")
    publish_digest(new, report_root=tmp_path, mode="live")
    expected = (tmp_path / "latest.md").read_bytes()
    old = digest()
    old.metadata["evaluated_at_utc"] = "2026-07-01T13:00:00+00:00"
    old.metadata["generated_at_utc"] = "2026-07-01T13:00:00+00:00"
    publish_digest(old, report_root=tmp_path, mode="live")
    publish_digest(
        digest("2026-07-02", "2026-07-06T12:00:00+00:00"), report_root=tmp_path, mode="replay"
    )
    assert (tmp_path / "latest.md").read_bytes() == expected
    assert not (tmp_path / "2026-07-02.md").exists()


def test_blocked_attempt_replaces_latest_with_status_and_previous_success_link(tmp_path):
    publish_digest(digest(status="COMPLETED"), report_root=tmp_path, mode="live")
    bad = digest(
        "2026-07-01",
        "2026-07-02T12:00:00+00:00",
        status="BLOCKED",
        blocked_reasons=("core_symbol_not_current:SPY",),
    )
    publish_digest(bad, report_root=tmp_path, mode="live")
    text = (tmp_path / "latest.md").read_text()
    assert "暂停入场建议" in text and "最近成功报告" in text and "2026-06-30" in text
    assert "core_symbol_not_current:SPY" in text


def test_missing_cutoff_integrity_and_conflicting_run_are_rejected(tmp_path):
    valid = digest()
    result = publish_digest(valid, report_root=tmp_path, mode="live")
    before = (tmp_path / "latest.md").read_bytes()
    future = deepcopy(valid)
    future.metadata["price_cutoff_utc"] = "2027-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        publish_digest(future, report_root=tmp_path, mode="live")
    changed = valid.model_copy(update={"status": "BLOCKED"})
    with pytest.raises(ValueError):
        publish_digest(changed, report_root=tmp_path, mode="live")
    assert (tmp_path / "latest.md").read_bytes() == before
    assert json.loads(Path(result["json"]).read_text())["status"] == "DEGRADED"


def test_markdown_contains_full_prompt_and_escapes_untrusted_cells(tmp_path):
    value = digest(
        research={"prompts": [{"symbols": ["AAA"], "text": "Test prompt\n```json\n{}\n```"}]},
        stocks=(
            DigestStock(
                symbol="AAA",
                status="UNAVAILABLE",
                reason="no_price",
                company_name="<script>|`unsafe`",
            ),
        ),
    )
    value.metadata["model_status"] = "<script>|unsafe"
    text = render_digest_markdown(value, base_dir=tmp_path)
    assert "Test prompt" in text and "```json" in text
    assert "暂无符合条件的入场候选" in text
    assert "&lt;script&gt;&#124;&#96;unsafe&#96;" in text


def test_same_instant_blocked_attempt_wins_and_run_write_failure_preserves_alias(
    tmp_path, monkeypatch
):
    import quant_system.decision.digest_reports as reports

    successful = digest(status="COMPLETED")
    publish_digest(successful, report_root=tmp_path, mode="live")
    before = (tmp_path / "latest.md").read_bytes()
    replacement = reports.os.replace

    def fail_run(source, destination):
        if Path(destination).name == "digest.md":
            raise OSError("simulated disk failure")
        return replacement(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(reports.os, "replace", fail_run)
        with pytest.raises(OSError):
            publish_digest(digest(), report_root=tmp_path, mode="live")
    assert (tmp_path / "latest.md").read_bytes() == before
    publish_digest(digest(status="BLOCKED"), report_root=tmp_path, mode="live")
    assert "暂停入场建议" in (tmp_path / "latest.md").read_text()
    assert "最近成功报告" in (tmp_path / "latest.md").read_text()


def test_relative_links_resolve_from_run_and_fixed_reading_paths(tmp_path):
    import re
    from urllib.parse import unquote

    source = tmp_path / "sources with spaces" / "source.json"
    source.parent.mkdir()
    source.write_text("{}")
    paths = publish_digest(
        digest(artifacts={"source": str(source)}), report_root=tmp_path, mode="live"
    )
    for file in (Path(paths["markdown"]), tmp_path / "2026-06-30.md", tmp_path / "latest.md"):
        links = re.findall(r"\]\(([^)]+)\)", file.read_text())
        assert links
        assert all((file.parent / unquote(link)).is_file() for link in links)
