# Strategy Calibration

This branch calibrates the post-Phase-6 strategy before adding automation.
Calibration outputs are read-only diagnostics; they do not place orders and do
not bypass canonical Buy-the-Dip rules.

## Rotation diagnostics

Run:

```bash
quant decision rotation --date 2026-07-24
```

The command writes JSON, CSV, Markdown, and HTML artifacts under:

```text
data/reports/daily/<date>/<run_id>/rotation.*
```

The report groups the current AI watchlist and hedge overlay by:

- `universe_role`
- `sector`
- `theme`
- `subtheme`

For each group it calculates equal-weight 20-session and 60-session returns,
benchmark returns, relative returns, and a rotation label:

- `LEADING`: both short and long relative returns are positive.
- `IMPROVING`: short relative return is positive after long relative weakness.
- `WEAKENING`: short relative return is negative after long relative strength.
- `LAGGING`: both short and long relative returns are negative.
- `INSUFFICIENT_DATA`: there is not enough local price history.

The `portfolio_posture` summary is a manual review hint, not a trade signal. For
example, `AI_MOMENTUM_WEAKENING` means AI alpha still has long-window support,
but the 20-session spread versus hedge overlay has turned negative.

## Sector confirmation gate

The canonical market-regime benchmark remains `QQQ`. Sector ETFs such as `SMH`,
`SOXX`, `IGV`, and `DTCR` are not allowed to replace the broad-market weather
check, because that would turn the system into a narrow sector-beta strategy.
Instead, each AI-alpha candidate carries its configured `benchmark_etf` from the
watchlist and uses that ETF as a sector confirmation layer.

For AI-alpha candidates, the premarket calibration output now includes:

- `benchmark_etf`: the sector/theme ETF assigned in the watchlist.
- `sector_confirmation_pass`: whether the ETF backdrop supports manual review.
- `sector_confirmation_reasons`: the ETF symbol, trend state, and relative
  20/60-session context used for the gate.

If the candidate is core AI-alpha or AI-satellite and `sector_confirmation_pass`
is false, the row is not allowed into manual review. The resulting action is:

```text
WATCH_ONLY_SECTOR_CONFIRMATION_GATE
```

This means a strong individual stock reversal is still not enough when its
sector ETF is weakening or lagging. The intent is:

```text
QQQ/SPY = can the portfolio take growth risk?
SMH/SOXX/IGV/DTCR = is the candidate's industry wind supportive?
Stock = which specific setup is worth reviewing?
```

## Satellite scan layer

The core AI watchlist intentionally remains concentrated. Broader Futu raw-pool
coverage and selected EDA, advanced-packaging, metrology, test, and power
semiconductor names live in:

```text
configs/universe/ai_satellite_watchlist.yaml
```

Run the separate satellite scan with:

```bash
quant decision satellite --date 2026-08-07
```

This writes a separate report set:

```text
data/reports/daily/<date>/<run_id>/satellite.json
data/reports/daily/<date>/<run_id>/satellite_candidates.csv
data/reports/daily/<date>/<run_id>/satellite.md
data/reports/daily/<date>/<run_id>/satellite.html
```

The satellite command scans only `AI_SATELLITE` symbols, but computes calibration
context from the core AI, satellite, and hedge-overlay universes together. This
keeps broad-market and rotation diagnostics anchored while preventing satellite
names from diluting the core AI breadth metrics.

## Important boundary

The current AI watchlist is `CURRENT_SNAPSHOT_FORWARD_ONLY`. Rotation diagnostics
may use it for current and forward decision support, but historical diagnostics
with this universe must not be presented as an unbiased historical backtest.

## Next calibration steps

1. Add `STRICT` and `RELAXED` candidate tiers around the existing canonical
   rule set.
2. Compare tiers by candidate count, subsequent return distribution, drawdown,
   and false-positive rate.
3. Decide whether rotation posture should only annotate reports or also tighten
   candidate sizing/cash posture.

## Three-level diagnostics

Run:

```bash
quant decision hierarchy --date 2026-07-24
```

This command builds the calibration context across three layers:

1. Market proxies: `QQQ` and `SPY`.
2. Sector/theme proxies: benchmark ETFs such as `SMH`, `SOXX`, `IGV`, `DTCR`,
   `XLP`, `XLV`, `XLU`, and `XLF`.
3. Stocks: watchlist members, with mega/high-liquidity names tagged as
   `leader_stock`.

The hierarchy report uses these trend states:

- `DRIFT_DOWN`: persistent short-window weakness, falling MA20, and a high
  down-day ratio.
- `WASHOUT`: large drawdown or oversold RSI while price is still falling.
- `REVERSAL_ATTEMPT`: short-window rebound with price reclaiming MA20.
- `CONFIRMED_REVERSAL`: positive 20-session return, positive relative return,
  price above MA20, and rising MA20.
- `UPTREND`: positive 20-session and 60-session returns with price above MA20.
- `WEAKENING`: short-window weakness after longer-window strength.
- `LAGGING`: no recovery or leadership signal.

The `strategy_context` section converts diagnostics into a non-binding
calibration posture:

- `DEFENSIVE`: avoid new AI alpha exposure; review cash and defensive overlays.
- `STRICT`: only the strictest baseline entries deserve manual review.
- `BASELINE`: use canonical rules without extra pressure.
- `RELAXED_WATCHLIST`: AI leaders show reversal breadth, so relaxed candidates
  may be reviewed, but this is not permission to auto-buy.

