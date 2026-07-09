# Analisi operativa Trado - 2026-06-24

Report generato da query read-only sul DB locale `localhost:5433/trado` e da
script/analytics gia' presenti nel repository. Non sono stati modificati runtime
settings, posizioni, ordini, servizi o codice trading.

Timestamp estrazione principale: `2026-06-24T15:02:50Z`.

## Executive summary

Il sistema sta aprendo e gestendo trade, ma l'edge recente e' deteriorato. La
conclusione piu' importante e' che il problema non e' principalmente di costi
live: e' qualita' del segnale/side selection, con un drag molto forte sui LONG e
con un recente peggioramento delle strategie che storicamente sembravano migliori.

Metriche base:

| Area | Valore |
|---|---:|
| Mercati attivi | 525 |
| Candele | 4,524,459 |
| Segnali | 2,204 |
| Posizioni | 1,500 |
| Ordini | 5,415 |
| Decision log | 53,246 |
| Finestra segnali | 2026-05-11 -> 2026-06-24 |
| Finestra posizioni chiuse | 2026-05-11 -> 2026-06-24 |

Risultato complessivo:

| Mode | Trade chiusi | WR | PF | PnL | Last 150 PnL | Last 30d PnL |
|---|---:|---:|---:|---:|---:|---:|
| Paper | 968 | 43.3% | 0.88 | -10,529.51 | -5,078.06 | +616.53 |
| Live | 483 | 43.9% | 0.90 | -40.42 | -46.26 | -30.23 |

La lettura corretta e':

- Paper storico negativo, nonostante il periodo 30d sia leggermente positivo.
- Ultimi 150 trade paper molto deboli: PF 0.53, PnL -5,078.
- Live quasi flat in valore assoluto, ma negativo: PF 0.90, PnL lordo -40.42.
- Le fee live non spiegano il problema: il netto stimato e' -53.80, quindi circa
  13.31 USD di fee su 483 trade.
- Il side LONG e' il problema principale: paper LONG -17,245 vs SHORT +6,716;
  live LONG -39.04 vs SHORT -1.38.
- AutoCalibration vede correttamente deterioramento e starvation, ma sta
  applicando cambi frequenti senza aver ancora recuperato l'edge.

## Edge delle strategie

### Out-of-sample

`scripts.edge_oos_report` usa split temporale 70/30 sul paper chiuso.

Sistema intero:

| Scope | Trade | WR | PF | PnL |
|---|---:|---:|---:|---:|
| Full | 968 | 43% | 0.88 | -10,529.5 |
| Train 70% | 677 | 43% | 0.94 | -3,433.9 |
| Test 30% | 291 | 44% | 0.72 | -7,095.6 |
| Time-weighted half-life 50 | 968 | 43% | 0.62 | -1,799.7 |

Verdetto OOS:

- Celle con campione test sufficiente: 47 / 102.
- Celle con PF_test >= 1.20 grezzo: 12.
- Celle con edge OOS robusto, cioe' train concorde + PF_test >= 1.20: 0.

Questa e' una bandiera rossa: i PF_test alti sono quasi tutti train/test
discordanti su campioni piccoli. Non vanno trattati come edge stabile.

### Classificazione strategie/celle

Robuste:

- Nessuna cella `timeframe:strategy` passa il criterio OOS robusto.

Promettenti ma non ancora affidabili:

- `15m:SHORT`: paper n=303, PF 1.44, PnL +9,619; live n=183, PF 1.08,
  PnL +10.96.
- Bucket expected-R short: `SHORT:15m:60-69` expR +0.106, n=197; `SHORT:15m:70-79`
  expR +0.129, n=104; `SHORT:4h:60-69` expR +0.197, n=19.
- Alcune celle 4h hanno PF_test alto (`support_resistance`, `volume_spike`,
  `ema_crossover`, `vwap`, `supertrend`), ma il train era debole e il campione
  test e' piccolo. Sono candidate da A/B, non da promozione diretta.

