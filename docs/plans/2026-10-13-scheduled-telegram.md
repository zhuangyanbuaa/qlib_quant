# Scheduled Daily Run and Telegram Implementation Plan

**Goal:** Run the existing manual-news pipeline once per NYSE session at actual
close +60 minutes and notify the user's Telegram, including degraded/blocked results.

**Architecture:** A launchd interval job invokes a calendar-aware tick. SQLite
persists scheduled attempts and a notification outbox; process exclusion prevents
overlap. Existing canonical pipeline keeps data budgets, provenance and price gates.
Secrets stay in .env, never in plist/arguments/reports. Telegram delivery is explicit
user-authorized. Human news research remains a manual step after prompt generation.

**Tech Stack:** exchange-calendars/IANA timezone clocks, Python stdlib HTTPS, SQLite,
launchd/plistlib, existing Pydantic/Typer and pytest.

User selections: manual news; actual exchange close +60 minutes. Bot credentials
are configured locally and live connection verification has succeeded.
Use Code/writing-plans and automation-workflows; preserve current checkout. No
automatic commits. Final checkpoint follows all three tasks and setup validation.

## Task 1: Telegram and settings

Create `operations/telegram.py`; extend Settings/.env.example. Use SecretStr bot
token, redacted public readiness, getMe/getUpdates and sendMessage over HTTPS.
Never log provider bodies or token-bearing URL errors. Plain text within Telegram
limits; send summaries/paths, not research/holdings attachments. 429 respects durable
retry_after. Ambiguous delivery or a crash after send reservation is not auto-resent.
Test fixed transport fixtures, secrecy, malformed responses and bounded delivery.

## Task 2: Calendar scheduler and durable state

Create `operations/scheduling.py` and `configs/operations/daily.yaml`. Trigger from
XNYS close/open, not Tokyo clock. Default close+60, manual mode, polling every minute,
at most two pipeline attempts with 15-minute backoff before next actual open.
Holidays/early close/DST/weekend catch-up must behave correctly; no historical stale
catch-up beyond the order window. Persist daily idempotence, crash recovery and
independent notification retry. Acquire a separate scheduler lock, then reuse pipeline
lock. Missing Telegram settings block unattended runs until configured. Rotate bounded
local logs. Test duplicate ticks, process contention, retries, failures and notifications.

## Task 3: CLI, launchd, docs and real setup

Add automation tick/status/install/uninstall and telegram check/find-chat/test commands.
Generate user LaunchAgent with RunAtLoad + StartInterval and absolute interpreter,
project cwd and config paths. No credentials in plist. Verify registered service and
next trigger. On user's bot-ready reply, discover only an unambiguous private chat,
write chat ID locally and send the authorized test notification. Install the agent;
without credentials it remains registered but paused by readiness, not falsely active.
Document BotFather/start, env fields, operation, retry/delivery limits and sleep/login
behavior. Run full tests/Ruff/diff and an isolated scheduled pipeline smoke. Do not
implement intraday jobs, weekly review, automatic news research or order execution.

## Execution record — 2026-10-13

Tasks 1–3 implemented. Full suite: **312 tests passed**; Ruff and diff checks passed.
Fixed transport tests cover secret redaction, bot/private-chat discovery, malformed
receipts, definitive rate limiting and ambiguous delivery. Calendar tests cover DST,
early close, holiday/weekend catch-up and expired windows. State tests cover daily
deduplication, bounded acquisition retries, separate notification retries, recovery
and concurrent exclusion. Integration smoke used fixed local Parquet through the
canonical pipeline, generated a manual research package and delivered to a fake
transport once; repeat tick performed no extra acquisition/send.

Installed `com.qlib-quant.daily.212ae437` under the user's LaunchAgents directory.
`launchctl print` confirms registration, a 60-second interval and successful entry
into the tick command. Before credentials were configured it deliberately exited 2
with BLOCKED_CONFIGURATION / telegram_not_configured.

The user subsequently configured the token and chat ID in the ignored mode-0600
.env. Live getMe verification returned VALID_BOT, and the authorized connection
test returned SENT with message_id 9. getUpdates contained no private messages;
the user's already configured destination was preserved rather than inferred or
overwritten. No secret was requested in chat or written to the documentation.

The installed interval service automatically started a catch-up run for NYSE
2026-10-12. Live price acquisition succeeded for all 138 requested symbols, with
0 failures, 0 stale symbols and 552 appended rows. Quality validation and report
generation passed. The scheduled job finished DONE on its first attempt; the
pipeline result is DEGRADED because manual news coverage remains
PENDING_MANUAL_REVIEW / UNKNOWN. It produced 2 research candidates, 0 execution
eligible candidates and a manual research package covering 7 symbols. The daily
index and all four manual research artifacts exist locally. The durable outbox
records SENT on its first attempt, with Telegram message_id 10 and no error.
This verifies live acquisition through report generation and real Telegram delivery,
separately from the connection test. Human news research remains pending.

Selected policy: manual news and actual NYSE close+60 minutes. Next normal trigger
reported by the installed service: 2026-10-13 21:00 UTC / 2026-10-14 06:00 Asia/Tokyo.
The most recent completed session can catch up before its next actual open when
configuration becomes ready. No credentials appear in the plist, CLI arguments or
logs. Runtime state, logs and .env remain ignored. No new commit/push was made.
