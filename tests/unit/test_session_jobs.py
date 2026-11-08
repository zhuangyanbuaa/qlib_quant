from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from quant_system.operations.scheduling import DailySchedule, ScheduleState
from quant_system.operations.session_jobs import (
    find_session_plan,
    session_windows,
    tick_session_jobs,
)
from quant_system.settings import Settings

NOW = datetime(2026, 10, 13, 14, tzinfo=UTC)


def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        telegram_bot_token="123456:" + ("fixture_token_" * 3),
        telegram_chat_id="42",
    )


class Notifier:
    def __init__(self):
        self.calls = []

    def send(self, chat, text):
        self.calls.append(text)
        return len(self.calls)


@pytest.mark.parametrize("day,hour", [(date(2026, 3, 6), 14), (date(2026, 3, 9), 13)])
def test_open_jobs_follow_dst_and_collect_before_cutoff(day, hour):
    config = DailySchedule(session_jobs=("premarket", "preopen", "open30", "open60"))
    now = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    rows = [w for w in session_windows(config, now) if w["order_session"] == day]
    gate = next(w for w in rows if w["kind"] == "open30")
    snapshot = next(w for w in rows if w["kind"] == "snapshot_open30")
    assert gate["due"].hour == hour + 1 and gate["due"].minute == 0
    assert gate["due"] - snapshot["due"] == timedelta(minutes=2)
    assert next(w for w in rows if w["kind"] == "preopen")["due"].minute == 20


def test_weekly_uses_last_exchange_session_on_friday_holiday():
    now = datetime(2026, 7, 2, 21, tzinfo=UTC)
    rows = session_windows(DailySchedule(session_jobs=("weekly",)), now)
    active = [w for w in rows if w["due"] <= now < w["deadline"]]
    assert len(active) == 1 and active[0]["signal_session"] == date(2026, 7, 2)
    assert active[0]["order_session"] == date(2026, 7, 6)


def test_session_jobs_have_independent_keys_and_deduplicated_send(tmp_path):
    calls = []
    notifier = Notifier()

    def execute(*args, **kwargs):
        calls.append(args[2]["kind"])
        return {"status": "DEGRADED", "summary": {"defer_count": 1}}

    config = DailySchedule(session_jobs=("open30", "open60"))
    for now in (NOW, NOW, NOW + timedelta(minutes=30), NOW + timedelta(minutes=30)):
        result = tick_session_jobs(
            settings(tmp_path), config, now_utc=now, executor=execute, client=notifier
        )
        assert result["jobs"][0]["status"] == "DONE"
    assert calls == ["open30", "open60"] and len(notifier.calls) == 2
    assert "DEFER" in notifier.calls[0]


def test_late_wake_does_not_backfill_gate(tmp_path):
    calls = []
    result = tick_session_jobs(
        settings(tmp_path),
        DailySchedule(session_jobs=("open30",)),
        now_utc=NOW + timedelta(minutes=6),
        executor=lambda *a, **k: calls.append(True),
        client=Notifier(),
    )
    assert result["jobs"] == [] and not calls


def test_failures_retry_only_within_window_and_limit(tmp_path):
    calls = []
    notifier = Notifier()

    def execute(*args, **kwargs):
        calls.append(True)
        return {"status": "BLOCKED"}

    config = DailySchedule(session_jobs=("open30",))
    for seconds in (0, 30, 60, 120, 600):
        tick_session_jobs(
            settings(tmp_path),
            config,
            now_utc=NOW + timedelta(seconds=seconds),
            executor=execute,
            client=notifier,
        )
    assert len(calls) == 2 and len(notifier.calls) == 2


def test_crash_is_reported_even_after_job_window_expired(tmp_path):
    def execute(*args, **kwargs):
        raise SystemExit("fixture")

    config = DailySchedule(session_jobs=("open30",))
    with pytest.raises(SystemExit):
        tick_session_jobs(
            settings(tmp_path), config, now_utc=NOW, executor=execute, client=Notifier()
        )
    notifier = Notifier()
    result = tick_session_jobs(
        settings(tmp_path), config, now_utc=NOW + timedelta(minutes=6), client=notifier
    )
    assert not result["jobs"] and "InterruptedPipeline" in notifier.calls[0]


def test_missing_plan_is_visible_and_does_not_select_wrong_session(tmp_path):
    result = tick_session_jobs(
        settings(tmp_path), DailySchedule(session_jobs=("open30",)), now_utc=NOW, client=Notifier()
    )
    assert result["jobs"][0]["result_status"] == "BLOCKED"
    assert (
        find_session_plan(
            Path(tmp_path),
            signal_session=date(2026, 10, 12),
            order_session=date(2026, 10, 13),
            cutoff=NOW,
        )
        is None
    )


def test_pending_outbox_is_not_starved_by_recent_sent_rows(tmp_path):
    state = ScheduleState(tmp_path / "ops.sqlite")
    for index in range(7):
        state.enqueue(str(index), "destination", str(index), NOW)
    with state.db:
        state.db.execute("UPDATE telegram_outbox SET status='SENT' WHERE delivery_key!='0'")
    notifier = Notifier()
    state.deliver(notifier, "42", "destination", NOW, 3)
    assert notifier.calls == ["0"]
    state.close()


def test_plan_selection_rejects_future_and_wrong_order_session(tmp_path):
    import json

    root = tmp_path / "reports/daily/2026-10-12"
    for run, generated, order in [
        ("valid", "2026-10-13T12:30:00+00:00", "2026-10-13"),
        ("future", "2026-10-13T14:01:00+00:00", "2026-10-13"),
        ("wrong", "2026-10-13T13:59:00+00:00", "2026-10-14"),
    ]:
        folder = root / run
        folder.mkdir(parents=True)
        (folder / "premarket.json").write_text(
            json.dumps(
                {
                    "metadata": {
                        "signal_session": "2026-10-12",
                        "earliest_order_session": order,
                        "generated_at_utc": generated,
                        "news_cutoff_utc": generated,
                    },
                    "candidates": [],
                }
            )
        )
    selected = find_session_plan(
        tmp_path, signal_session=date(2026, 10, 12), order_session=date(2026, 10, 13), cutoff=NOW
    )
    assert selected[1].parent.name == "valid"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_jobs": ("open30", "open30")},
        {"session_jobs": ("premarket",), "premarket_minutes": 10},
        {"session_jobs": ("premarket",), "trigger": "premarket"},
    ],
)
def test_schedule_rejects_duplicate_or_impossible_jobs(kwargs):
    with pytest.raises(ValueError):
        DailySchedule(**kwargs)