Negative / da penalizzare:

| Strategia | Trade attribuiti | PF | PnL | Recent PnL |
|---|---:|---:|---:|---:|
| rsi | 126 | 0.38 | -8,965.99 | 0.00 |
| macd | 229 | 0.60 | -8,861.10 | +221.16 |
| supertrend | 240 | 0.62 | -8,745.13 | -316.34 |
| volume_profile | 155 | 0.73 | -4,187.88 | +9.79 |
| volume_spike | 439 | 0.90 | -3,882.64 | -2,750.63 |
| fear_greed | 66 | 0.58 | -2,324.96 | 0.00 |
| cvd_divergence | 145 | 0.85 | -2,065.30 | -1,442.68 |
| support_resistance | 491 | 0.95 | -1,969.20 | -3,559.32 |
| vwap | 552 | 0.96 | -1,866.53 | -5,467.97 |

Strategie storicamente positive ma regime recente deteriorato:

| Strategia | PF totale | PnL totale | Recent PF | Recent PnL |
|---|---:|---:|---:|---:|
| fibonacci | 1.13 | +4,022.91 | 0.47 | -3,321.76 |
| candlestick_patterns | 1.04 | +1,442.59 | 0.71 | -1,334.91 |
| oi_trend | 1.03 | +394.08 | 0.40 | -2,286.00 |
| wyckoff_spring_upthrust | 1.01 | +118.07 | 0.85 | -452.47 |

Inerti:

- Il detector `strategy_attribution` non marca strategie come `inert`.
- Alcune sorgenti hanno zero o pochissimi trade recenti perche' disabilitate o
  fuori flusso operativo; non vanno interpretate come robuste.

## Timeframe, side e market attribution

### Paper

| Timeframe | Trade | WR | PF | PnL |
|---|---:|---:|---:|---:|
| 15m | 408 | 52.7% | 1.21 | +6,299.63 |
| 4h | 58 | 37.9% | 0.86 | -712.74 |
| 1h | 125 | 36.8% | 0.72 | -3,233.38 |
| 1d | 44 | 15.9% | 0.15 | -3,491.07 |
| 30m | 86 | 30.2% | 0.52 | -4,290.95 |
| 5m | 247 | 41.7% | 0.80 | -5,101.00 |

Side:

| Side | Trade | WR | PF | PnL |
|---|---:|---:|---:|---:|
| SHORT | 665 | 49.5% | 1.12 | +6,715.84 |
| LONG | 303 | 29.7% | 0.42 | -17,245.35 |

Peggiori celle paper:

| Cella | Trade | PF | PnL |
|---|---:|---:|---:|
| 15m:LONG | 105 | 0.59 | -3,319.78 |
| 1h:LONG | 50 | 0.42 | -3,252.79 |
| 1d:LONG | 36 | 0.14 | -3,195.41 |
| 5m:LONG | 49 | 0.48 | -2,593.80 |
| 30m:LONG | 30 | 0.33 | -2,553.48 |
| 5m:SHORT | 198 | 0.87 | -2,507.21 |

Migliori celle paper:

| Cella | Trade | PF | PnL |
|---|---:|---:|---:|
| 15m:SHORT | 303 | 1.44 | +9,619.41 |
| 4h:SHORT | 25 | 2.18 | +1,617.35 |

Peggiori market paper con almeno 3 trade:

- `SOL`: n=17, PF 0.19, PnL -1,609.16.
- `CRV`: n=10, PF 0.17, PnL -1,416.44.
- `ZEC`: n=14, PF 0.31, PnL -1,133.15.
- `ASTER`: n=8, PF 0.11, PnL -1,065.00.
- `TRUMP`: n=7, PF 0.08, PnL -1,002.52.

Migliori market paper con almeno 3 trade:

