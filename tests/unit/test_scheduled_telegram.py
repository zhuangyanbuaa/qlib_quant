import json
import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant_system.operations.launchd import agent_definition, install_agent, uninstall_agent
from quant_system.operations.scheduling import (
    DailySchedule,
    ScheduleState,
    rotate_logs,
    schedule_windows,
    scheduler_lock,
    tick_daily,
)
from quant_system.operations.telegram import TelegramClient, TelegramError
from quant_system.settings import Settings

TOKEN = "123456:" + "fixture_not_a_real_token" * 2


def settings(tmp_path):
    return Settings(
        _env_file=None, data_dir=tmp_path, telegram_bot_token=TOKEN, telegram_chat_id="42"
    )


class Notifier:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def send(self, chat, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return 7


def test_telegram_preserves_secrecy_and_private_chat_discovery():
    calls = []

    def transport(url, payload, timeout):
        calls.append((url, payload))
        if url.endswith("getMe"):
            return {"ok": True, "result": {"is_bot": True, "username": "fixture_bot"}}
        if url.endswith("getUpdates"):
            return {
                "ok": True,
                "result": [
                    {
                        "message": {
                            "chat": {"id": 42, "type": "private"},
                            "from": {"id": 42, "is_bot": False},
                        }
                    },
                    {
                        "message": {
                            "chat": {"id": -4, "type": "group"},
                            "from": {"id": 42, "is_bot": False},
                        }
                    },
                ],
            }
        return {"ok": True, "result": {"message_id": 7}}

    client = TelegramClient(TOKEN, transport=transport)
    assert client.check()["status"] == "VALID_BOT"
    assert client.private_chats() == ["42"]
    assert client.send("42", "fixture") == 7
    assert "offset" not in calls[1][1]
    assert "parse_mode" not in calls[-1][1]
    configured = Settings(_env_file=None, telegram_bot_token=TOKEN, telegram_chat_id="42")
    assert configured.telegram_configured
    assert TOKEN not in repr(configured) and TOKEN not in json.dumps(configured.public_config())


@pytest.mark.parametrize(
    "payload",
    [{"ok": False, "description": TOKEN, "error_code": 401}, {"ok": True, "result": {}}, [TOKEN]],
)
def test_telegram_errors_and_missing_receipts_are_redacted(payload):
    client = TelegramClient(TOKEN, transport=lambda *args: payload)
    with pytest.raises(TelegramError) as caught:
        client.send("42", "fixture")
    assert TOKEN not in str(caught.value)


def test_transport_exception_cannot_leak_token_url():
    def broken(*args):
        raise OSError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")

    with pytest.raises(TelegramError) as caught:
        TelegramClient(TOKEN, transport=broken).send("42", "fixture")
    assert caught.value.ambiguous and TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-03-06T22:00:00+00:00", "2026-03-06"),
        ("2026-03-09T21:00:00+00:00", "2026-03-09"),
        ("2026-11-27T19:00:00+00:00", "2026-11-27"),
    ],
)
def test_actual_close_handles_dst_and_early_close(now, expected):
    active, _ = schedule_windows(DailySchedule(), datetime.fromisoformat(now))
    assert active["session"] == expected and active["due"] == datetime.fromisoformat(now)


def test_holidays_and_expired_windows_do_not_create_new_daily_runs():
    active, upcoming = schedule_windows(DailySchedule(), datetime(2026, 7, 3, 21, tzinfo=UTC))
    assert active["session"] == "2026-07-02"
    assert upcoming["session"] == "2026-07-06"
    active, _ = schedule_windows(DailySchedule(), datetime(2026, 7, 6, 14, tzinfo=UTC))
    assert active is None
    active, _ = schedule_windows(
        DailySchedule(trigger="premarket"), datetime(2026, 3, 9, 12, 30, tzinfo=UTC)
    )
    assert active["session"] == "2026-03-06"


def test_scheduler_runs_once_and_notifies_degraded_result_once(tmp_path):
    from test_digest_notifications import fixture_digest

    calls, notifier = [], Notifier()

    def pipeline(*args, **kwargs):
        calls.append(kwargs)
        digest = fixture_digest(2)
        digest.metadata["signal_session"] = kwargs["signal_session"].isoformat()
        return {
            "status": "DEGRADED",
            "digest": digest.model_dump(mode="json"),
            "summary": {"candidate_count": 2},
            "manual_research": {"symbol_count": 3, "artifacts": {"prompt": "fixture.md"}},
        }

    now = datetime(2026, 10, 12, 21, tzinfo=UTC)
    first = tick_daily(
        settings(tmp_path), DailySchedule(), now_utc=now, pipeline=pipeline, client=notifier
    )
    second = tick_daily(
        settings(tmp_path),
        DailySchedule(),
        now_utc=now + timedelta(minutes=1),
        pipeline=pipeline,
        client=notifier,
    )
    assert first["status"] == second["status"] == "DONE"
    assert len(calls) == len(notifier.calls) == 1
    assert calls[0]["manual_news"] and "DEGRADED" in notifier.calls[0]
    assert "待新闻审核" in notifier.calls[0]
    assert "fixture.md" not in notifier.calls[0]


