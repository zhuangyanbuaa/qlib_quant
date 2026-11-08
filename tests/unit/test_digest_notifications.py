from datetime import UTC, datetime, timedelta

import pytest

from quant_system.decision.digest import DailyDigest, DigestStock, DigestValue
from quant_system.operations.digest_notifications import render_digest_notifications
from quant_system.operations.scheduling import ScheduleState
from quant_system.operations.telegram import TelegramError


def fixture_digest(count=0):
    stocks = tuple(
        DigestStock(
            symbol=f"FIX{i:04}",
            resolved_symbol=f"FIX{i:04}",
            status="ASSESSED",
            reason="news_review_required",
            entry_status="PENDING_NEWS",
            technical_score=DigestValue(status="AVAILABLE", value=80, reason=""),
        )
        for i in range(count)
    )
    return DailyDigest(
        schema_version="2",
        metadata={
            "run_id": "11111111-1111-4111-8111-111111111111",
            "signal_session": "2026-11-03",
            "earliest_order_session": "2026-11-04",
            "generated_at_utc": "2026-11-04T02:00:00+00:00",
        },
        coverage=(),
        stocks=stocks,
        pending_candidates=stocks,
    )


def test_unicode_pagination_is_complete_and_headers_share_run_and_session():
    pages = render_digest_notifications(fixture_digest(180), max_units=800, label="测试 🧪")
    assert len(pages) > 1
    assert all(len(p.encode("utf-16-le")) // 2 <= 800 for p in pages)
    for index, page in enumerate(pages, 1):
        assert f"{index}/{len(pages)}" in page and "2026-11-03" in page and "11111111" in page
    body = "\n".join(pages)
    for i in range(180):
        assert body.count(f"FIX{i:04}") == 1
    assert "CNN" in pages[0] and "VIX" in pages[0] and "系统技术分" in pages[0]
    assert "暂不可用" in body and "本机" not in body and "prompt" not in body


def test_no_candidates_and_blocked_are_distinct():
    assert "暂无符合条件" in "\n".join(render_digest_notifications(fixture_digest()))
    value = fixture_digest().model_copy(
        update={"status": "BLOCKED", "blocked_reasons": ("bad_price",)}
    )
    text = "\n".join(render_digest_notifications(value))
    assert "暂停入场建议" in text and "bad_price" in text and "暂无符合条件" not in text


class Sender:
    def __init__(self):
        self.calls = []
        self.fail = None

    def send(self, chat, text):
        self.calls.append(text)
        if self.fail and text == "page2":
            raise self.fail
        return len(self.calls)


@pytest.mark.parametrize("failure", ["retry", "unknown", "crash"])
def test_multipart_resumes_in_order_without_resending_successes(tmp_path, failure):
    path = tmp_path / "operations.sqlite"
    now = datetime(2026, 11, 4, 2, tzinfo=UTC)
    state = ScheduleState(path)
    state.enqueue_pages("bundle", "dest", ["page1", "page2", "page3"], now)
    sender = Sender()
    sender.fail = (
        TelegramError("telegram_rate_limited", retry_after=120)
        if failure == "retry"
        else (
            TelegramError("unknown", ambiguous=True)
            if failure == "unknown"
            else SystemExit("crash")
        )
    )
    if failure == "crash":
        with pytest.raises(SystemExit):
            state.deliver(sender, "42", "dest", now, 3)
    else:
        state.deliver(sender, "42", "dest", now, 3)
    state.close()
    state = ScheduleState(path)
    state.enqueue_pages("bundle", "dest", ["page1", "page2", "page3"], now)
    sender.fail = None
    state.deliver(sender, "42", "dest", now + timedelta(seconds=60), 3)
    assert sender.calls == ["page1", "page2"]
    state.deliver(sender, "42", "dest", now + timedelta(seconds=120), 3)
    assert sender.calls == (
        ["page1", "page2", "page2", "page3"] if failure == "retry" else ["page1", "page2"]
    )
    rows = state.db.execute(
        "SELECT part_index,total_parts,status FROM telegram_outbox ORDER BY part_index"
    ).fetchall()
    assert [r["status"] for r in rows] == (
        ["SENT"] * 3 if failure == "retry" else ["SENT", "UNKNOWN", "PENDING"]
    )
    assert all(r["total_parts"] == 3 for r in rows)
    state.close()


def test_expired_gate_and_legacy_delivery_do_not_resend(tmp_path):
    state = ScheduleState(tmp_path / "operations.sqlite")
    now = datetime(2026, 11, 4, 15, tzinfo=UTC)
    state.enqueue("old-key", "dest", "legacy", now)
    sender = Sender()
    state.deliver(sender, "42", "dest", now, 3)
    state.enqueue_pages("old-key", "dest", ["replacement"], now)
    state.enqueue_pages("gate", "dest", ["expired"], now, expires_at=now + timedelta(minutes=5))
    state.deliver(sender, "42", "dest", now + timedelta(minutes=6), 3)
    assert sender.calls == ["legacy"]
    assert (
        state.db.execute("SELECT status FROM telegram_outbox WHERE bundle_key='gate'").fetchone()[0]
        == "EXPIRED"
    )
    state.close()


def test_outbox_migrates_legacy_rows_and_preserves_delivery_budget(tmp_path):
    import sqlite3

    path = tmp_path / "operations.sqlite"
    now = datetime(2026, 11, 4, 2, tzinfo=UTC)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE telegram_outbox (delivery_key TEXT PRIMARY KEY, "
            "destination_hash TEXT NOT NULL,status TEXT NOT NULL, attempts INTEGER NOT NULL "
            "DEFAULT 0,next_at TEXT NOT NULL,text TEXT NOT NULL,message_id INTEGER,error_code TEXT)"
        )
        db.execute(
            "INSERT INTO telegram_outbox VALUES ('legacy','dest','SENT',1,?,'old',7,NULL)",
            (now.isoformat(),),
        )
    state = ScheduleState(path)
    assert (
        state.db.execute("SELECT bundle_key,total_parts FROM telegram_outbox").fetchone()[0]
        == "legacy"
    )
    state.enqueue_pages("new", "dest", [f"p{i}" for i in range(8)], now)
    sender = Sender()
    state.deliver(sender, "42", "dest", now, 3)
    assert sender.calls == [f"p{i}" for i in range(5)]
    state.deliver(sender, "42", "dest", now, 3)
    assert sender.calls == [f"p{i}" for i in range(8)]
    with pytest.raises(ValueError, match="conflict"):
        state.enqueue_pages("new", "dest", ["changed"], now)
    state.close()


def test_rate_limit_blocks_other_bundles_until_retry_after(tmp_path):
    state = ScheduleState(tmp_path / "operations.sqlite")
    now = datetime(2026, 11, 4, 2, tzinfo=UTC)
    state.enqueue_pages("a", "dest", ["page2"], now)
    state.enqueue_pages("b", "dest", ["another"], now)
    sender = Sender()
    sender.fail = TelegramError("telegram_rate_limited", retry_after=60)
    state.deliver(sender, "42", "dest", now, 3)
    state.deliver(sender, "42", "dest", now + timedelta(seconds=30), 3)
    assert sender.calls == ["page2"]
    sender.fail = None
    state.deliver(sender, "42", "dest", now + timedelta(seconds=60), 3)
    assert sender.calls == ["page2", "page2", "another"]
    state.close()


def test_market_and_all_sector_proxies_fit_first_page_with_readable_theme_names():
    from quant_system.decision.digest import DigestSector
    from quant_system.universe.config import load_daily_digest_config

    config = load_daily_digest_config()
    value = fixture_digest(180)
    sectors = [
        DigestSector(symbol=p.symbol, name=p.name, group=p.group)
        for p in config.proxies
        if p.group != "market"
    ]
    sectors.append(
        DigestSector(
            symbol="theme:ai_chips", name="ai_chips", group="theme", proxy_kind="WATCHLIST"
        )
    )
    value = value.model_copy(update={"sectors": tuple(sectors)})
    pages = render_digest_notifications(value)
    for proxy in config.proxies:
        if proxy.group != "market":
            assert proxy.symbol in pages[0]
    assert "AI 芯片" in pages[0] and "ai_chips" not in pages[0]
    assert "关注池代理" in pages[0]


def test_digest_gate_details_include_symbol_score_and_explicit_status():
    value = fixture_digest(1)
    value.data_health["latest_gate"] = {
        "rows": [{"symbol": "FIX0000", "decision": "CANCEL", "reason": "high_news_risk_snapshot"}]
    }
    text = "\n".join(render_digest_notifications(value, label="T+30 门禁"))
    assert "FIX0000 80/100" in text and "取消" in text and "T+30" in text


def test_retry_after_survives_exhausted_or_expired_bundle(tmp_path):
    state = ScheduleState(tmp_path / "operations.sqlite")
    now = datetime(2026, 11, 4, 2, tzinfo=UTC)
    state.enqueue_pages("a", "dest", ["page2"], now, expires_at=now + timedelta(seconds=10))
    state.enqueue_pages("b", "dest", ["later"], now)
    sender = Sender()
    sender.fail = TelegramError("telegram_rate_limited", retry_after=120)
    state.deliver(sender, "42", "dest", now, 1)
    state.close()
    state = ScheduleState(tmp_path / "operations.sqlite")
    sender.fail = None
    state.deliver(sender, "42", "dest", now + timedelta(seconds=60), 1)
    assert sender.calls == ["page2"]
    state.deliver(sender, "42", "dest", now + timedelta(seconds=120), 1)
    assert sender.calls == ["page2", "later"]
    state.close()


def test_legacy_or_wrong_session_digest_cannot_be_announced_as_current():
    from quant_system.operations.digest_notifications import notification_pages

    for changes in ({"schema_version": "1"}, {}):
        value = fixture_digest(1).model_copy(update=changes)
        result = {"status": "COMPLETED", "digest": value.model_dump(mode="json")}
        text = "\n".join(notification_pages(result, "2026-11-02"))
        assert "暂停入场建议" in text and "FIX0000" not in text