- `ETH`: n=25, PF 2.23, PnL +1,421.70.
- `INJ`: n=12, PF 2.32, PnL +1,187.91.
- `POL`: n=6, PF inf, PnL +1,080.53.
- `DOGE`: n=20, PF 1.96, PnL +989.38.
- `HYPE/USDC`: n=7, PF 5.39, PnL +878.84.

### Live

| Timeframe | Trade | WR | PF | PnL |
|---|---:|---:|---:|---:|
| 15m | 239 | 46.9% | 1.05 | +7.60 |
| 1d | 13 | 38.5% | 1.13 | +1.23 |
| 4h | 17 | 41.2% | 0.86 | -3.34 |
| 5m | 135 | 43.0% | 0.95 | -5.20 |
| 1h | 58 | 43.1% | 0.69 | -18.84 |
| 30m | 21 | 23.8% | 0.04 | -21.86 |

Side:

| Side | Trade | WR | PF | PnL |
|---|---:|---:|---:|---:|
| SHORT | 359 | 44.3% | 0.99 | -1.38 |
| LONG | 124 | 42.7% | 0.67 | -39.04 |

Peggiori celle live:

| Cella | Trade | PF | PnL |
|---|---:|---:|---:|
| 1h:LONG | 30 | 0.38 | -22.77 |
| 30m:SHORT | 17 | 0.06 | -15.99 |
| 5m:LONG | 21 | 0.53 | -10.63 |
| 15m:LONG | 56 | 0.91 | -3.36 |

Migliori celle live:

| Cella | Trade | PF | PnL |
|---|---:|---:|---:|
| 15m:SHORT | 183 | 1.08 | +10.96 |
| 5m:SHORT | 114 | 1.07 | +5.43 |
| 1h:SHORT | 28 | 1.16 | +3.93 |

## Trading economics e position management

### Live cost report

| Voce | Valore |
|---|---:|
| Trade live chiusi | 483 |
| PnL lordo | -40.4154 |
| Fee | -13.3094 |
| Funding | -0.0796 |
| Slippage entry aggregato | -34.0503 |
| PnL netto stimato | -53.8044 |
| Trade con fee tracciata | 237 |
| Trade con fee stimata | 246 |
| Avg slippage bps | -15.44 |
| Taker fee rate usato | 0.00045 |

Interpretazione:

- Le fee peggiorano il risultato ma non sono il driver principale.
- Lo slippage entry aggregato e' favorevole (negativo come costo), quindi gli
  ordini market non risultano il primo colpevole dai dati correnti.
- Il notional live medio e' molto basso: circa 42 USD/trade. Il live sta
  facendo una versione "miniaturizzata" del paper; quando l'edge e' sottile,
  basta poco per portarlo sotto zero.

### Drawdown

Paper closed-PnL curve:

- Max drawdown: 19,916.57, pari a 105.2% rispetto al peak equity di riferimento.
- Peak: 2026-06-06 20:22 UTC.
- Trough: 2026-06-23 09:00 UTC.
- Ending equity da closed PnL rispetto a paper equity 10,000: -529.51.

Live account snapshots:

- Snapshot: 15,567.
- Primo valore: 129.03, 2026-05-14 10:52 UTC.
- Ultimo valore: 165.22, 2026-06-24 15:00 UTC.
- Max drawdown snapshot: 171.94, pari a 52.8%.
- Peak: 2026-06-05 19:16 UTC.
- Trough: 2026-06-19 13:45 UTC.

### Exit reason e TP state

Paper:

| Exit reason | Trade | WR | PF | PnL |
|---|---:|---:|---:|---:|
| stop_loss | 822 | 40.6% | 0.77 | -18,234.87 |
| time_stop | 75 | 24.0% | 0.53 | -1,451.81 |
| trailing_stop | 41 | 100.0% | inf | +4,787.79 |
| tp3 | 12 | 100.0% | inf | +4,330.80 |
| breakeven_stop | 13 | 100.0% | inf | +457.25 |

Live:

