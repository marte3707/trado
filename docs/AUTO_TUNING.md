# Piano operativo: Auto-Calibration + Auto-Tuner

Questo documento descrive il meccanismo automatico di calibrazione e tuning dei
parametri del signal engine, e i passi manuali consigliati quando si vuole
osservare/intervenire sul processo.

## Componenti

Il sistema ha tre livelli di automazione gerarchici. Più alto = più conservativo,
più basso = più aggressivo nell'intervenire sui parametri.

### 1. AutoCalibrationEngine

File: `backend/app/engine/calibration.py`

Cosa fa, automaticamente ad ogni ciclo del worker dopo che almeno una posizione
si chiude (o quando il numero di trade chiusi totali cambia rispetto all'ultima
run):

1. Calcola le statistiche storiche dei trade chiusi in modalità paper
   (`win_rate`, `realized_pnl`, `profit_factor`) e dell'equity slope (regressione
   lineare sulla curva del PnL cumulato).
2. Confronta i risultati con la **baseline** salvata in
   `AppSetting("auto_calibration_baseline")`. La baseline viene creata al primo
   run e contiene i valori di partenza di `risk` settings, `strategy_weights` e
   `strategy_params`.
3. Aggiorna le `StrategyConfig` (peso + parametri) e i `risk` settings in
   funzione delle performance:
   - **Defensive mode** quando `win_rate < 0.4` o `pnl < 0` o
     `profit_factor < 0.9`: alza `min_signal_score` di +8, alza
     `min_risk_reward` di +0.25, riduce `max_risk_per_trade_pct` di 25%.
   - **Deep defensive** quando `equity_slope < deep_defensive_slope_threshold`
     (default −2.0 USD/trade): score +12, RR +0.4, risk_pct ×0.5. Per uscirne
     servono **2 run consecutive con slope ≥ 0** (isteresi gestita via
     `auto_calibration_state.deep_defensive_positive_streak`).
   - **Restore baseline** quando `win_rate ≥ 0.58` e `pnl > 0` e
     `profit_factor ≥ 1.2`.
4. **Auto-disable strategie** che con ≥ 25 trade chiusi hanno
   `win_rate < 0.30`. Safety floor: almeno 1 strategia per timeframe rimane
   sempre attiva (anche se sotto soglia).

Limite: questa calibrazione **aggiusta solo soglie e pesi**, non attiva
filtri strutturali sul signal engine (regime, MTF, volatilità, participation
ratio). Per quelli c'è il livello successivo.

### 2. AutoTuner

File: `backend/app/engine/auto_tuner.py`

Cosa fa, quando attivato esplicitamente: prende una **sequenza di filtri
opt-in** e li attiva uno alla volta, aspettando un numero minimo di trade
chiusi tra un'attivazione e la successiva. Dopo l'attesa, confronta lo slope
dell'equity post-attivazione con quello pre-attivazione. Se è migliorato (o
non peggiorato oltre la soglia) lo mantiene attivo e passa al successivo;
altrimenti fa rollback automatico (`revert` patch) e marca lo step come fallito.

Esecuzione automatica: dopo ogni `AutoCalibrationEngine.run()` il worker
invoca `AutoTuner().run(db)` (vedi `backend/app/worker/main.py`). L'esecuzione
è no-op se `auto_tuner_config.enabled` è `false`.

Sequenza di default (`AppSetting("auto_tuner_config").sequence`):

| # | Step                   | Patch                                | Effetto |
|---|------------------------|--------------------------------------|---------|
| 1 | `regime_filter`        | `regime_filter_enabled: true`        | ADX(14) classifica trending/ranging/mixed; neutralizza strategie trend-following in ranging e mean-reverting in trending. |
| 2 | `mtf_confluence`       | `mtf_confluence_enabled: true`       | Su un LONG 15m richiede `close > EMA50` sul TF superiore (1h); idem viceversa per SHORT. |
| 3 | `volatility_filter`    | `volatility_filter_enabled: true`    | Percentile dell'ATR rispetto alle ultime ~50 candele. Scarta se `< 0.10` o `> 0.95`. |
| 4 | `participation_ratio_50` | `participation_ratio: 0.50`        | Da 0.35 (default) a 0.50: richiede maggior consenso fra le strategie. |

Configurazione (`AppSetting("auto_tuner_config")`):

```json
{
  "enabled": false,
  "min_trades_per_step": 50,
  "improvement_threshold": 0.0,
  "sequence": [ ... ]
}
```

- `min_trades_per_step`: trade chiusi *dopo* l'attivazione di uno step prima di
  valutare l'effetto. Default 50.
- `improvement_threshold`: `slope_after` deve essere `≥ slope_before + threshold`
  per considerare lo step un successo. Default 0 (non peggioramento).

