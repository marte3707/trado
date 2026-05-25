# Refactor Calibration & Strategies — 2026-05-21

Documento incrementale degli interventi su `AutoCalibrationEngine`, `AutoTuner`,
`SignalEngine` e correlati. Contesto di partenza: deep defensive attivo, 0 segnali,
`min_score=72`, auto-tuner abilitato ma nessuno step completato.

## Indice

1. [Sommario interventi](#sommario-interventi)
2. [Come funziona ora — calibrazione](#come-funziona-ora--calibrazione)
3. [Come funziona ora — auto-tuner](#come-funziona-ora--auto-tuner)
4. [Come funziona ora — signal engine](#come-funziona-ora--signal-engine)
5. [Walk-forward holdout validation](#walk-forward-holdout-validation)
6. [Settings nuovi/modificati](#settings-nuovimodificati)
7. [Endpoint nuovi](#endpoint-nuovi)
8. [Verifiche manuali consigliate](#verifiche-manuali-consigliate)
9. [Limiti consapevoli](#limiti-consapevoli)

---

## Sommario interventi

| # | Area | Modifica | File principali |
|---|------|----------|-----------------|
| §1.1 | calibration | Starvation counter ignora tick `active_timeframes=[]` (no-op) | [calibration.py](../backend/app/engine/calibration.py), [worker/main.py](../backend/app/worker/main.py) |
| §1.2 | calibration | `equity_slope_normalized` unit-less; soglia default `-2.0`→`-0.1` | [diagnostics.py](../backend/app/engine/diagnostics.py) |
| §1.3 | calibration | `preview()` ora in `SAVEPOINT` — read-only garantito | [calibration.py](../backend/app/engine/calibration.py) |
| §1.4 | auto-tuner | Confronto su `mean_pnl`; threshold in standard-error units | [auto_tuner.py](../backend/app/engine/auto_tuner.py) |
| §1.5/1.6 | signal | `_weight_pipeline` nel breakdown del segnale | [signal_engine.py](../backend/app/engine/signal_engine.py) |
| §1.8/1.9 | signal | `existing_signal` check upfront, prima delle HTTP esterne | [signal_engine.py](../backend/app/engine/signal_engine.py) |
| §1.10 | calibration | Wilson LB/UB; default `min_strategy_trades` 8→20 | [calibration.py](../backend/app/engine/calibration.py), [performance.py](../backend/app/engine/performance.py) |
| §1.11 | calibration | Auto-disable bookkeeping + retest dopo N trade | [calibration.py](../backend/app/engine/calibration.py) |
| §1.12 | calibration | `_strategy_params` rispetta low-WR/high-R strategies | [calibration.py](../backend/app/engine/calibration.py) |
| §2.2 | calibration | Cache `strategy_positions` condivisa nei sotto-metodi | [calibration.py](../backend/app/engine/calibration.py) |
| §2.3 | signal | Cache fundings/L2 per worker tick (`begin_cycle()`) | [signal_engine.py](../backend/app/engine/signal_engine.py), [worker/main.py](../backend/app/worker/main.py) |
| §3.7 | risk | `min_stop_distance_pct` scalato per timeframe (0.4%→3%) | [signal_engine.py](../backend/app/engine/signal_engine.py) |
| §3.8 | risk | Default `volatility_filter` range 25-90 (era 10-95) | [signal_engine.py](../backend/app/engine/signal_engine.py), [volatility.py](../backend/app/engine/volatility.py) |
| §3.9 | risk | MTF confluence "graded" via ATR distance | [mtf.py](../backend/app/engine/mtf.py) |
| §4.4 | observability | `skip_reasons` aggregato in `worker_cycle` event | [signal_engine.py](../backend/app/engine/signal_engine.py), [worker/main.py](../backend/app/worker/main.py) |
| §4.6 | signal | `disabled_signal_sources` esteso a `funding_rate`, `ob_imbalance`, `altcoin_season` | [signal_engine.py](../backend/app/engine/signal_engine.py) |
| §3.1 | calibration | Walk-forward holdout + factor stability tracking | [calibration.py](../backend/app/engine/calibration.py), [performance.py](../backend/app/engine/performance.py) |
| migration | DB | Upgrade idempotente dei vecchi default | [init_db.py](../backend/app/db/init_db.py) |

---

## Come funziona ora — calibrazione

### Flusso ad ogni run

1. **Carica posizioni chiuse paper** (limite `lookback_trades=500` default)
2. **Calcola `overall` stats** sulle posizioni
3. **Sotto la soglia `min_closed_trades=20` → skip** (registra `auto_calibration_skipped`)
4. **Cache condivisa**: `positions_by_timeframe[tf][strategy]` riempita una sola volta
5. **`_calibrate_strategy_weights`**:
   - Per ogni strategia disabilitata: se sono passati `auto_disable_retest_after_trades=200`
     trade dal disable → **re-enable** ("auto-retest")
   - Per ogni strategia attiva con `trades >= min_strategy_trades=20`:
     - Auto-disable se `Wilson UB(WR) < 0.30` (era: WR puntuale)
     - Altrimenti: factor da `_strategy_factor_holdout_validated`
       - Calcola `_strategy_factor(full_stats)` → `factor_full`
       - Splitta posizioni in train/test temporale (70/30)
       - Calcola `_strategy_factor(train_stats)` e `_strategy_factor(test_stats)`
       - Se **concordi** (entrambi sotto o sopra 1.0) → applica `factor_full`
       - Se **divergenti** → `factor = 1.0` ("holdout instability")
     - Stability check: registra il factor in `factor_history` (ultimi 5).
       Se `std_dev > 0.25` → freeze `factor = 1.0` ("stability freeze")
     - `new_weight = clamp(baseline_weight × factor, min_weight, max_weight)`
6. **`_calibrate_strategy_params`**:
   - Stessa logica holdout su `_strategy_params_for_stats`
   - Se train e test divergono nel "verso" (baseline vs defensive) → params restano baseline
   - I parametri defensive sono più selettivi (es. RSI oversold 30→25)
7. **`_calibrate_risk_settings`** (deep defensive / starvation):
   - Usa `equity_slope_normalized` (unit-less, mediana |PnL|)
   - Se `slope < -0.1` → entra in deep defensive: `min_score +12`, `min_RR +0.4`, `risk × 0.5`
   - Se starvation hard (15+ cicli senza segnali, escludendo i no-op) → ripristina baseline
   - Se starvation soft (10+ cicli) → attenua (+6, +0.2, risk × 0.75)
   - Se trend recente positivo (profit_factor recente > 1.0) → torna baseline
8. **Persistenza** del `factor_history`, `auto_disabled_at_trade_count`, deep defensive state
9. **DecisionLog** `auto_calibration_applied` / `auto_calibration_checked` / `auto_calibration_skipped`

### Wilson confidence bounds

Punto chiave: prima si decideva su `win_rate` puntuale; con 20 trade un WR 0.30 ha CI 95% che va da ~0.13 a ~0.55. Decidere di tagliare un weight a 0.6× su quel punto è sensibile al rumore. Ora:

- `wr_lb` = Wilson lower bound 95%
- `wr_ub` = Wilson upper bound 95%
- Decisioni "negative" (factor < 1.0, auto-disable) → richiedono `wr_ub < soglia` (anche nel migliore dei casi)
- Decisioni "positive" (factor > 1.0) → richiedono `wr_lb >= soglia` (anche nel peggiore dei casi)

### `factor_history` e stability freeze

Per ogni strategia tracciamo gli ultimi 5 factor decisi. La standard deviation di questa serie è un proxy di overfitting:
- σ > 0.25 → il sistema sta oscillando tra "0.6 → 1.2 → 0.6" sui dati recenti → freeze a 1.0
- σ ≤ 0.25 → variazioni piccole → coerente, lascia passare

---

## Come funziona ora — auto-tuner

Il sequence-based tuner attiva uno step (es. `regime_filter`), aspetta `min_trades_per_step` posizioni post-attivazione, poi valuta:

1. `metrics_before`: snapshot al momento dell'attivazione (`min_trades` posizioni prima)
2. `metrics_after`: stats su posizioni chiuse dopo `activated_at`
3. **Threshold**:
   - Base: `improvement_threshold` (default 0)
   - Adattiva (se `adaptive_threshold_multiplier > 0`):
     `multiplier × std_pnl / sqrt(n)` = standard error del mean
   - `threshold = max(base, adaptive)`
4. **`improved = mean_pnl_after >= mean_pnl_before + threshold`**
5. Se `improved` → `auto_tuner_step_success`, prossimo step
6. Altrimenti → applica `revert` patch, `auto_tuner_step_reverted`

### Pre-fix vs post-fix

| Aspetto | Pre-fix | Post-fix |
|---------|---------|----------|
| Metrica confronto | `equity_slope` (USD/trade-index) | `mean_pnl` (USD/trade) |
| Threshold adattiva | `multiplier × std_pnl` | `multiplier × std_pnl / sqrt(n)` |
| Unità coerenti | ❌ slope vs std (incomparabili) | ✅ mean vs SE |

Il bug pre-fix faceva sì che la threshold fosse quasi sempre troppo alta → tutti gli step revertati ("nessuno step completato" nella memory di sessione).

---

## Come funziona ora — signal engine

### Ordine operazioni per `generate(market, timeframe)`

1. Carica candele (min 60)
2. **Volatility filter** (default 25-90 ATR percentile rank)
3. Liquidity check
4. **`existing_signal` check upfront** (era a metà funzione)
   - Se esiste segnale per la stessa candela → `return None` immediato
   - Evita HTTP esterne quando non servono
5. Esegue strategie attive del timeframe → `results`, `weights = {strategy_name: weight}`
6. Source esterne (con guardia `disabled_signal_sources`):
   - `fear_greed` (cache globale)
   - `altcoin_season` (toggleabile)
   - `whale_flow` (toggleabile)
   - `funding_rate` (toggleabile, cache per cycle)
   - `ob_imbalance` (toggleabile, cache L2 per cycle)
7. `_persist_indicators` (snapshot indicators per candela)
8. **Weight pipeline** (snapshot in breakdown):
   - `calibrated_weights` ← DB
   - `performance_adjusted_weights` ← Wilson-based factor
   - `regime_filtered` ← ADX-based filter (es. trend-following neutralizzate in ranging)
9. `scoring.score(results, weights, participation_ratio)`
10. **Filter gates** (ciascuno incrementa il counter `skip_reasons`):
    - `funding_block` (se threshold configurato)
    - `whale_contra_flow` (se whale flow contrario con high confidence)
    - `low_score` (score < min_signal_score)
    - `mtf_confluence` (close oltre 0.5 ATR nel verso sbagliato sul TF superiore)
11. `risk.build_plan(min_stop_distance_pct_per_tf, ...)` con stop scalato per timeframe
12. `risk_reward` check
13. Persist `Signal`

### `_weight_pipeline` nel breakdown

Esempio strategia `ema_crossover` su 5m:
```json
"_weight_pipeline": {
  "ema_crossover": {
    "calibrated": 14,
    "performance_adjusted": 14,
    "regime_filtered": 14
  }
}
```

Se vedi `0` in `regime_filtered` → il regime detection ha neutralizzato la strategia.
Se vedi `7` in `performance_adjusted` e `14` in `calibrated` → la performance recente ha penalizzato.

### Stop scaling per timeframe

| Timeframe | min_stop_pct |
|-----------|--------------|
| 1m | 0.4% |
| 5m | 0.7% |
| 15m | 1.0% |
| 30m | 1.3% |
| 1h | 1.6% |
| 2h | 1.8% |
| 4h | 2.2% |
| 1d | 3.0% |

Prima era 2.0% fisso per tutti i TF. Override possibile via `risk.min_stop_distance_pct` in `AppSetting`.

### MTF confluence "graded"

Prima: binario (close > EMA50 = pass per long, altrimenti block).
Ora: blocca solo se `(close - ema50) / atr14 < -0.5` per long (e specularmente per short). La zona di "indecisione" (entro 0.5 ATR dalla EMA) passa, evitando di bloccare segnali validi in transizioni di trend.

### `skip_reasons` aggregati

Ogni `worker_cycle` event ora include:
```json
"skip_reasons": {
  "volatility_filter": 12,
  "low_score": 45,
  "duplicate": 8,
  "risk_reward": 3
}
```

Tornano utile in produzione (dove i debug log sono filtrati) per capire dove muoiono i segnali.

---

## Walk-forward holdout validation

Vedere `_strategy_factor_holdout_validated` e `_strategy_params_holdout_validated` in
[calibration.py](../backend/app/engine/calibration.py).

### Premessa

Senza un backtester che simuli le candele storiche attraverso le strategie, il walk-forward "puro" non è possibile. La validation implementata è una **naive temporal holdout**: le posizioni più vecchie (70%) sono il training set, le più recenti (30%) sono il test set. Una decisione di calibration viene applicata SOLO se regge su entrambi i set.

### Esempio concreto

Strategia `rsi` con 30 posizioni chiuse:
- **Train (21 posizioni più vecchie)**: 14 win, 7 loss → WR 0.67, PF 1.8 → `factor_train = 1.2`
- **Test (9 posizioni più recenti)**: 2 win, 7 loss → WR 0.22, PF 0.4 → `factor_test = 0.6`
- **Concordance**: `factor_train >= 1.0` ma `factor_test < 1.0` → **NON concordi**
- Risultato: `factor = 1.0` con reason `holdout instability: train_factor=1.20 vs test_factor=0.60`

Il sistema riconosce che la "buona performance" della strategia è dovuta a un periodo storico che NON sta più funzionando → non promuovere il peso.

### Endpoint diagnostico

`GET /analytics/calibration/stability` ritorna per ogni `(timeframe:strategy)`:
- `trades`: numero totale posizioni
- `holdout`: stats train vs test (con `wr_delta`)
- `factor_history`: lista degli ultimi factor decisi
- `factor_stddev`: σ dei factor recenti
- `stable`: bool (σ ≤ soglia configurata)

---

## Settings nuovi/modificati

### `auto_calibration` (chiave `AppSetting`)

| Key | Default vecchio | Default nuovo | Note |
|-----|-----------------|---------------|------|
| `lookback_trades` | 500 | 500 | invariato |
| `min_closed_trades` | 20 | 20 | invariato |
| `min_strategy_trades` | 8 | **20** | raised: 8 trade è statisticamente insignificante |
| `min_signal_score_floor` | 55 | 55 | invariato |
| `min_signal_score_ceiling` | 80 | 80 | invariato |
| `min_risk_reward_floor` | 2.0 | 2.0 | invariato |
| `min_risk_reward_ceiling` | 3.0 | 3.0 | invariato |
| `min_risk_per_trade_pct` | 0.25 | 0.25 | invariato |
| `deep_defensive_slope_threshold` | -2.0 | **-0.1** | unit-less (normalized slope) |
| `auto_disable_min_trades` | 25 | 25 | invariato |
| `auto_disable_win_rate` | 0.30 | 0.30 | invariato (ora applicato come Wilson UB) |
| `auto_disable_retest_after_trades` | — | **200** | **nuovo**: re-enable dopo N trade |
| `walk_forward_enabled` | — | **True** | **nuovo**: holdout validation |
| `walk_forward_train_ratio` | — | **0.7** | **nuovo** |
| `walk_forward_min_per_side` | — | **5** | **nuovo** |
| `factor_history_window` | — | **5** | **nuovo**: ultimi N factor tracciati |
| `factor_history_std_threshold` | — | **0.25** | **nuovo**: σ sopra cui freeze |
| `starvation_soft_cycles` | 10 | 10 | invariato |
| `starvation_hard_cycles` | 15 | 15 | invariato |
| `min_trades_for_slope` | 10 | 10 | invariato |
| `recent_window` | 50 | 50 | invariato |
| `recent_slope_exit_cycles` | 3 | 3 | invariato |

### Migrazione automatica

[init_db.py](../backend/app/db/init_db.py) all'avvio backend:
- Se il DB esistente ha `deep_defensive_slope_threshold == -2.0` → aggiorna a `-0.1`
- Se ha `min_strategy_trades == 8` → aggiorna a `20`
- Se manca `auto_disable_retest_after_trades` → aggiunge `200`

Le altre chiavi (walk-forward, factor history) sono lette con `settings.get(key, default)` quindi non servono migration esplicite.

### `risk` (chiave `AppSetting`)

Nuove chiavi opzionali leggibili da signal engine:
- `disabled_signal_sources`: lista estesa (ora include `funding_rate`, `ob_imbalance`, `altcoin_season`)
- `min_stop_distance_pct`: override globale; se non set, usa lo scaling per timeframe
- `atr_pct_min`: default 0.25 (era 0.10)
- `atr_pct_max`: default 0.90 (era 0.95)

---

## Endpoint nuovi

### `GET /analytics/calibration/stability`

Diagnostica walk-forward + factor stability. Esempio risposta:
```json
{
  "settings": {
    "walk_forward_train_ratio": 0.7,
    "walk_forward_min_per_side": 5,
    "factor_history_window": 5,
    "factor_history_std_threshold": 0.25
  },
  "by_strategy": [
    {
      "key": "15m:rsi",
      "timeframe": "15m",
      "strategy": "rsi",
      "enabled": true,
      "weight": 9,
      "trades": 30,
      "factor_history": [1.0, 1.0, 0.8, 1.0, 1.0],
      "factor_stddev": 0.08,
      "stable": true,
      "holdout": {
        "train": { "trades": 21, "win_rate": 0.43, "profit_factor": 0.85, ... },
        "test": { "trades": 9, "win_rate": 0.56, "profit_factor": 1.32, ... },
        "wr_delta": 0.13
      }
    }
  ]
}
```

### Eventi `worker_cycle` arricchiti

Ora includono `skip_reasons: {reason: count}`. Visibile da `GET /analytics/worker-cycles`.

---

## Verifiche manuali consigliate

### 1. Riavvio servizi backend/worker

Necessario perché le modifiche backend richiedono riavvio (vedi `CLAUDE.md`):
```bash
launchctl bootout gui/$(id -u)/com.trado.backend
launchctl bootout gui/$(id -u)/com.trado.worker
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.backend.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.worker.plist
```

All'avvio backend, la migrazione idempotente aggiorna i 3 default modificati.

### 2. Conferma migrazione settings

```bash
curl -s http://localhost:8000/settings/auto-calibration | jq '.deep_defensive_slope_threshold, .min_strategy_trades, .auto_disable_retest_after_trades'
```
Deve mostrare `-0.1`, `20`, `200`.

### 3. Stato deep defensive

```bash
curl -s http://localhost:8000/analytics/calibration | jq '.state.deep_defensive_active, .state.last_equity_slope, .state.consecutive_zero_signal_cycles'
```

Atteso (post-fix): `deep_defensive_active=false` se il counter starvation non era artificialmente gonfiato (no-op tick esclusi). `last_equity_slope` ora è normalizzato (valori tipici tra -1 e 1).

### 4. Skip reasons in worker_cycle

```bash
curl -s http://localhost:8000/analytics/worker-cycles?limit=5 | jq '.[].data.skip_reasons'
```

Mostra dove muoiono i segnali. Se vedi `volatility_filter` molto alto → il filtro è troppo stretto per i tuoi market. Se `risk_reward` alto → il min_RR + stop scaling potrebbe non lasciar passare RR ≥ 2.0.

### 5. Walk-forward stability

```bash
curl -s http://localhost:8000/analytics/calibration/stability | jq '.by_strategy[] | {key, factor_stddev, stable, wr_delta: .holdout.wr_delta}'
```

Strategie con `stable: false` (σ > 0.25) sono congelate. Strategie con `|wr_delta| > 0.2` hanno performance instabili tra train e test → la calibration eviterà di modificarle.

### 6. Preview calibration senza side effects

```bash
curl -s http://localhost:8000/analytics/calibration | jq '.changes'
```

Ora questa chiamata è garantita read-only (SAVEPOINT + rollback interno). Se vedi `changes` non vuoto, la prossima `run` reale farebbe quegli aggiustamenti.

### 7. Force calibration con dati freschi

Se vuoi forzare una nuova calibrazione (es. dopo aver modificato manualmente i pesi):

```bash
# Reset baseline (cattura nuovo punto di partenza)
curl -X POST http://localhost:8000/auto-calibration/baseline/reset

# Force run
curl -X POST http://localhost:8000/auto-calibration/run
```

### 8. Auto-tuner sblocco

Se l'auto-tuner non avanzava per il bug §1.4:
```bash
curl -s http://localhost:8000/auto-tuner/status | jq '.current_step_key, .progress'
curl -X POST http://localhost:8000/auto-tuner/run  # forza valutazione
```

Ora con la threshold corretta gli step possono progredire (success o reverted), non restano "in_progress" perennemente.

### 9. Performance HTTP

In `worker_cycle` il numero di chiamate L2 + fundings era N×M (markets × timeframes). Ora è O(N) per L2 (1 per market) e O(1) per fundings (1 per tick). Se hai metriche di latency del tick worker, dovresti vedere un calo significativo.

---

## §2.1 — Vectorization indicators: investigation e non-applicabile

Tentativo di vectorizzazione con numpy 2.4.6 di [indicators/core.py](../backend/app/indicators/core.py). **Risultato: revertato** dopo benchmark empirico.

### Benchmark (Python 3.14.4, N=250 candele, 1000 iterazioni)

| Indicator | Pure-Python | Numpy vectorized | Speedup                    |
|-----------|-------------|------------------|----------------------------|
| EMA(21)   | 8 ms        | 140 ms           | **0.06×** (15× più lento!) |
| SMA(20)   | 42 ms       | 109 ms           | 0.39×                      |
| BB(20,2)  | 385 ms      | 513 ms           | 0.75×                      |

Speedup invariato anche per N=500/1000/2500/5000 — l'overhead di `np.asarray()` + `np.empty()` + `np.cumsum()` non si ammortizza per la nostra finestra di lookback (250 punti).

### Diagnosi

1. **EMA è intrinsecamente ricorrente** (`prev = (val - prev) × mult + prev`) → non vettorizzabile in one-shot, solo loop scalare con array numpy che paga overhead di indexing senza beneficio
2. **N=250 è troppo piccolo** per ammortizzare l'overhead di array creation numpy; il sweet spot tipico è N > 10⁴
3. **`_none_padded_list` finale richiede loop scalare** per gestire None warmup — perde parte del guadagno
4. **Python 3.14 ha tail-call optimization** e altre ottimizzazioni che rendono pure-Python più competitivo

### Misurazione del peso reale degli indicators

Suite completa (EMA×2 + RSI + MACD + ATR + ADX + Bollinger + VWAP + Supertrend) su N=250: **2.5 ms per call**.

Per uno scenario tipico di worker tick (50 market × 3 timeframe = 150 generate calls):

- Tempo indicators: 150 × 2.5 ms = **375 ms per tick**
- Worker sleep tra tick: 60 s
- **Indicators = 0.6% del wall-clock** — non è un collo di bottiglia

Il vero collo di bottiglia erano le chiamate HTTP esterne (fundings/L2 book ripetuti per (market × timeframe)), risolto in §2.3 con il caching per-cycle.

### Decisione

- **Revert** all'implementazione pure-Python originale
- **Rimossa** la dipendenza `numpy` da [pyproject.toml](../backend/pyproject.toml)
- **3 test di robustness** aggiuntivi mantenuti in [test_indicators.py](../backend/tests/test_indicators.py):
  - `test_atr_finite_for_diverse_series`
  - `test_rsi_bounded_for_long_series`
  - `test_adx_monotonic_in_trending_series`

### Quando numpy diventerebbe utile

Solo se in futuro:

- La finestra di lookback crescesse a 10⁴+ candele (es. backtest historico, non signal live)
- Si introducessero indicators **non ricorrenti** con rolling window grandi (volume profile, market profile)
- Si calcolassero indicators **batch su tutti i market simultaneamente** invece che per-market (richiede ristrutturazione architetturale)

Per ora, lo status quo pure-Python è ottimale per il workload di produzione.

---

## §4.8 — Nuove strategie

Aggiunte 3 strategie nel registry, abilitate di default su tutti i timeframe seedati,
con peso conservativo (8-10). La calibrazione le promuoverà/penalizzerà via Wilson-based
factor dopo ≥ 20 trade chiusi.

### `cvd_divergence` — Cumulative Volume Delta divergence

File: [cvd_divergence_strategy.py](../backend/app/strategies/cvd_divergence_strategy.py)

**Limite noto**: Hyperliquid candles non hanno il trade-tape (lato aggressivo). Approssimiamo con `sign(close - open) × volume` ("Bull-Bear Volume Delta" — convenzione retail standard, ben correlata col CVD vero ma rumorosa su doji).

**Logica**:

- Splitta la finestra `lookback=30` in due metà cronologiche
- Calcola CVD cumulativo su ogni candela e confronta min/max train vs test
- **Bullish divergence**: prezzo fa nuovi MINIMI nella seconda metà MA il CVD nella stessa metà non scende sotto la prima metà → meno pressione di vendita aggressiva → LONG
- **Bearish divergence**: prezzo nuovi MASSIMI MA CVD scende → SHORT

**Params** (defaults seedati):

- `lookback: 30` — finestra di analisi
- `min_divergence_pct: 0.005` — divergenza minima del prezzo per scattare (0.5%)

**Weight default**: 8

### `liquidation_cascade` — Detection cascata liquidazioni

File: [liquidation_cascade_strategy.py](../backend/app/strategies/liquidation_cascade_strategy.py)

**Limite noto**: senza liquidation feed diretto, rileviamo il PATTERN OHLCV post-cascata invece dell'evento stesso.

**Pattern atteso (3 condizioni simultanee)**:

1. Volume eccezionale (≥ `volume_factor=2.5×` la media `lookback=50`)
2. Range candle estremo (≥ `range_factor=2.0×` ATR(14))
3. Wick lungo nella direzione "sbagliata" + close vicino al lato opposto del range → assorbimento

**Direzioni**:

- **LONG**: lower_wick_ratio ≥ 0.55 + close nella metà alta (close_position ≥ 0.65) → spike down assorbito = liquidazione long completata
- **SHORT**: upper_wick_ratio ≥ 0.55 + close nella metà bassa (close_position ≤ 0.35) → spike up assorbito = liquidazione short completata

**Differenza vs Wyckoff Spring/Upthrust**: non richiede livelli S/R pre-definiti; cerca solo il climax volume+wick su qualsiasi swing.

**Weight default**: 10

### `volume_profile` — POC interaction (VPVR approssimato)

File: [volume_profile_strategy.py](../backend/app/strategies/volume_profile_strategy.py)

**Limite noto**: senza tick-by-tick, distribuiamo il volume di ogni candela uniformemente tra `low` e `high` (approssimazione standard "TPO-volume-style" usata da molte piattaforme retail).

**Costruzione**:

1. Discretizza il range `lookback=100` in `bins=30` fasce di prezzo
2. Spalma il volume di ogni candela sui bin coperti dal suo range
3. **POC** (Point of Control) = bin con volume massimo
4. **HVN** = bin nei top `hvn_percentile=80%` della distribuzione

**Segnali**:

- **LONG (reclaim)**: previous_close < POC < latest_close, near_poc (distanza ≤ `proximity_pct=0.4%`)
- **SHORT (loss)**: previous_close > POC > latest_close, near_poc
- Confidence +10 se POC è anche HVN (volume picco rilevante)

**Weight default**: 9

### Files creati / modificati

- 3 nuovi: [cvd_divergence_strategy.py](../backend/app/strategies/cvd_divergence_strategy.py), [liquidation_cascade_strategy.py](../backend/app/strategies/liquidation_cascade_strategy.py), [volume_profile_strategy.py](../backend/app/strategies/volume_profile_strategy.py)
- [registry.py](../backend/app/strategies/registry.py): import + lista
- [init_db.py](../backend/app/db/init_db.py): `DEFAULT_STRATEGIES` + `DEFAULT_STRATEGY_WEIGHTS`
- Nuovo test file [test_new_strategies.py](../backend/tests/test_new_strategies.py) con 11 test

### Verifiche post-deploy

Dopo il riavvio servizi backend/worker, le 3 strategie saranno seedate automaticamente in `StrategyConfig` per ogni timeframe abilitato. Per disabilitarne una manualmente:

```bash
# Esempio: disabilita liquidation_cascade su 5m
curl -X PATCH http://localhost:8000/strategies \
  -H 'Content-Type: application/json' \
  -d '{"strategy_name":"liquidation_cascade","timeframe":"5m","enabled":false}'
```

Dopo qualche giorno di trading, controllare la stability page:

```bash
curl -s http://localhost:8000/analytics/calibration/stability \
  | jq '.by_strategy[] | select(.strategy | test("cvd|liquidation|volume_profile"))'
```

Se una strategia ha `wr_delta` molto negativo (test set peggiore del train) → la calibration la freezerà; se ha `factor_stddev > 0.25` → stability freeze automatico.

---

## Limiti consapevoli

Cose che NON sono state implementate in questa sessione (refactor troppo grossi):

| Riferimento audit | Cosa serve | Perché non fatto |
|-------------------|------------|------------------|
| §3.1 walk-forward "puro" | Backtester che simula candele storiche attraverso strategie + risk + execution | Refactor di settimane. Holdout naive copre l'80% del beneficio |
| §2.1 vectorization indicators | Riscrivere [indicators/core.py](../backend/app/indicators/core.py) con numpy/pandas-ta | **Tentato e revertato**: vedi sezione §2.1 |
| §4.8 nuove strategie | CVD, liquidation cascade, volume profile, funding divergence | **3 su 4 implementate**: vedi sezione §4.8. `funding_divergence` skipped — `funding_rate` esiste già come segnale esterno, una versione "divergence vs price" sarebbe duplicato |
| §4.9 regime detection | Hurst exponent, GARCH | Sostituire ADX richiede analisi e backtest |
| §3.10 A/B auto-tuner | Confronto parallelo on/off vs sequenziale | Richiede shadow engine |

---

## Test coverage

204 test passano (197 pre-sessione + 7 nuovi per walk-forward):

- `test_holdout_split_stats_separates_train_and_test_temporally`
- `test_holdout_split_stats_returns_none_when_insufficient_samples`
- `test_holdout_blocks_change_when_train_and_test_disagree`
- `test_holdout_passes_when_train_and_test_agree`
- `test_holdout_disabled_returns_full_factor_directly`
- `test_time_weighted_summarize_weights_recent_higher`
- `test_factor_stability_freezes_change_when_history_oscillates`

Test esistenti aggiornati per la nuova signature (Wilson, holdout opzionale):
- `test_strategy_factor_penalizes_unprofitable_strategy`
- `test_auto_disable_chronic_underperformer_with_safety_floor`
- `test_calibrate_strategy_params_updates_strategy_config_params`

Typecheck (mypy) e lint (ruff) puliti sui file toccati.