| Exit reason | Trade | WR | PF | PnL |
|---|---:|---:|---:|---:|
| btc_macro_protection_loss_exit | 44 | 2.3% | 0.00 | -42.33 |
| stop_loss | 375 | 46.9% | 0.98 | -4.91 |
| tiny_fill_below_min_notional | 6 | 16.7% | 0.05 | -0.18 |
| liquidation_guard_failed | 6 | 16.7% | 0.02 | -0.12 |
| unknown | 26 | 50.0% | 1.26 | +3.20 |

TP state:

| Mode | TP state | Trade | WR | PF | PnL |
|---|---|---:|---:|---:|---:|
| Paper | no_tp1 | 603 | 9.3% | 0.13 | -73,343.01 |
| Paper | tp1_hit | 286 | 99.3% | 256.30 | +40,848.39 |
| Paper | tp2_hit | 79 | 100.0% | inf | +21,965.10 |
| Live | no_tp1 | 319 | 15.0% | 0.13 | -335.43 |
| Live | tp1_hit | 146 | 100.0% | inf | +237.67 |
| Live | tp2_hit | 18 | 100.0% | inf | +57.35 |

Conclusione: il position management produce risultati buoni quando il trade
raggiunge TP1. Il problema e' prima: selezione dell'entry e side/regime. Widening
degli stop non e' la prima raccomandazione, perche' rischia solo di aumentare la
perdita sui trade che non arrivano a TP1.

## Score ed expected-R

Score bucket paper:

| Bucket | Trade | WR | PF | PnL |
|---|---:|---:|---:|---:|
| SHORT:60-69 | 374 | 50.5% | 1.19 | +5,786.12 |
| SHORT:70-79 | 277 | 48.4% | 1.03 | +694.97 |
| LONG:60-69 | 199 | 32.2% | 0.50 | -9,156.22 |
| LONG:70-79 | 82 | 26.8% | 0.35 | -5,761.42 |
| LONG:50-59 | 16 | 6.2% | 0.00 | -2,332.91 |

Expected-R peggiori bucket utilizzabili:

| Bucket | n | Expected R |
|---|---:|---:|
| LONG:15m:70-79 | 19 | -0.272 |
| LONG:1d:60-69 | 27 | -0.264 |
| LONG:4h:60-69 | 22 | -0.263 |
| SHORT:30m:70-79 | 30 | -0.263 |
| LONG:5m:70-79 | 25 | -0.215 |
| LONG:1h:60-69 | 34 | -0.142 |
| LONG:30m:60-69 | 18 | -0.141 |
| LONG:1h:70-79 | 15 | -0.120 |

Expected-R migliori bucket utilizzabili:

| Bucket | n | Expected R |
|---|---:|---:|
| SHORT:4h:60-69 | 19 | +0.197 |
| SHORT:15m:70-79 | 104 | +0.129 |
| SHORT:15m:60-69 | 197 | +0.106 |
| SHORT:1h:60-69 | 46 | +0.102 |
| SHORT:30m:60-69 | 25 | +0.066 |
| SHORT:1h:70-79 | 28 | +0.038 |

Il risultato e' coerente con il resto: score piu' alto non salva i LONG. Il
filtro expected-R dovrebbe essere usato come gate direction/timeframe-aware, non
solo come metrica UI.

## Funnel operativo e runtime

Stato runtime letto da `app_settings`:

- `operation_mode`: `AUTO_TRADE`.
- `enable_live_trading`: `true`.
- Kill switch: `false`.
- Timeframe runtime: enabled `15m`, `1h`, `4h`; monitoring-only `5m`.
- Worker heartbeat: `phase=idle`, timestamp `2026-06-24T15:03:59Z`.

Segnali:

| Stato | Conteggio |
|---|---:|
| new | 433 |
| closed | 984 |
| executed | 149 |
| ignored | 638 |

Segnali per timeframe:

| Timeframe | Segnali |
|---|---:|
| 15m | 929 |
| 5m | 440 |
| 1h | 336 |
| 30m | 254 |
| 4h | 158 |
| 1d | 87 |

