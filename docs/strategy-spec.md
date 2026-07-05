# Buy-the-Dip v1 strategy specification

## Hypothesis

When QQQ remains in a long-term uptrend, liquid stocks with positive 60-session
relative strength may exhibit short-term mean reversion after a moderate,
ATR-scaled pullback and a subsequent daily confirmation.

This is a rules baseline, not a claim that the edge has been established.

## Universe

Phase 3 uses explicitly supplied symbols and records the universe as:

```text
CURRENT_SNAPSHOT_FORWARD_ONLY
```

The current list is not point-in-time membership data. Results therefore carry
survivorship and selection bias and must not be presented as an unbiased
historical simulation.

## Canonical features

`features/technical.py` is the only implementation used by scanning and
backtesting:

- MA5, MA20, MA50, and MA200;
- 20-session MA50 slope;
- RSI14 using Wilder smoothing;
- ATR20 and ADX14 using Wilder smoothing;
- 20-session closing high and drawdown;
- 20-session average dollar volume;
- 60-session return relative to QQQ.

All rolling and shifted values are trailing. Changing a future price must not
change an earlier feature row.

## Market regime

- `GREEN`: QQQ is above MA200 and its MA50 slope is non-negative.
- `YELLOW`: QQQ is above MA200 and its MA50 decline is within the configured
  tolerance.
- `RED`: all other states; no new candidates.

Yellow signals receive half the normal risk budget.

## Candidate rules

On T-1, the stock must satisfy all of:

- 20-session average dollar volume at least $20 million;
- close above MA200;
- MA50 slope at least -2%;
- 60-session return relative to QQQ non-negative;
- 5%–15% drawdown from its 20-session closing high;
- RSI14 from 30 through 45;
- close below MA20;
- pullback distance from 1 through 3 ATR20.

On T, confirmation requires either:

- close crosses back above MA5; or
- close exceeds the previous session high.

The T close is the data cutoff. The signal is created only after the official
close plus the configured data delay. The earliest fill is the T+1 official
open. Signal IDs are deterministic for a symbol and signal session.

## Execution

The first baseline uses T+1 market-on-open simulation:

- cancel if the opening gap is above +3% or below -5% from signal close;
- buy fill = open plus 10 bps slippage;
- sell fill = trigger price minus 10 bps slippage;
- commission = 2 bps on each side;
- initial stop = entry minus 2 ATR20;
- profit target = entry plus 3 ATR20;
- time exit at the close of the fifth held session;
- if stop and target are both inside one daily bar, stop occurs first;
- a gap through the stop fills at the worse opening price.

New positions are sized at the open before any later stop, target, or closing
proceeds are credited. This prevents intraday cash from funding an earlier
opening order.

## Portfolio constraints

- initial cash: $100,000;
- risk budget: 0.5% of current equity per trade;
- cash reserve: at least 40% of current equity;
- maximum position: 15% of equity;
- maximum gross exposure: 60% of equity;
- maximum concurrent positions: 5;
- no overlapping position in the same symbol.

Candidates for the same session are ordered by 60-session relative strength,
then symbol. LightGBM is not used in Phase 3.

## Maturity and accounting

Positions without enough future sessions remain open. They are marked to market
in the equity curve but are not converted into closed T+N returns. Closed trade
records include signal, order, entry and exit fill identifiers, times, prices,
commissions, quantity, regime, exit reason, gross PnL, and net PnL.
Each summary also stores the complete validated configuration and its SHA-256
hash.

When no positions remain, final realized equity must equal initial cash plus the
sum of trade-level net PnL.

## Robustness grid

The fixed grid tests:

- baseline friction;
- 1.5x and 2x commission and slippage;
- stop distance at 0.8x and 1.2x;
- target distance at 0.8x and 1.2x.

The grid is diagnostic, not an optimizer. Parameter changes should form a
performance plateau rather than selecting the best row after seeing results.

Sample interpretation:

- fewer than 30 closed trades: `INSUFFICIENT`;
- 30–99: `PRELIMINARY`;
- 100–199: `ADEQUATE`;
- 200 or more: `HIGH_CONFIDENCE`.