This layer is intentionally read-only. It calibrates the daily workbench context
before the canonical Buy-the-Dip rule thresholds are changed.

## Tiered candidate scan

`quant decision premarket` now attaches read-only tiered candidates by default.
Disable this with:

```bash
quant decision premarket --date 2026-07-24 --no-calibration
```

The canonical `candidates` section remains the baseline Buy-the-Dip output. The
additional `calibration_candidate_tiers` section answers a different question:
"What would strict, baseline, and relaxed rules show under the current market
context?"

Tier definitions:

- `STRICT`: requires stronger 60-session relative strength, a healthier MA50
  slope, a narrower dip window, and a tighter RSI/ATR-drawdown setup.
- `BASELINE`: the canonical Buy-the-Dip rules used by scan and backtest.
- `RELAXED`: allows weaker relative strength, a wider dip window, and a wider
  RSI/ATR-drawdown range, but still requires daily confirmation and news vetoes.

Relaxed candidates must also pass a quality gate before they are allowed into
manual review. The current calibrated gate is intentionally narrow: the symbol
must be a `leader_stock` and its hierarchy state must be `REVERSAL_ATTEMPT`,
`CONFIRMED_REVERSAL`, or `UPTREND`.

The report still records supporting context when available:

- its theme or sector rotation row is `LEADING`/`IMPROVING`, or has positive
  20-session relative return;
- for AI alpha rows, AI alpha is stronger than hedge overlay on the 20-session
  spread.

Those supporting context fields explain the setup, but they do not by themselves
permit manual review. If the leader-stock condition is missing, the row remains
visible as `WATCH_ONLY_RELAXED_QUALITY_GATE`.

The strategy context controls how these rows should be interpreted:

- `DEFENSIVE`: AI alpha rows are deferred; quality-gated hedge overlay rows may
  be reviewed.
- `STRICT`: only `STRICT` rows are manual-review candidates.
- `BASELINE`: `STRICT` and `BASELINE` rows are manual-review candidates.
- `RELAXED_WATCHLIST`: quality-gated `RELAXED` rows may be reviewed as watchlist
  candidates, not automatic buy candidates.

## Defensive overlay gate

Hedge overlay rows have their own quality gate. The defensive overlay is not a
generic permission to buy every low-beta or diversifying stock.

Manual review is currently limited to:

- `defensive_healthcare` or `defensive_staples`;
- `leader_stock` symbols only;
- symbol state in `REVERSAL_ATTEMPT`, `CONFIRMED_REVERSAL`, or `UPTREND`;
- positive theme rotation.

`defensive_financials` and `defensive_utilities` remain useful as context, but
they are not manual-review overlays by default. YTD calibration showed that
financial diversifiers and rate-sensitive utilities were a drag when treated as
Buy-the-Dip defensive entries.

## Calibration closure

Run:

```bash
python scripts/strategy_calibration_closure.py
```

The closure script is a read-only research tool. It does not write raw Parquet,
SQLite trading state, model registry rows, or production strategy config. It
summarizes:

- 2026 YTD current calibration;
- hand-picked regime slices for the February/March and June/July drawdowns and
  the late-March/late-July reversal windows;
- parameter sensitivity for current, tighter-relaxed, and looser-relaxed rule
  variants.

Latest local run:

```text
data/reports/simulations/calibration_closure/20260808T154501Z/summary.md
```

Important scope limits:

- Universe policy is `CURRENT_SNAPSHOT_FORWARD_ONLY`.
- Results are candidate-quality diagnostics from signal-session close to +5
  benchmark sessions close, not executable fill P&L.
- This should not be presented as an unbiased historical backtest because the
  watchlist is a current selected universe.

Summary from the latest run:

| slice / variant | candidate rows | manual rows | matured manual | avg 5d | median 5d | win | avg relative |
|---|---:|---:|---:|---:|---:|---:|---:|
| current YTD | 700 | 45 | 44 | 0.49% | 0.92% | 54.55% | 0.16% |
| Feb/Mar drift-down | 289 | 11 | 11 | -1.93% | -1.32% | 36.36% | -1.06% |
| Jun/Jul drift-down | 195 | 10 | 9 | -0.04% | 2.31% | 66.67% | 2.04% |
| relaxed tighter | 311 | 19 | 19 | 1.36% | 2.31% | 63.16% | 1.37% |
| relaxed looser | 853 | 57 | 56 | -0.45% | -0.42% | 46.43% | -0.61% |

Closure decision:

- Merge the current calibration as conservative decision support.
- Do not loosen `RELAXED`; the looser variant expands candidates and turns
  manual-review expectancy negative.
- Consider using the tighter-relaxed result as the preferred mental model for
  real money: fewer candidates, better candidate quality, but only 19 matured
  manual-review rows so still below a high-confidence sample.
- Treat February/March-style persistent drawdowns as the main weakness. In that
  regime, the current manual-review set was negative, so the system should bias
  toward `STRICT`, smaller size, or no new AI alpha rather than trying to catch
  every dip.
- Late-March and late-July reversal slices had too few mature candidates in the
  current local dataset to justify custom reversal-specific thresholds. Do not
  optimize rules to those two windows.

Final recommendation: keep the strategy-calibration branch as a conservative
manual-review overlay, not an alpha expansion. After merging, continue
forward-only observation before enabling automation or increasing risk.