Latency segnale-candela:

- Campione: 1,599 segnali con `source_candle_open_time`.
- Media: 117.2 min.
- Mediana: 19.9 min.
- P90: 168.5 min.
- P99: 2,246.5 min.

Ultimi 200 `worker_cycle`:

- `generated_avg`: 0.0.
- Top skip reasons:
  - `volatility_filter`: 2,324.
  - `low_score`: 1,513.
  - `low_liquidity`: 1,234.
  - `insufficient_candles`: 629.
  - `directional_score_filter`: 85.
  - `edge_probation_filter`: 19.

Eventi 7d piu' frequenti:

| Event type | Count |
|---|---:|
| hyperliquid_rate_limit_skipped | 10,535 |
| worker_cycle | 5,424 |
| live_liquidation_guard_cycle | 776 |
| position_stop_updated | 155 |
| macro_risk_state_changed | 118 |
| fast_risk_cycle_failed | 102 |
| live_order_skipped_after_paper_skip | 85 |
| paper_order_rejected_portfolio_guard | 85 |
| paper_order_skipped_duplicate_market | 80 |
| auto_calibration_applied | 73 |

Questo dice due cose:

1. La macchina e' viva, ma nel momento dell'analisi e' in starvation di nuovi
   segnali.
2. `volatility_filter`, score, liquidity e dati insufficienti sono i punti del
   funnel che meritano misurazione sui candidati scartati. Non basta dire che
   "le strategie non funzionano": molti candidati non arrivano nemmeno al trade.

## AutoCalibration e AutoTuner

Stato AutoCalibration:

| Campo | Valore |
|---|---|
| last_status | applied |
| last_run_at | 2026-06-24T14:31:10Z |
| last_total_closed_trade_count | 968 |
| last_trade_count | 500 |
| last_summary PF | 0.949 |
| last_summary PnL | -2,209.73 |
| last_equity_slope | -0.1318 |
| last_recent_slope | -0.4258 |
| recent_defensive_active | true |
| deep_defensive_active | false |
| deep_defensive_cooldown_remaining | 1 |
| starvation_override_active | true |
| consecutive_zero_signal_cycles | 24 |

Stability report:

- `auto_calibration_applied`: 715 run dal 2026-05-13 al 2026-06-24.
- Whipsaw enable/disable: 3 celle (`1d:macd`, `1d:supertrend`, `1d:vwap`).
- Churn pesi piu' alto: `5m:candlestick_patterns`, `15m:ema_crossover`,
  `15m:vwap`, `15m:wyckoff_spring_upthrust`, `5m:fibonacci`.
- Risk setting piu' mossi: `directional_score_adjustments` 208,
  `max_risk_per_trade_pct` 171, `min_risk_reward` 167, `min_signal_score` 150.

AutoTuner:

- Enabled: true.
- Mode: `ab_test`.
- Current step: `strategy_test:15m:volume_profile`.
- Activated: `2026-06-23T09:01:48Z`.
- Allocation: 50/50 deterministic hash.
- Test overlay: disabilita `volume_profile` solo su `15m`.
- Min trades per arm: 30.

L'A/B test attuale ha senso perche' `volume_profile` e' negativo storico ma
recente quasi flat. Va lasciato arrivare a campione sufficiente prima di
decidere.

## Config e guardrail osservati

Runtime risk essenziale:

| Setting | Valore |
|---|---:|
| min_signal_score | 65 |
| min_risk_reward | 2.0 |
| max_risk_per_trade_pct | 2.0 |
| max_open_positions | 25 |
| max_total_exposure_pct | 85 |
| max_leverage | 10 |
| atr_pct_min / max | 0.2 / 0.9 |

Guardrail directional/portfolio:

