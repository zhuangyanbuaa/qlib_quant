# Daily Runbook

This runbook is the daily manual workflow for the local decision-support system.
It is intentionally human-in-the-loop: the repo generates reports, research
lists, risk context, and prompts; it does not place broker orders.

Use NYSE session dates in `YYYY-MM-DD` form. If `--date` is omitted, most daily
commands use the latest completed NYSE session from the exchange calendar.

If the `quant` command is not available in your shell, use `uv run quant ...` or
run the one-command wrapper:

```bash
python scripts/run_daily_workbench.py
```

## Daily sequence

### 1. Refresh price data

Run this after the latest US session has completed and Yahoo data should be
available:

```bash
uv run quant data update-prices --all-stored
```

What it does:

- updates every symbol already present in DuckDB;
- respects provider rate limits, retry/backoff, and daily call budgets;
- appends new raw Parquet records instead of mutating old successful records;
- refreshes DuckDB analytical views;
- writes a quality report under `data/reports/quality/`.

If you only want to refresh one configured universe:

```bash
uv run quant data update-prices --universe configs/universe/ai_watchlist.yaml
uv run quant data update-prices --universe configs/universe/ai_satellite_watchlist.yaml
uv run quant data update-prices --universe configs/universe/hedge_overlay.yaml
```

Use `--all-stored` for the normal daily routine after initial backfill.

### 2. Optional: refresh local news

News is not fetched automatically by the dashboard or the one-command daily
runner. If you want the news-risk layer to use fresh provider data, fetch a
bounded symbol set first:

```bash
uv run quant data update-news \
  --symbols NVDA,AMD,AVGO,ASML,TSM,MU,ARM,MRVL,ANET,VRT \
  --start 2026-08-03T00:00:00Z
```

What it does:

- fetches provider news and SEC/company events when configured;
- applies rate limits and daily budgets from `configs/sources/news.yaml`;
- writes point-in-time raw news/event Parquet;
- classifies events with deterministic rules or optional FinBERT;
- marks timeout/model failures as `DEGRADED` instead of silently trusting stale
  inference.

For normal usage, keep this list small: focus on existing positions, top
premarket candidates, and names you are likely to manually research. If you do
not fetch news, run the daily workbench with `--no-news-risk`.

### 3. Generate the daily workbench

Conservative default when news is not refreshed:

```bash
python scripts/run_daily_workbench.py --no-news-risk
```

With local news-risk enabled after step 2:

```bash
python scripts/run_daily_workbench.py --news-risk
```

For a specific NYSE session:

```bash
python scripts/run_daily_workbench.py --date 2026-08-07 --no-news-risk
```

What it does:

- generates the premarket Buy-the-Dip plan;
- attaches market/sector/stock rotation context;
- attaches read-only LightGBM rank context when available;
- generates a broader manual research list;
- writes a Codex news-research prompt for the selected symbols;
- scans the separate 2x leveraged overlay radar;
- checks manual positions if recorded;
- writes `daily_index.json` and `daily_index.md`.

Primary output:

```text
data/reports/daily/<date>/<run_id>/daily_index.md
data/reports/daily/<date>/<run_id>/daily_index.json
```

Open `daily_index.md` first. It is the table of contents for the day.

### 4. Review the generated research prompt

Open:

```text
data/reports/daily/<date>/<run_id>/news_research_prompt.md
```

Copy it into a browsing-capable Codex session and ask for recent-news
summaries. The prompt is deliberately not a buy/sell instruction; it asks for
source-linked context and a manual classification such as:

- `重点研究`
- `继续观察`
- `暂时跳过`

Use this step for names in the research list, satellite watchlist, and any
2x/generic leverage attention rows.

### 5. Optional: open the local dashboard

```bash
streamlit run apps/decision_dashboard.py
```

What it shows:

- `Today`: `daily_index` summary and next manual steps;
- `Candidates`: premarket and calibration candidates;
- `Research`: wider manual research list;
- `News`: local point-in-time news already stored in Parquet;
- `2x Overlay`: concrete 2x products plus generic risk-on leverage watch rows;
- `Portfolio`: manual position checks;
- `Reports`: generated Markdown artifacts;
- `Data Health`: report metadata and quality context.

Stop it with `Ctrl-C` in the terminal where Streamlit is running.

### 6. Optional: pre-open and open gates

Before market open:

```bash
uv run quant decision preopen-refresh
```

T+30 and T+60 checks:

```bash
uv run quant decision open-gate --minutes 30
uv run quant decision open-gate --minutes 60
```

Without a broker/intraday snapshot, these gates are conservative and may return
`DEFER`. That is intentional: the system must not pretend daily bars contain
point-in-time intraday information.

### 7. Optional: update manual journal and paper ledger

Record an actual manual fill:

```bash
uv run quant journal add-fill \
  --symbol NVDA \
  --side BUY \
  --quantity 10 \
  --price 120.50 \
  --stop-price 112.00 \
  --target-price 138.00 \
  --fill-time 2026-08-07T13:30:00+00:00
```

Check reconstructed manual positions:

```bash
uv run quant journal positions
```

Check whether existing manual positions should be held, reduced, or exited:

```bash
uv run quant decision positions --date 2026-08-07
```

Forward paper trading:

```bash
uv run quant paper update
uv run quant paper advance --date 2026-08-07
uv run quant paper positions
```

Manual fills and paper fills are separate SQLite tables. Paper results are for
workflow calibration only.

## Weekly review

Once per week, run or inspect the latest calibration artifacts:

```bash
python scripts/strategy_calibration_closure.py
```

Review:

- whether `RELAXED` candidates helped or added noise;
- whether defensive overlay warnings arrived early enough;
- whether `generic_2x_watch` rows were useful or too noisy;
- whether the news prompt changed any manual decision;
- whether any data-quality or stale-source issue blocked recommendations.

Do not promote looser rules from a single good week. Prefer robust behavior
across drawdowns, reversals, and flat markets.

## Troubleshooting

### `ModuleNotFoundError: No module named 'quant_system'`

Use one of these:

```bash
uv run quant --help
python scripts/run_daily_workbench.py --no-news-risk
PYTHONPATH=src .venv/bin/python -m quant_system --help
```

### Empty dashboard News tab

This means no local news/events were found for the report symbols and cutoff.
Run `quant data update-news --symbols ...` first, then rerun the workbench with
`--news-risk`.

### Price update is blocked

Read the quality report path printed by `update-prices`. A missing or stale
critical price source must block recommendations; do not override it by hand
unless you first fix the source data and rerun the report.

### Which report should I open first?

Open:

```text
data/reports/daily/<date>/<run_id>/daily_index.md
```

It links the day’s premarket plan, research list, leverage overlay, position
check, and recommended next manual steps.
