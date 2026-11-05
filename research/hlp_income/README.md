# Low-risk income: HLP vault, funding carry, and a switch between them

Data from `export_hlp_history.py` (Hyperliquid `vaultDetails`, HLP vault
`0xdfc24b077bc1425ad1dea75bcb6f8158e10df303`). The allTime series has one sample every 1–2
weeks, from 2023-05 to 2026-11, with account value and cumulative PnL.

**Method.** A depositor's return over an interval is ΔPnL / account value. Deposits and
withdrawals move account value but not PnL. Two denominators were used:
- start-of-interval value: optimistic when large inflows arrive mid-interval
- max(start, end): conservative; used for every decision below

Intervals are 1–2 weeks long, so intra-interval dips (e.g. the March 2025 JELLY episode)
are smoothed out. Real peak-to-trough drawdowns were somewhat deeper than shown.

## HLP depositor returns

`python hlp_analysis.py --data ./data` (start-of-interval denominator):

```
allTime: 96 intervals, 2023-06-07 -> 2026-11-05
  CAGR 34.8%  vol 14.9%  return/vol 2.34  max DD -5.8%  negative intervals 12%
  worst interval -4.42% (to 2023-12-13), best +17.84% (to 2024-01-10)

By calendar year (compounded interval returns, attributed to interval end):
  2023:  +19.6%  (196 days, worst interval -4.42%)
  2024:  +79.1%  (357 days, worst interval +0.03%)
  2025:  +19.0%  (371 days, worst interval -0.80%)
  2026:   +8.1%  (315 days, worst interval -0.51%)

By half-year:
  2023 H1:   -0.7%  annualised  -12.0%
  2023 H2:  +20.4%  annualised  +47.4%
  2024 H1:  +59.1%  annualised +153.9%
  2024 H2:  +12.6%  annualised  +28.0%
  2025 H1:   +6.0%  annualised  +11.8%
  2025 H2:  +12.3%  annualised  +26.1%
  2026 H1:   +7.1%  annualised  +14.8%
  2026 H2:   +0.9%  annualised   +2.5%

Last month: +0.05% over 30.5 days (annualised +0.6%), worst step -0.32% at 2026-11-01 05:48

Last week: -0.20% over 7.1 days (annualised -9.6%), worst step -0.32% at 2026-11-01 05:48
```

Conservative denominator: **CAGR 23.3%, max drawdown -5.8%**. By year: 2023 +9.7%,
2024 +50.7%, 2025 +15.1%, 2026 YTD +7.0%.

## HLP vs carry vs switcher

`python switch_backtest.py --hlp ./data --funding ../funding_carry/data`. The switcher
holds HLP and moves to BTC+ETH carry while 7-day funding on both is ≥15% APR. That is the
same trigger as the worker's funding alert, read before each interval, with a 0.45% cost
per round-trip switch.

```
2023-06-07 -> 2026-11-05, carry active in 25% of intervals
| Year | HLP | Carry BTC+ETH | Switcher |
|---|---|---|---|
| 2023 | +9.7% | +7.1% | +10.9% |
| 2024 | +50.7% | +16.2% | +20.9% |
| 2025 | +15.1% | +4.7% | +13.7% |
| 2026 | +7.0% | +1.0% | +7.0% |
hlp     total +103.7%  max DD -5.8%
carry   total +31.7%  max DD -0.7%
switch  total +63.1%  max DD -5.1%
```

## Verdict

- **HLP is the best steady-income candidate tested.**
  - Every calendar year was positive.
  - Max drawdown was about 6% at this sampling, and 88% of 1–2 week intervals were positive.
  - It earned ~23% a year conservatively, far ahead of carry (~8%), and it doesn't need Trado to
    trade anything.
- **The switcher is rejected.** HLP earned most exactly when funding was high (2024), so moving
  to carry in those periods cost about 40 points of total return.
- **Yield is compressing.** 2026 H2 runs at roughly 2.5% annualised and the last month at ~0.6%,
  the same pattern as funding. Historical returns came largely from volatile, high-volume markets.
- **Risks the numbers don't show:**
  - platform/bridge risk, with everything on one venue
  - market-manipulation incidents (JELLY in March 2025, POPCAT in November 2025)
  - the 4-day withdrawal lockup
  - no guarantee: HLP is a market-making and liquidation book, not a yield product