- `portfolio_guard.enabled=true`.
- `max_directional_risk_r`: live 13, paper 25.
- `max_directional_open_positions`: live 13, paper 25.
- `max_low_tf_directional_positions`: live 13, paper 25, timeframes `5m`, `15m`.
- Current learned `directional_score_adjustments`: solo `SHORT:15m` con +12 score.
- Current `direction_strategy_weight_overrides`: `1h:LONG:vwap`,
  `15m:LONG:oi_trend`, `1h:LONG:fibonacci` a peso 1.

Nota: i dati indicano LONG come drag principale, ma lo stato corrente non ha
`directional_min_score_adjustments` o blocklist LONG attivi. Ci sono solo alcuni
override di peso per strategie LONG specifiche.

Open exposure al momento:

| Mode | Open | Side | Notional | Unrealized |
|---|---:|---|---:|---:|
| Paper | 35 | 24 short / 11 long | 49,883.65 | +2,228.31 |
| Live | 14 | 10 short / 4 long | 523.75 | +15.73 |

## Raccomandazioni prioritarie

### P0 - Non aumentare size live finche' il recente non recupera

Motivo: live netto stimato -53.80 e paper last150 PF 0.53. Le fee non sono il
problema principale, ma aumentare notional ora amplificherebbe un edge recente
negativo.

Rollback: nessuno, e' una decisione operativa/configurativa.

### P1 - Rendere il gate LONG molto piu' severo

Motivo: il drag e' coerente su paper, live, score bucket ed expected-R. LONG e'
negativo anche quando lo score e' alto.

Implementazione conservativa proposta:

- Aggiungere/rafforzare un gate expected-R direction/timeframe-aware per LONG.
- In alternativa, introdurre `directional_min_score_adjustments` per le celle
  LONG con expected-R negativo e campione sufficiente.
- Non bloccare tutti i LONG globalmente in modo permanente; partire da
  `15m`, `1h`, `4h`, `5m` e `1d` con soglie mode-aware.

File candidati:

- `backend/app/engine/edge_profile.py`
- `backend/app/engine/signal_engine.py`
- `backend/app/core/runtime_settings.py`
- test in `backend/tests/test_score_calibration.py`, `backend/tests/test_scoring.py`,
  `backend/tests/test_runtime_settings.py`

Rollback: disattivare il gate o riportare gli adjustment a `{}` via runtime
settings.

### P1 - Investigare `btc_macro_protection_loss_exit`

Motivo: nel live e' quasi tutto il PnL negativo: 44 trade, WR 2.3%, PnL -42.33.
Lo stop_loss live normale e' quasi break-even (PF 0.98, PnL -4.91), quindi il
problema non e' genericamente "stop troppo stretti".

Implementazione conservativa proposta:

- Aggiungere un report che separi: posizioni salvate, posizioni chiuse in perdita,
  prezzo/tempo di intervento, regime BTC, R-multiple al momento dell'uscita.
- Valutare soglie diverse per loss-exit live, ma solo dopo il report.

File candidati:

- `backend/app/engine/btc_macro_protection.py`
- `backend/app/engine/position_management.py`
- `backend/app/api/routers/analytics.py`
- test in `backend/tests/test_btc_macro_protection.py`

Rollback: feature flag sul report/gate, senza cambiare default finche' non
validato.

### P1 - Campionare candidati scartati dal `volatility_filter`

Motivo: negli ultimi worker cycle e' il primo skip reason. Senza log di shadow
outcome non sappiamo se stia tagliando rumore o edge.

Implementazione conservativa proposta:

- Salvare un campione limitato dei candidati scartati con market, timeframe,
  side previsto, score pre-filtro, ATR percentile, liquidity, source candle.
- Non aprire trade shadow; solo metadati sufficienti per confrontare outcome
  successivo.
- Rate limit e retention stretta per non gonfiare DB/log.

File candidati:

- `backend/app/engine/signal_engine.py`
- `backend/app/db/models.py` o `decision_logs` con event type dedicato e sampling
- `backend/app/api/routers/analytics.py`
- test in `backend/tests/test_signal_engine_edge.py`