def test_pipeline_retries_are_bounded_and_notify_failures(tmp_path):
    calls, notifier = [], Notifier()

    def failed(*args, **kwargs):
        calls.append(True)
        return {"status": "BLOCKED", "blocked_reasons": ["critical_price_missing"]}

    now = datetime(2026, 10, 12, 21, tzinfo=UTC)
    for minutes in (0, 1, 15, 30):
        tick_daily(
            settings(tmp_path),
            DailySchedule(),
            now_utc=now + timedelta(minutes=minutes),
            pipeline=failed,
            client=notifier,
        )
    assert len(calls) == 2 and len(notifier.calls) == 2
    assert "BLOCKED" in notifier.calls[0] and "没有可用的推荐报告" in notifier.calls[0]


def test_notification_429_retries_without_rerunning_pipeline(tmp_path):
    notifier = Notifier(TelegramError("telegram_rate_limited", retry_after=120))
    calls = []

    def pipeline(*args, **kwargs):
        calls.append(True)
        return {"status": "COMPLETED"}

    now = datetime(2026, 10, 12, 21, tzinfo=UTC)
    first = tick_daily(
        settings(tmp_path), DailySchedule(), now_utc=now, pipeline=pipeline, client=notifier
    )
    assert first["notifications"][0]["status"] == "RETRY"
    tick_daily(
        settings(tmp_path),
        DailySchedule(),
        now_utc=now + timedelta(seconds=60),
        pipeline=pipeline,
        client=notifier,
    )
    assert len(notifier.calls) == 1
    notifier.error = None
    last = tick_daily(
        settings(tmp_path),
        DailySchedule(),
        now_utc=now + timedelta(seconds=120),
        pipeline=pipeline,
        client=notifier,
    )
    assert last["notifications"][0]["status"] == "SENT" and len(calls) == 1


def test_ambiguous_delivery_is_not_repeated(tmp_path):
    notifier = Notifier(TelegramError("unknown_delivery", ambiguous=True))
    now = datetime(2026, 10, 12, 21, tzinfo=UTC)
    for minutes in (0, 1, 20):
        result = tick_daily(
            settings(tmp_path),
            DailySchedule(),
            now_utc=now + timedelta(minutes=minutes),
            pipeline=lambda *args, **kwargs: {"status": "COMPLETED"},
            client=notifier,
        )
        assert result["notifications"][0]["status"] == "UNKNOWN"
    assert len(notifier.calls) == 1


def test_crash_recovery_and_scheduler_exclusion(tmp_path):
    now = datetime(2026, 10, 12, 21, tzinfo=UTC)

    def crash(*args, **kwargs):
        raise SystemExit("fixture crash")

    with pytest.raises(SystemExit):
        tick_daily(
            settings(tmp_path), DailySchedule(), now_utc=now, pipeline=crash, client=Notifier()
        )
    calls = []
    recovered = tick_daily(
        settings(tmp_path),
        DailySchedule(),
        now_utc=now + timedelta(minutes=15),
        pipeline=lambda *args, **kwargs: calls.append(True) or {"status": "COMPLETED"},
        client=Notifier(),
    )
    assert recovered["attempts"] == 2 and len(calls) == 1
    with scheduler_lock(tmp_path):
        assert (
            tick_daily(settings(tmp_path), DailySchedule(), now_utc=now)["status"] == "SKIPPED_BUSY"
        )


def test_missing_configuration_never_runs_or_sends_and_logs_rotate(tmp_path):
    calls = []
    result = tick_daily(
        Settings(_env_file=None, data_dir=tmp_path),
        DailySchedule(),
        pipeline=lambda *args, **kwargs: calls.append(True),
    )
    assert result["status"] == "BLOCKED_CONFIGURATION" and not calls
    log = tmp_path / "logs/automation.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("x" * 1001)
    rotate_logs(log.parent, DailySchedule(log_max_bytes=1000))
    assert log.with_name("automation.log.1").exists() and not log.exists()


def test_launchd_has_calendar_polling_and_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr("quant_system.operations.launchd.sys.platform", "darwin")
    config = Path("configs/operations/daily.yaml").resolve()
    definition = agent_definition(settings(tmp_path), config)
    assert definition["StartInterval"] == 60 and "StartCalendarInterval" not in definition
    assert TOKEN not in plistlib.dumps(definition).decode()
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if "print" in args else 0)

    result = install_agent(settings(tmp_path), config, directory=tmp_path / "agents", runner=runner)
    assert result["status"] == "INSTALLED"
    assert any("bootstrap" in call for call in calls)
    uninstall_agent(directory=tmp_path / "agents", runner=runner)
    assert not Path(result["plist_path"]).exists()


def test_interrupted_send_reservation_is_never_resent(tmp_path):
    state = ScheduleState(tmp_path / "operations.sqlite")
    now = datetime(2026, 10, 12, 21, tzinfo=UTC)
    state.enqueue("k", "destination", "fixture", now)
    with state.db:
        state.db.execute("UPDATE telegram_outbox SET status='SENDING'")
    notifier = Notifier()
    assert state.deliver(notifier, "42", "destination", now, 3)[0]["status"] == "UNKNOWN"
    assert not notifier.calls
    state.close()
