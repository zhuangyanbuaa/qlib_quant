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