Rollback: sampling off via setting.

### P1 - Lasciare completare A/B `15m:volume_profile`, poi queue mirata

Motivo: AutoTuner sta gia' testando una cella sensata. Interromperlo ora
aggiungerebbe rumore.

Queue consigliata dopo il test attuale:

1. Disabilitazione/reduzione `vwap` sui timeframe recenti peggiori.
2. Disabilitazione/reduzione `ema_crossover` dove recent PF resta sotto 0.6.
3. Disabilitazione/reduzione `support_resistance` se il drag recente persiste.
4. Revisione `fibonacci`: storico positivo ma recente molto negativo, quindi
   non va spento globalmente senza A/B.

File candidati:

- `backend/app/engine/auto_tuner.py`
- `frontend/components/calibration/AutoTunerTab.tsx`
- test in `backend/tests/test_auto_tuner.py`

Rollback: cancellare queue o rollback AutoTuner.

### P2 - Ridurre churn AutoCalibration

Motivo: 715 `applied`, risk setting mossi centinaia di volte, ma edge recente
ancora declining. Serve piu' isteresi/raffreddamento sui cambi frequenti.

Implementazione conservativa proposta:

- Cooldown per modifiche ripetute allo stesso risk setting.
- Min delta effettivo prima di emettere/applicare una modifica.
- Report UI che distingue "calibration observed" da "calibration changed".

File candidati:

- `backend/app/engine/calibration.py`
- `backend/app/worker/calibration_runner.py`
- `frontend/components/calibration/DiagnosticsTab.tsx`
- test in `backend/tests/test_calibration.py`, `backend/tests/test_worker_schedule.py`

Rollback: setting di cooldown a 0.

### P2 - Portare OOS/attribution in dashboard

Motivo: oggi l'informazione decisiva e' disponibile da script, ma non e' visibile
nel workflow operativo. La UI puo' evitare decisioni manuali basate solo su PnL
totale.

Implementazione proposta:

- Endpoint read-only per OOS summary compatto.
- Card in Calibration/Strategy Analysis con: robust/promising/negative/inert,
  train/test PF, recent PF, expected-R bucket.

File candidati:

- `backend/scripts/edge_oos_report.py` come base logica
- `backend/app/api/routers/analytics.py`
- `frontend/components/screens/StrategyAnalysisScreen.tsx`
- `frontend/lib/api.ts`

Rollback: nascondere la card senza toccare engine.

## Limiti metodologici

- L'attribuzione per strategia conta una posizione per ogni strategia che ha
  contribuito al segnale: i PnL si sovrappongono tra strategie.
- I candidati scartati da filtri precoci non hanno ancora outcome shadow, quindi
  non si puo' dire se `volatility_filter` stia tagliando edge o rumore.
- Molte celle OOS positive hanno train/test discordanti e campioni piccoli.
- I timeframe `30m` e `1d` hanno storico nel DB e `StrategyConfig`, ma il runtime
  corrente abilita `15m`, `1h`, `4h` e usa `5m` come monitoring-only.
- I dati live hanno notional molto inferiore al paper; il confronto PnL assoluto
  va letto con cautela, ma direction/timeframe e PF restano informativi.

## Comandi e verifiche usati

Comandi principali eseguiti in sola lettura:

```bash
cd backend
../venv/bin/python -m scripts.edge_oos_report
../venv/bin/python -m scripts.calibration_stability_report
```

In aggiunta sono state eseguite query SQLAlchemy read-only per:

- conteggi/range DB;
- performance paper/live all, 30d, last500, last150;
- daily PnL e drawdown;
- attribution per timeframe, direction, market, score bucket;
- live cost report;
- outcome profile;
- expected-R calibration;
- worker cycle skip reasons;
- state compatto di AutoCalibration/AutoTuner;
- heartbeat e runtime settings.

Nessun test e' stato necessario perche' non e' stata modificata logica
applicativa. Il report stesso e' l'unico artifact aggiunto.
