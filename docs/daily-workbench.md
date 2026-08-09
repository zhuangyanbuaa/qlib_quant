# Daily Workbench

Phase 6 turns the project into a daily manual-trading workbench. The system still
does not place broker orders.

## Record manual fills

```bash
quant journal add-fill \
  --symbol NVDA \
  --side BUY \
  --quantity 10 \
  --price 120.50 \
  --stop-price 112.00 \
  --target-price 138.00 \
  --fill-time 2026-07-27T13:30:00+00:00
```

Show reconstructed holdings:

```bash
quant journal positions
```

## Check exits

```bash
quant decision positions --date 2026-07-24
```

The position check writes JSON, CSV, Markdown, and HTML under
`data/reports/daily/<date>/<run_id>/`. Actions are decision-support labels:
`HOLD`, `EXIT_STOP`, `EXIT_TARGET`, `EXIT_TIME`, `DEFENSIVE_ROTATION`, or
`REVIEW_MISSING_PLAN`.

## Generate a manual research list

For hand-trading support, generate a wider research list without widening the
actual trade gate:

```bash
quant decision research-list --date 2026-08-07 --no-news-risk --max-symbols 20
```

The command first builds the usual premarket calibration context, then writes:

```text
data/reports/daily/<date>/<run_id>/research.json
data/reports/daily/<date>/<run_id>/research_candidates.csv
data/reports/daily/<date>/<run_id>/research.md
data/reports/daily/<date>/<run_id>/news_research_prompt.md
```

Research buckets are intentionally broader than trade actions:

- `ACTIONABLE_CANDIDATE`: passed the existing action/review path.
- `RESEARCH_WATCHLIST`: technically interesting enough to research, but still
  requires manual judgment.
- `BLOCKED_BUT_INTERESTING`: has a setup worth reading about, but was blocked by
  defensive context, sector confirmation, or quality gates.
- `DEFENSIVE_RESEARCH`: defensive overlay context worth checking.

Copy `news_research_prompt.md` into a browsing-capable Codex session to summarize
recent news for the listed symbols. The prompt requires source links and dates
and asks for `重点研究` / `继续观察` / `暂时跳过`, not buy/sell instructions.

## Occasional 2x leveraged ETF overlay strategy

If you occasionally consider 2x long ETFs, use the separate subsidiary strategy:

```bash
quant decision leverage-overlay \
  --date 2026-08-07 \
  --underlying MU \
  --leveraged-etf MUU \
  --sector-etf SOXX \
  --catalyst-review-needed
```

The command writes:

```text
data/reports/daily/<date>/<run_id>/leverage_overlay.json
data/reports/daily/<date>/<run_id>/leverage_overlay.md
data/reports/daily/<date>/<run_id>/leverage_overlay_prompt.md
```

Actions are deliberately manual:

- `ALLOW_MANUAL_REVIEW`: all checklist items, including catalyst, passed.
- `NEED_CATALYST_REVIEW`: technical and risk gates passed, but catalyst/news
  still needs manual validation.
- `COMMON_STOCK_PREFERRED`: setup is interesting, but not strong enough for 2x.
- `NO_2X_TRADE`: one or more critical leverage gates failed.

This overlay is intentionally outside the main Buy-the-Dip strategy gate. It
does not add rows to `premarket.candidates`, does not create paper fills, and
does not override sector confirmation or manual review discipline. The full
framework lives in:

```text
docs/2x-leveraged-etf-framework.md
```

## Refresh and open gates

Run a pre-open refresh from the latest premarket report:

```bash
quant decision preopen-refresh
```

Run T+30 or T+60 gate checks:

```bash
quant decision open-gate --minutes 30
quant decision open-gate --minutes 60
```

If you provide a broker snapshot CSV/JSON with `symbol,last_price,news_risk`,
the gate can return `KEEP`, `DEFER`, or `CANCEL`. Without a snapshot it returns
conservative `DEFER` rows rather than pretending intraday data exists.

## Forward paper trading

Create paper entries from the latest premarket report:

```bash
quant paper update
```

Advance open paper positions and record paper exits when stop/target/time rules
trigger:

```bash
quant paper advance --date 2026-07-24
quant paper positions
quant paper fills
```

Paper fills are stored separately from manual fills in `paper_fills`.

## Run the local dashboard

```bash
streamlit run apps/decision_dashboard.py
```

The dashboard reads daily reports and `data/db/operations.sqlite`. It is local
and human-in-the-loop only.
