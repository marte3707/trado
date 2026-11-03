# Funding-rate carry (long spot + short perp) on Hyperliquid

Data: `export_hyperliquid_funding.py`, i.e. real hourly funding and premium for the top 30 Hyperliquid
perps by open interest, 2023-05 → 2026-11 (643k funding rows). Backtest: `carry_backtest.py`.

Vibe-Trading's `perp-funding-basis` skill only documents the carry framework. Its backtest
engines take directional weights and cannot hold a hedged spot+perp pair, so this backtest
follows that framework in plain pandas.

## Model

- Decision once a day at 00:00 UTC on trailing 7-day funding (no lookahead). Enter above 15% APR,
  exit below 5% APR. A position earns the hourly settlements that follow.
- Capital per unit of notional: 1 spot + 1/3 perp margin (3x). Idle capital earns 0%.
- Costs per leg change: perp 0.045% + spot 0.07% (Hyperliquid base taker) + slippage
  (3 bps majors, 10 bps alts per leg). Positions resize when notional drifts more than 25%.
  Spot is sold to refill margin after rallies.
- Basis: the perp premium at entry vs exit is realised, and marked to market daily.
- **Not modelled:** exchange/counterparty risk, ADL, oracle or stablecoin depeg. Also, most alts
  have no Hyperliquid spot market, so their spot leg would sit on another venue.

## Results

| Config | CAGR | Sharpe | Max DD | Worst day | Avg invested | Entries | Funding | Basis | Costs | Liq-risk days |
|---|---|---|---|---|---|---|---|---|---|---|
| BTC+ETH always on | 8.1% | 8.94 | -0.8% | -0.14% | 100% | 2 | +43.1% | +0.0% | -12.1% | 3 |
| Majors (BTC/ETH/SOL), 15%/5% | 7.5% | 8.33 | -0.3% | -0.09% | 47% | 24 | +35.1% | +0.9% | -7.5% | 5 |
| HL-spot set (+HYPE), 15%/5% | 6.7% | 9.10 | -0.2% | -0.07% | 45% | 29 | +32.8% | +0.8% | -8.3% | 9 |
| All 30, top 5, 15%/5% | 10.6% | 8.06 | -0.8% | -0.26% | 78% | 87 | +66.2% | +1.7% | -26.2% | 67 |
| All 30, top 5, 25%/10% | 10.2% | 7.42 | -0.5% | -0.18% | 52% | 107 | +60.2% | +2.3% | -22.8% | 57 |
| All 30, top 10, 15%/5% | 9.3% | 8.55 | -0.4% | -0.09% | 66% | 146 | +55.8% | +1.4% | -21.5% | 107 |
| All 30, top 5, 2x slippage | 6.7% | 4.87 | -2.0% | -0.39% | 78% | 87 | +63.0% | +1.6% | -39.5% | 67 |

Funding / Basis / Costs are cumulative, as % of starting equity.

Per calendar year (CAGR within the year):

| Config | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|
| BTC+ETH always on | +7.1% | +16.0% | +4.4% | +1.0% |
| Majors (BTC/ETH/SOL), 15%/5% | +7.7% | +15.8% | +3.0% | +0.0% |
| HL-spot set (+HYPE), 15%/5% | +5.7% | +12.8% | +4.9% | +0.1% |
| All 30, top 5, 15%/5% | +8.8% | +22.1% | +3.5% | +3.0% |
| All 30, top 5, 25%/10% | +9.3% | +22.3% | +1.4% | +3.0% |
| All 30, top 10, 15%/5% | +8.1% | +19.3% | +2.9% | +2.3% |
| All 30, top 5, 2x slippage | +6.8% | +17.9% | -0.5% | -0.2% |

Average funding by year (APR %): BTC 15.1 / 24.1 / 10.6 / 5.5, ETH 22.4 / 22.0 / 8.5 / 5.9,
SOL 13.0 / 28.1 / 5.3 / 0.7, HYPE – / 118.8 / 22.1 / 9.4 (2023 / 2024 / 2025 / 2026).
In the last 90 days, 72% of hours paid exactly the 0.00125%/h baseline.

## Verdict

**Not built into Trado now.** The trade is genuinely low-risk in the model: every year was
positive and drawdowns stayed below 2%. But the money was made in the 2024 bull market. In 2025
and 2026 it returns roughly 0–5% a year, at or below what idle stablecoins earn, and costs eat
about a third of gross funding for the 30-coin variants. The very high Sharpe reflects smooth
funding accrual and none of the tail risks listed above.

When this becomes worth running: 7-day funding on BTC/ETH sustained above ~15–20% APR.
Re-run the export and this backtest at that point.
