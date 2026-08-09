# 2x Leveraged ETF Manual Trading Framework

This document defines the subsidiary, manual-only 2x long ETF overlay strategy,
such as single-stock or thematic 2x products. It is intentionally not part of
the main Buy-the-Dip gate.

Core principle:

> Leverage is only used after trend confirmation. It is not used for bottom
> fishing, averaging down, emotional chase trades, or rescuing a wrong view.

## Repository boundary and command

Run the overlay separately:

```bash
quant decision leverage-overlay \
  --date 2026-08-07 \
  --underlying MU \
  --leveraged-etf MUU \
  --sector-etf SOXX \
  --catalyst-review-needed
```

It writes `leverage_overlay.json`, `leverage_overlay.md`, and
`leverage_overlay_prompt.md` under the daily report directory.

- 2x ETF ideas are manual-only tactical overlays.
- They do not bypass canonical Buy-the-Dip rules, sector confirmation,
  reversal/repair context, news review, or position-risk controls.
- The system creates a checklist and catalyst prompt, but it must not
  place orders or modify main-strategy candidates.
- If the user wants long-term company exposure, prefer the common stock or
  unlevered ETF.
- If the setup is only "interesting" but not confirmed, use the research list,
  not a 2x ETF.

The overlay action labels are:

- `ALLOW_MANUAL_REVIEW`: all checklist items, including catalyst, passed.
- `NEED_CATALYST_REVIEW`: technical and risk gates passed, but catalyst/news
  still needs manual validation.
- `COMMON_STOCK_PREFERRED`: setup is interesting, but not strong enough for 2x.
- `NO_2X_TRADE`: one or more critical leverage gates failed.

## 1. Most important rule

The prerequisite for using a 2x ETF is not "I think this stock will rise." It is:

> The market has already proved the trend is turning up, and leverage is being
> used to amplify a high-probability, bounded-risk trade.

Avoid two extremes:

- Too early: treating the first bounce in a downtrend as a reversal.
- Too late: chasing after several large up days and euphoric sentiment.

The ideal zone is between those extremes:

> Trend confirmed, but not obviously accelerated or overheated.

## 2. Why 2x ETFs cannot be traded like ordinary stocks

Most 2x ETFs target roughly 2x the underlying asset's daily return, not a simple
2x long-term return. This creates several practical consequences:

1. Losses are amplified quickly in down moves.
2. High-volatility sideways markets create path decay.
3. Short-term trades should not be silently converted into long-term holds.
4. Averaging down is especially dangerous.
5. Entry quality, stop discipline, and market regime matter more than they do
   for ordinary stock positions.

Basic rules:

- Long-term bullish company view: prefer the common stock.
- Short-term trend highly confirmed: only then consider a 2x ETF.
- Treat 2x ETFs as tactical positions, not core long-term holdings.

## 3. First layer: market must be risk-on

Only consider 2x long ETFs when risk appetite is clearly open.

Watch QQQ, SOXX, AI hardware, semiconductor, optical communication, and other
high-beta groups.

Typical risk-on evidence:

- Major indexes are above their 20-day and 50-day moving averages.
- The 20-day moving average is rising.
- Pullbacks are bought quickly.
- High-beta stocks outperform broad indexes.
- Multiple stocks in the same industry are strengthening together.
- Good news pushes prices higher.
- Ordinary news does not trigger large selloffs.
- Bad-news dips are bought back quickly.

Risk-off evidence:

- Good news gaps up and fades.
- Ordinary news triggers selling.
- Bad news causes consecutive sharp declines.
- High-beta stocks underperform indexes.
- SOXX / QQQ break key moving averages.
- Individual stocks keep breaking prior lows.
- The market shows repeated deleveraging.

Rule:

> Do not use 2x long ETFs to bottom-fish in risk-off environments.

## 4. Second layer: trend launch, not dead-cat bounce

Wrong structure: bounce inside a downtrend.

```text
100
 ↓
80
 ↓
65
 ↓
50
 ↑
58
```

The 50 to 58 move is +16%, but it can still be only a countertrend bounce.

At this location:

- Do not assume trend reversal.
- Do not use a 2x ETF.
- Do not interpret "fast rebound" as lower risk.

## 5. Preferred setup A: breakout, pullback, renewed strength

This is the highest-priority structure.

```text
           New high
            ↑
        ─────────
       /
Prior high ───────
       ↑
       │ Breakout
       ↓
   Pullback to prior high
       ↓
   Support holds
       ↑
   Strength resumes
```

Ideal conditions:

- Common stock breaks a key prior high.
- Breakout volume expands.
- A normal pullback follows.
- Pullback volume contracts.
- Price holds the breakout level or key support.
- Renewed strength confirms that buyers returned.

Preferred approach:

> Do not chase the breakout day. Wait for the first successful pullback, then
> consider leverage only when the trend turns up again.

This makes the invalidation level clearer and improves risk/reward.

## 6. Tradable setup B: high-level consolidation breakout

```text
Resistance ─────────────
            ╱╲    ╱╲
           ╱  ╲__╱  ╲
Support    ─────────────

          Consolidation

                    ↑
                 Breakout
```

