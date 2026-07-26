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

The position check writes JSON, CSV, and Markdown under
`data/reports/daily/<date>/<run_id>/`. Actions are decision-support labels:
`HOLD`, `EXIT_STOP`, `EXIT_TARGET`, `EXIT_TIME`, `DEFENSIVE_ROTATION`, or
`REVIEW_MISSING_PLAN`.

## Run the local dashboard

```bash
streamlit run apps/decision_dashboard.py
```

The dashboard reads daily reports and `data/db/operations.sqlite`. It is local
and human-in-the-loop only.
