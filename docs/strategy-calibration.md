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