Suitable conditions:

- Prior uptrend is healthy.
- Price consolidates for several days to several weeks.
- Volatility contracts.
- Volume declines during consolidation.
- Breakout volume expands clearly.
- Market and industry are both risk-on.

This structure is usually better than chasing after several consecutive large
green candles.

## 7. Situations where 2x ETFs should not be bought

### 7.1 Chasing after several large up days

```text
Day 1  +8%
Day 2 +12%
Day 3 +18%
          ↑
       Buy 2x
```

Problems:

- Large embedded profit-taking pressure.
- Volatility has already expanded.
- Normal pullbacks become painful in a 2x product.
- Risk/reward deteriorates.

Rule:

> A strong trend does not mean every price is buyable.

### 7.2 Bottom-fishing in a downtrend

Do not use 2x ETFs if the common stock is still:

- making new lows;
- below falling 20-day / 50-day moving averages;
- unable to reclaim prior highs;
- in a weak industry backdrop.

Even a strong short-term bounce is not enough.

### 7.3 Averaging down with a 2x ETF

Forbidden logic:

> "It has fallen a lot. I will add some to lower my cost basis."

In 2x products this often becomes:

> Increasing exposure to amplify an idea that the market has not validated.

### 7.4 Rebuying because of FOMO

Before chasing a position back, ask:

> If I were completely flat right now, would I actively open a new position at
> this price?

If the answer is no, do not rebuy only because:

- you sold too early;
- you fear further upside;
- you want to recover missed profit.

## 8. Fundamental catalyst requirement

2x ETFs work best when three conditions align:

> Technical trend + fundamental trend + market risk appetite.

Acceptable catalysts:

- earnings beat;
- guidance raise;
- revenue acceleration;
- gross margin expansion;
- new product cycle;
- AI capex growth;
- supply/demand shortage;
- industry pricing upcycle;
- major customer order;
- policy shift creating structural benefit.

Avoid:

> Price up only, with no fundamental or industry logic.

## 9. 2x ETF entry checklist

Check every item before buying.

| Item | Condition |
|---|---|
| Market | QQQ / SOXX are risk-on |
| Industry | Multiple stocks in the group are rising together |
| Common-stock trend | 20D / 50D trend is up |
| Technical structure | Breakout or first successful pullback |
| Catalyst | Clear fundamental / industry catalyst |
| Risk/reward | Approximately 2:1 or better |
| Stop | Defined before entry |
| Location | Not an emotional chase after consecutive large up days |

Simple score:

- 7-8 satisfied: 2x ETF may be considered.
- 5-6 satisfied: common stock is usually more appropriate; use leverage with
  caution.
- 4 or fewer satisfied: no trade.

## 10. Position management

2x ETFs should not use ordinary-stock sizing logic.

Separate total exposure into:

```text
Long-term view
└── Common-stock core

Confirmed short-term trend enhancement
└── 2x ETF tactical
```

Principles:

- Do not use a 2x ETF to replace the entire common-stock position.
- Do not rapidly increase leverage just because the short-term trade is
  profitable.
- Predefine per-trade risk.
- Reduce leveraged size as high-volatility events approach.

## 11. Stop-loss principles

> Decide where the trade is wrong before deciding how much to buy.

Define the invalidation level before entry. Examples:

- break below breakout level;
- break below recent swing low;
- break below key moving average;
- industry trend weakens;
- QQQ / SOXX clearly move risk-off;
- catalyst is invalidated.

Forbidden:

> Buying first, then deciding whether to stop based on the size of the loss.

## 12. MUU trade review

Prior MUU mistakes can be summarized as:

### Mistake 1: treating a bounce as a reversal

Leverage was used before the trend had sufficiently repaired.

### Mistake 2: adding leverage in an uncertain phase

The common stock's direction had not yet been confirmed by the market.

### Mistake 3: missing invalidation conditions

The trade gradually changed from:

> short-term trend trade

into:

> waiting after a drawdown.

Final rule:

> MUU is only for confirmed trend-following trades. It is not for bottom-fishing,
> averaging down, or holding through a broken setup.

## 13. One-line decision flow

Answer in order:

```text
1. Is the market risk-on?
       ↓ Yes
2. Is the industry strengthening together?
       ↓ Yes
3. Is the common stock in an uptrend?
       ↓ Yes
4. Is this a breakout / first pullback, not a multi-day chase?
       ↓ Yes
5. Is there a clear catalyst?
       ↓ Yes
6. Where is the stop?
       ↓ Clear
7. Is risk/reward >= 2:1?
       ↓ Yes

       2x ETF may be considered
```

If any core condition cannot be answered:

> Prefer the common stock, or do not trade.

## 14. Final discipline

2x ETF may be used only with:

> Risk-on + strong industry + strong common stock + confirmed trend + good
> location + catalyst + defined stop.

Do not use 2x ETF with:

> Risk-off + guessing bottoms + FOMO + consecutive extended candles + averaging
> down + no stop.

Core idea:

> Leverage does not improve win rate. It amplifies a setup whose probability is
> already high enough.

> Leverage is an amplifier after trend confirmation, not a rescue tool when the
> thesis is wrong.