Stato (`AppSetting("auto_tuner_state")`):

```json
{
  "current_step_index": 0,
  "current_step_key": "regime_filter",
  "activated_at": "2026-05-15T13:00:00Z",
  "trades_at_activation": 88,
  "metrics_before": { "win_rate": 0.114, "equity_slope": -139.95 },
  "risk_snapshot_before": { ... },
  "step_history": [ ... ]
}
```

### 3. Intervento manuale

Anche con AutoTuner attivo, mantieni queste leve:

- `POST /api/auto-tuner/pause` — disabilita il tuner senza fare rollback dello
  step corrente.
- `POST /api/auto-tuner/resume` — riabilita.
- `POST /api/auto-tuner/rollback` — fa rollback dello step attualmente in test
  e libera lo slot per il prossimo.
- `POST /api/auto-tuner/reset` — azzera la history (lo step corrente, se c'è,
  viene prima reverted). Utile dopo modifiche maggiori al codice.
- `PATCH /api/settings` su `risk` — qualunque setting (incluso quelli toccati
  dal tuner) può essere sovrascritto manualmente.
- `POST /api/auto-calibration/baseline/reset` — cancella la baseline frozen
  così la prossima calibrazione la ricostruisce dai valori correnti.

## Flusso end-to-end consigliato

### Fase A — Diagnosi iniziale (sempre prima di intervenire)

1. `GET /api/analytics/diagnostics?lookback=200`
2. Verifica:
   - `trade_count >= 20` (altrimenti la calibrazione non si attiva).
   - `exit_reason_distribution`: se `stop_loss` è > 70% delle chiusure, root cause
     molto probabile = SL stretti. In tal caso, **prima** di attivare il tuner
     allarga gli SL con `min_atr_multiplier` e `min_stop_distance_pct`.
   - `planned_stop_pct.p25`: se è esattamente uguale a `min_stop_distance_pct`,
     il floor sta dominando. Alzalo.
   - `equity_slope`: segno e magnitudine. Negativo grande → drawdown in corso.
   - `baseline_vs_current.risk`: vedi quanto la calibrazione si è già allontanata
     dalla baseline.
3. Salva l'output del diagnostics da qualche parte (commit `/tmp` o copia
   manuale) per confronto post-tuning.

### Fase B — Intervento mirato sugli SL (manuale, una tantum)

Solo se la diagnosi mostra SL stretti come fattore dominante:

```bash
curl -X PATCH http://localhost:8000/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"risk":{"value":{
    ...incolla qui il dict risk corrente...,
    "min_atr_multiplier": 2.5,
    "min_stop_distance_pct": 0.030
  }}}'
```

Poi forza una calibrazione:

```bash
curl -X POST http://localhost:8000/api/auto-calibration/run
```

Questo lascia che la calibrazione veda i nuovi valori di SL e:
- disabiliti le strategie cronicamente perdenti (auto-disable);
- entri in deep defensive se l'equity slope è ancora pesantemente negativo.

### Fase C — Attivazione AutoTuner

Quando vuoi che il sistema provi automaticamente i filtri opt-in:

```bash
curl -X POST http://localhost:8000/api/auto-tuner/resume
```

Questo set `auto_tuner_config.enabled = true`. Dal tick successivo del worker:

1. Il tuner avvia lo step 1 (`regime_filter`), modifica `risk.regime_filter_enabled = true`,
   registra `metrics_before` (slope, wr, pf delle 50 trade più recenti) e
   `trades_at_activation`. Log: `auto_tuner_step_started`.
2. Ad ogni tick successivo verifica se sono arrivati `min_trades_per_step`
   trade chiusi dopo l'attivazione.
3. Quando soglia raggiunta, calcola `metrics_after` sui soli trade post-attivazione
   e confronta `slope_after` vs `slope_before`.
4. Se `slope_after >= slope_before + improvement_threshold` → step success,
   `risk.regime_filter_enabled` rimane `true`, lo step viene marcato nella history,
   parte lo step 2. Log: `auto_tuner_step_success`.
5. Altrimenti → rollback automatico (`regime_filter_enabled = false`), step
   marcato come `reverted`, parte lo step 2. Log: `auto_tuner_step_reverted`.
6. Quando tutti gli step sono stati provati, il tuner si ferma. Log:
   `auto_tuner_finished`.

### Fase D — Monitoraggio durante il tuning

In qualsiasi momento:

```bash
curl -s http://localhost:8000/api/auto-tuner/status
```

Restituisce:

```json
{
  "enabled": true,
  "current_step_key": "regime_filter",
  "activated_at": "2026-05-15T13:00:00Z",
  "metrics_before": { "equity_slope": -139.95, "win_rate": 0.114 },
  "progress": { "trades_done": 17, "trades_required": 50 },
  "step_history": [ ... ]
}
```

