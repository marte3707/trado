# Donchian-ensemble trend following — vibetrading backtest

Strategy from Zarattini, Pagani & Barbon (2025), *Catching Crypto Trends: A Tactical
Approach for Bitcoin and Altcoins* (Swiss Finance Institute RP 25-80). Rules are in
`strategy.py`. Parameters are the paper-style defaults and were **not** tuned on this data.

## How it was tested

- Engine: `vibetrading==0.4.0` (`BacktestEngine`, `exchange="hyperliquid"`, daily steps).
- Data: Bitstamp BTC/USD daily (2011→2026-11-02). Candles are re-indexed to close time
  to avoid lookahead (vibetrading reads `close` as-of the candle open timestamp).
- Costs: 0.045% taker fee (Hyperliquid base tier), 5 bps slippage, constant funding of
  0.01%/8h (~11% APR) paid on longs, which is the Hyperliquid baseline rate.
- Independent check: a vectorized pandas re-implementation gives matching numbers
  (2016–2026 Sharpe 1.28, 2021–2026 Sharpe 0.47).

## Results (BTC only)

| Period | Strategy CAGR | Sharpe | Max DD | Buy & hold CAGR | B&H Sharpe | B&H Max DD |
|---|---|---|---|---|---|---|
| 2016-01 → 2026-11 | 20.3% | 1.31 | -19.1% | 61.6% | 1.06 | -83.4% |
| 2016-01 → 2020-12 | 38.0% | 1.98 | -13.9% | 131.9% | 1.49 | -83.4% |
| 2021-01 → 2026-11 | 6.5% | 0.56 | -20.2% | 18.5% | 0.58 | -76.7% |
| 2022 (bear) | -9.1% | -2.46 | -9.6% | -64.2% | -1.28 | -67.0% |
| 2024-01 → 2026-11 | 5.2% | 0.46 | -14.1% | 24.3% | 0.70 | -53.1% |
| 2025-01 → 2026-11 | -3.1% | -0.32 | -14.1% | -9.2% | -0.01 | -53.1% |

Robustness (same windows, full / since 2021):

| Variant | Sharpe | CAGR |
|---|---|---|
| No funding (spot-equivalent) | 1.45 / 0.73 | 22.7% / 8.8% |
| Target vol 40% | 1.30 / 0.47 | 30.1% / 7.4% |
| Without 5/10-day lookbacks | 1.19 / 0.45 | 19.1% / 5.4% |

## Verdict

Not promoted to Trado. The strategy is an excellent drawdown reducer (max DD ~20% vs ~80%),
but since 2021 its risk-adjusted return on BTC is no better than buy & hold and it is
slightly negative over 2025–2026. Perp funding on Hyperliquid eats roughly a third of the gross edge.

The paper's headline results come from a diversified multi-coin portfolio, which could not
be tested here (the sandbox only reached BTC data). Run it locally before deciding:

```bash
pip install vibetrading==0.4.0
cd research/donchian_ensemble
python backtest.py --binance BTC,ETH,SOL,XRP,DOGE,ADA,AVAX,LINK,DOT,BNB --start 2021-01-01 --end 2026-11-02
```

Promotion bar suggested: Sharpe ≥ 1.0 and CAGR above cash for **2021→today** with real
funding, and positive in at least 2 of the last 3 calendar years.
