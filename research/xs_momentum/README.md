# Cross-sectional momentum on Hyperliquid perps

Method after Liu, Tsyvinski & Wu (2022), *Common Risk Factors in Cryptocurrency*, Journal of
Finance. Every Monday, coins are ranked by trailing return. The strategy goes long the top K
and short the bottom K, equal weight, with gross exposure 1x. Script: `xs_momentum_backtest.py`.

- **Universe (point in time):** at each rebalance, the 40 coins with the highest trailing 30-day
  dollar volume and at least 60 days of history.
- **Costs:** 0.045% taker fee + 10 bps slippage per side on turnover. Real funding where it was
  exported, otherwise the 0.00125%/h baseline (it nets to ~0 in a balanced long/short book).
- **No lookahead:** weights chosen at a daily close earn the next day's return.

## Data

1. `funding_carry/data`: today's top 30 perps by open interest, real funding. **Survivorship-biased.**
2. `funding_carry/data_all`: all 235 perps listed since 2023-05, including 56 delisted, candles only:
   `python3 export_hyperliquid_funding.py --top 0 --include-delisted --candles-only --out ./data_all`

## Results, Sep 2023 → Nov 2026

### Biased universe (today's top 30): looks good

| Config | CAGR | Sharpe | Max DD | Price P&L/yr | Funding/yr | Costs/yr | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|
| Equal-weight long, top-40 liquid (benchmark) | 55.2% | 0.98 | -64.3% | +68.5% | +0.0% | +0.0% | +88.9% | +146.0% | -38.0% | +40.2% |
| L/S top-bottom 5, 21d | 25.1% | 0.87 | -32.0% | +36.4% | -2.7% | -6.5% | +3.9% | +41.5% | +23.9% | +11.9% |
| L/S 5, 7d | 15.1% | 0.60 | -34.7% | +31.5% | -1.2% | -11.0% | -8.9% | +39.8% | +25.9% | -2.7% |
| L/S 5, 14d | 27.9% | 0.95 | -19.7% | +39.5% | -2.5% | -7.6% | -7.6% | +55.9% | +49.5% | +1.5% |
| L/S 5, 28d | 27.8% | 0.92 | -35.9% | +38.5% | -3.3% | -5.4% | +14.5% | +39.7% | -0.9% | +37.6% |
| L/S 5, 56d | 18.2% | 0.68 | -37.6% | +28.3% | -2.3% | -4.0% | +19.1% | -23.2% | +35.8% | +37.0% |
| L/S 3, 21d | 21.6% | 0.67 | -54.6% | +39.8% | -3.8% | -7.7% | +5.9% | +91.5% | -20.7% | +15.7% |
| L/S 5, 21d, skip 1d | 26.0% | 0.89 | -32.8% | +36.9% | -2.5% | -6.2% | +8.1% | +39.8% | +24.4% | +10.8% |
| Long-only top 5, 21d | 73.4% | 1.09 | -61.2% | +107.2% | -14.1% | -6.1% | +81.2% | +105.7% | -16.7% | +84.9% |
| L/S 5, 21d, 2x slippage | 19.6% | 0.73 | -32.8% | +36.4% | -2.7% | -10.9% | +2.5% | +35.7% | +18.1% | +7.6% |
| L/S 5, 21d, top-20 liquid only | 11.3% | 0.51 | -34.7% | +24.0% | -2.7% | -6.2% | +3.9% | +42.1% | +20.5% | -21.1% |

### Full universe incl. delisted coins: the honest test

| Config | CAGR | Sharpe | Max DD | Price P&L/yr | Funding/yr | Costs/yr | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|
| Equal-weight long, top-40 liquid (benchmark) | -9.9% | 0.25 | -86.6% | +19.7% | +0.0% | +0.0% | +111.5% | +30.9% | -71.0% | -10.6% |
| L/S top-bottom 5, 21d | 26.9% | 0.79 | -53.8% | +39.9% | +0.0% | -7.9% | -0.7% | +74.2% | +59.4% | -22.7% |
| L/S 5, 7d | 9.6% | 0.43 | -58.5% | +30.0% | +0.0% | -12.5% | +3.4% | -26.4% | +56.5% | +12.4% |
| L/S 5, 14d | 24.4% | 0.74 | -43.1% | +39.2% | +0.0% | -9.4% | -8.3% | +21.6% | +50.1% | +19.5% |
| L/S 5, 28d | 4.4% | 0.31 | -41.9% | +19.8% | +0.0% | -7.3% | -17.8% | +12.7% | +51.1% | -18.1% |
| L/S 5, 56d | 5.0% | 0.32 | -38.9% | +18.4% | +0.0% | -5.3% | -6.0% | +9.6% | +23.4% | -8.2% |
| L/S 3, 21d | 42.3% | 0.93 | -64.0% | +58.0% | +0.0% | -8.8% | -13.5% | +141.0% | +88.7% | -22.0% |
| L/S 5, 21d, skip 1d | 18.2% | 0.61 | -53.2% | +32.8% | +0.0% | -7.9% | +3.9% | +44.9% | +48.0% | -23.7% |
| Long-only top 5, 21d | -19.8% | 0.27 | -91.6% | +44.9% | -10.9% | -7.4% | +96.4% | +91.6% | -69.5% | -56.9% |
| L/S 5, 21d, 2x slippage | 20.1% | 0.65 | -55.1% | +39.9% | +0.0% | -13.4% | -2.5% | +64.3% | +50.9% | -26.0% |
| L/S 5, 21d, top-20 liquid only | 18.2% | 0.65 | -45.1% | +30.1% | +0.0% | -7.1% | +22.7% | +19.9% | +49.3% | -22.6% |

## Verdict

**Not promoted to Trado.** With delisted coins included, the realistic altcoin benchmark lost
about 10% a year with an -87% drawdown. Momentum long/short still shows a gross edge of about +20–58%
a year in price P&L, but it fails as a strategy:

- Sharpe 0.3–0.93 depending on settings, below the 1.0 bar.
- Max drawdown -39% to -64%, and 2026 is negative for most variants (-18% to -24%).
- Results swing heavily with small parameter changes (21-day lookback 26.9% CAGR, 28-day 4.4%,
  skipping one day 18.2%). That is the signature of noise, not a robust effect, in a 3-year sample.
- The long-only version loses 20% a year: the edge exists only in the relative ranking,
  so any version that can't short is worse than nothing.

Survivorship bias accounted for most of the difference between the two datasets. That is
why every cross-sectional test needs the `--include-delisted` export.