`progress` indica quanti trade chiusi sono ancora necessari per valutare lo step.

Per ispezionare il decision log (filtra in DB o frontend):
- Eventi: `auto_tuner_step_started`, `auto_tuner_step_success`,
  `auto_tuner_step_reverted`, `auto_tuner_paused`, `auto_tuner_resumed`,
  `auto_tuner_manual_rollback`, `auto_tuner_reset`, `auto_tuner_failed`.

### Fase E — Verifica finale

A tuning completato:

1. `GET /api/auto-tuner/status` → `current_step_key: null`,
   `step_history` con i 4 step e i loro outcome.
2. `GET /api/analytics/diagnostics` → confronto con baseline iniziale di Fase A:
   - `exit_reason_distribution`: la frazione di `stop_loss` dovrebbe essere
     ridotta.
   - `overall.win_rate` e `overall.profit_factor`: idealmente migliorati.
   - `equity_slope`: tendenza al positivo (o almeno meno negativo).
3. Strategie attive: `GET /api/strategies` per vedere quali sono state
   disabilitate automaticamente.

## Sicurezza e rollback rapido

Pannico immediato — voglio fermare tutto:

```bash
# Disabilita auto tuner (lo step corrente rimane applicato)
curl -X POST http://localhost:8000/api/auto-tuner/pause

# Rollback dello step corrente
curl -X POST http://localhost:8000/api/auto-tuner/rollback

# In aggiunta, se serve, ripristina manualmente filtri off
curl -X PATCH http://localhost:8000/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"risk":{"value":{
    "regime_filter_enabled": false,
    "mtf_confluence_enabled": false,
    "volatility_filter_enabled": false,
    "participation_ratio": 0.35
  }}}'
```

## Tracciamento eventi (DecisionLog)

Ogni cambio del tuner / calibrazione produce una entry in `decision_logs`.
Tipi di evento rilevanti:

| Event type                              | Significato |
|----------------------------------------|---|
| `auto_calibration_applied`             | Calibrazione ha modificato uno o più setting/strategia. |
| `auto_calibration_checked`             | Calibrazione girata, nessun cambio necessario. |
| `auto_calibration_skipped`             | Saltata (disabled o trade insufficienti). |
| `auto_calibration_failed`              | Eccezione durante run(). |
| `auto_calibration_baseline_reset`      | Baseline cancellata manualmente. |
| `auto_tuner_step_started`              | Step attivato dal tuner. |
| `auto_tuner_step_success`              | Step mantenuto perché migliorativo. |
| `auto_tuner_step_reverted`             | Step revertito automaticamente. |
| `auto_tuner_manual_rollback`           | Rollback manuale via API. |
| `auto_tuner_paused` / `auto_tuner_resumed` | Toggle config.enabled. |
| `auto_tuner_finished`                  | Sequenza completata. |
| `auto_tuner_failed`                    | Eccezione durante run(). |
| `auto_tuner_reset`                     | Stato azzerato. |

## Quando NON usare l'AutoTuner

- Quando il volume di trade è basso (es. < 50 trade chiusi negli ultimi giorni):
  il segnale rispetto al rumore è troppo basso, il tuner farà rollback random.
  Aspetta di avere più dati o riduci `min_trades_per_step` con consapevolezza.
- Durante incidenti di mercato (flash crash, manutenzione exchange): l'equity
  slope diventa inaffidabile come metrica. `POST /api/auto-tuner/pause`.
- Subito dopo modifiche maggiori al codice del signal engine: dai un ciclo di
  calibrazione "normale" prima, poi `POST /api/auto-tuner/reset` per ripartire
  pulito.

## Trade-off di design

- **Solo equity slope come metric di successo**: scelta deliberata. Win rate è
  troppo discreto e profit factor è poco sensibile a piccoli campioni. Slope è
  continuo e cattura l'andamento medio. Limite: rumoroso con 50 trade.
- **Rollback automatico vs. tenere alla cieca**: il tuner non aspetta convergenza
  statistica robusta; se uno step non migliora rapidamente, lo scarta. Bias a
  favore della prudenza, può scartare step che migliorerebbero su 200 trade ma
  non su 50.
- **Sequenza fissa**: l'ordine di default ha senso teorico (regime > MTF > volatility
  > participation), ma su mercati specifici un altro ordine potrebbe essere
  migliore. La sequenza è esposta in `auto_tuner_config.sequence` e può essere
  riordinata via PATCH se necessario.
- **No re-test di step revertiti**: una volta che uno step ha fatto rollback,
  non viene ritestato automaticamente. Per riprovarlo: `POST /api/auto-tuner/reset`
  (azzera la history e riparte dal primo step).
