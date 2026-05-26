# Guida utilizzo

## Modalita'

- `SIGNAL_ONLY`: genera e salva segnali, non apre posizioni.
- `PAPER_TRADING`: permette posizioni simulate.
- `AUTO_TRADE`: abilita l'esecuzione live su Hyperliquid se passano tutti i guardrail.

Il live trading richiede `ENABLE_LIVE_TRADING=true`, account/key Hyperliquid in ambiente, kill switch spento e validazioni rischio. Su mainnet richiede anche `ENABLE_MAINNET_LIVE_TRADING=true` o il corrispondente toggle runtime `enable_mainnet_live_trading`.

Non salvare private key nel database. Impostare `HYPERLIQUID_PRIVATE_KEY` solo come variabile ambiente del backend/worker.

## Gestione dinamica Stop Loss / Take Profit

La configurazione runtime e' in `/api/settings`, chiave `position_management`.
Default iniziale:

```json
{
  "timeStop": {
    "enabled": true,
    "paperEnabled": true,
    "liveMode": "alert",
    "maxAgeHoursByTimeframe": {
      "5m": 8,
      "15m": 16,
      "30m": 24,
      "1h": 48,
      "4h": 96,
      "1d": 168
    }
  },
  "trailingStop": {
    "enabled": true,
    "activationR": 1.0,
    "mode": "hybrid",
    "atrPeriod": 14,
    "atrMultiplierInitial": 3.0,
    "atrMultiplierAfterTp1": 2.5,
    "atrMultiplierAfterTp2": 2.0,
    "useChandelierExit": true,
    "useSwingTrailing": true,
    "useEmaTrailing": true,
    "emaPeriod": 21,
    "swingLookback": 20,
    "swingBufferAtr": 0.5,
    "emaBufferAtr": 0.5,
    "minStopUpdateDistancePct": 0.15,
    "updateOnlyOnCandleClose": true,
    "moveToBreakevenAtR": 1.0,
    "lockProfitAtR": 2.0,
    "lockProfitStopR": 1.0
  },
  "takeProfit": {
    "enabled": true,
    "tp1R": 1.0,
    "tp1ClosePct": 30,
    "tp2R": 2.0,
    "tp2ClosePct": 30,
    "runnerPct": 40,
    "runnerMode": "trailing_stop"
  }
}
```

In paper trading il worker simula TP parziali e aggiornamenti SL. Il `timeStop` chiude automaticamente solo posizioni paper oltre la soglia del timeframe usando il close dell'ultima candela disponibile e registra `paper_position_time_stop_closed`. In live trading `liveMode: "alert"` non chiude ordini: registra `live_position_time_stop_alert` e aggiorna `metadata.last_time_stop_alert_at` per evitare spam.

In live trading gli aggiornamenti SL sono inviati solo quando la modalita' effettiva e' `AUTO_TRADE`, `enable_live_trading` e' true, le credenziali sono configurate e il kill switch e' spento. Ogni modifica dello stop e' registrata in `position_stop_updates` e nei `decision_logs`.

Quando una posizione live viene riconciliata come chiusa su Hyperliquid, la posizione companion paper resta aperta di default per preservare le statistiche paper/auto-tuner. La reconciliation registra `live_companion_paper_left_open`. Per usare la paper come mirror operativo della live, impostare `live_execution.close_companion_paper_on_live_reconcile=true`.

## Avvio locale passo per passo

### 1. Prerequisiti

- Docker Desktop avviato.
- Python 3.11 o superiore.
- Node.js 20 o superiore.
- Accesso internet per scaricare dipendenze e leggere le API Hyperliquid.

Verifica rapida:

```bash
docker info
python3 --version
node --version
npm --version
```

Se `docker info` fallisce, apri Docker Desktop e riprova dopo che il daemon e' partito.

### 2. Configura variabili ambiente

Dalla root del progetto:

```bash
cp .env.example .env
```

Per la prima esecuzione lascia questi valori sicuri:

```env
OPERATION_MODE=SIGNAL_ONLY
HYPERLIQUID_NETWORK=testnet
ENABLE_LIVE_TRADING=false
ENABLE_MAINNET_LIVE_TRADING=false
```

Per preparare testnet live, configurare anche `HYPERLIQUID_ACCOUNT_ADDRESS` e `HYPERLIQUID_PRIVATE_KEY`. Usare una API wallet autorizzata dal wallet principale Hyperliquid.

### 3. Avvia PostgreSQL

Terminale 1, dalla root:

```bash
docker compose up -d postgres
docker compose ps
```

Il compose espone PostgreSQL sulla porta host `5433`, per evitare conflitti con eventuali PostgreSQL locali gia' attivi sulla porta `5432`.

Attendi che `postgres` risulti `healthy`. Se vuoi vedere i log:

```bash
docker compose logs -f postgres
```

### 4. Installa backend Python

Terminale 2, dalla root:

```bash
python3 -m venv venv
. venv/bin/activate
python -m pip install -e "backend[dev]"
```

Verifica test e typing:

```bash
python -m pytest backend/tests
python -m ruff check backend/app backend/tests
python -m mypy backend/app
```

### 5. Avvia backend API

Sempre nel terminale 2, con virtualenv attivo:

```bash
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

Verifica in un altro terminale:

```bash
curl http://localhost:8000/api/health
```

Risposta attesa: `status` uguale a `ok`, `mode` uguale a `SIGNAL_ONLY`, `live_trading_allowed` uguale a `false`.

Se `--reload` da' errore di permessi o file watcher, avvia senza reload:

```bash
python -m uvicorn app.main:app --port 8000
```

Se l'errore contiene `connection to server at "localhost", port 5433 failed`, PostgreSQL non e' raggiungibile. Controlla:

```bash
docker compose ps
docker compose logs postgres
```

In alternativa verifica manualmente la connessione configurata in `.env`:

```bash
python - <<'PY'
from app.core.config import get_settings
print(get_settings().database_url)
PY
```

Il backend crea le tabelle all'avvio, quindi non parte se `DATABASE_URL` punta a un database spento o non accessibile.

Se l'errore contiene `FATAL: database "trado" does not exist`, potresti stare puntando a un altro PostgreSQL locale oppure manca il database. Prima verifica che `.env` punti alla porta `5433`:

```env
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/trado
```

Poi crea il database nel container senza cancellare il volume:

```bash
docker compose exec postgres createdb -U postgres trado
```

Poi riavvia il backend:

```bash
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

Se vuoi invece ricreare PostgreSQL da zero cancellando tutti i dati locali:

```bash
docker compose down -v
docker compose up -d postgres
```

Usa questa seconda opzione solo se non ti interessa conservare i dati del database locale.

### 6. Sincronizza mercati Hyperliquid

Terminale 3, dalla root:

```bash
curl -X POST http://localhost:8000/api/markets/sync
curl http://localhost:8000/api/markets
```

La prima chiamata importa mercati spot e perpetual. La seconda deve restituire una lista di mercati.

### 7. Avvia worker

Terminale 4, dalla root:

```bash
. venv/bin/activate
cd backend
python -m app.worker.main
```

Il worker resta attivo e ogni ciclo:

- aggiorna candles;
- calcola strategie;
- genera segnali sopra soglia;
- invia alert Telegram se configurato;
- apre paper trade se `OPERATION_MODE=PAPER_TRADING`;
- in `AUTO_TRADE`, riconcilia posizioni live e invia ordini live solo se i guardrail passano.

## Checklist AUTO_TRADE

Prima di abilitare live:

1. Passare da paper a testnet:

```env
HYPERLIQUID_NETWORK=testnet
OPERATION_MODE=AUTO_TRADE
ENABLE_LIVE_TRADING=true
ENABLE_MAINNET_LIVE_TRADING=false
```

2. Configurare `HYPERLIQUID_ACCOUNT_ADDRESS` e `HYPERLIQUID_PRIVATE_KEY` nel backend e nel worker.
3. Tenere `KILL_SWITCH_ENABLED=false` solo quando si vuole davvero consentire invio ordini.
4. Verificare in Settings:
   - `Max posizioni live`
   - `Max daily loss`
   - `Max esposizione live`
   - `Risk per trade`
   - `Order cap`
   - `Max slippage`
5. Controllare `GET /api/health`: `live_trading_allowed` deve essere `true` e `live_readiness.checks` vuoto.
6. Provare `POST /api/signals/{id}/execute-live` su un singolo segnale testnet prima di lasciare il worker in `AUTO_TRADE`.

Mainnet:

```env
HYPERLIQUID_NETWORK=mainnet
ENABLE_MAINNET_LIVE_TRADING=true
```

Mainnet resta bloccato se `enable_mainnet_live_trading` non e' esplicitamente attivo anche nelle impostazioni runtime.

### 8. Installa e avvia frontend

Terminale 5, dalla root:

```bash
cd frontend
npm install
npm run dev
```

Apri:

```text
http://localhost:3000
```

La dashboard legge il backend da `NEXT_PUBLIC_API_BASE_URL`, che in `.env.example` punta a `http://localhost:8000`.

### 9. Comandi utili

Dalla root:

```bash
npm run frontend:build
npm run backend:test
npm run backend:lint
npm run backend:typecheck
```

API docs FastAPI:

```text
http://localhost:8000/docs
```

### 10. Spegnimento locale

Ferma backend, worker e frontend con `Ctrl+C` nei rispettivi terminali.

Ferma PostgreSQL:

```bash
docker compose down
```

Per eliminare anche i dati locali PostgreSQL:

```bash
docker compose down -v
```

Usa `down -v` solo se vuoi cancellare completamente il database locale.

## Flusso minimo dopo l'avvio

1. Apri `http://localhost:8000/api/health`.
2. Esegui `POST /api/markets/sync`.
3. Controlla `GET /api/markets`.
4. Avvia worker.
5. Apri `http://localhost:3000`.
6. Guarda `GET /api/signals` dopo alcuni cicli worker.

## Endpoint principali

- `GET /api/health`
- `GET /api/markets`
- `POST /api/markets/sync`
- `GET /api/candles?symbol=BTC&timeframe=5m`
- `GET /api/signals`
- `POST /api/signals/{id}/ignore`
- `POST /api/signals/{id}/execute-paper`
- `POST /api/signals/{id}/execute-live`
- `DELETE /api/signals/{id}`
- `GET /api/positions`
- `POST /api/positions/{id}/close`
- `DELETE /api/positions/{id}`
- `GET /api/settings`
- `PATCH /api/settings`
- `GET /api/strategies`
- `PATCH /api/strategies/{id}`
- `POST /api/kill-switch/enable`
- `POST /api/kill-switch/disable`

## Render/Vercel

- Frontend Vercel: impostare `NEXT_PUBLIC_API_BASE_URL` sull'URL Render del backend.
- Backend Render: usare start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- Worker Render: usare start command `python -m app.worker.main`.
- PostgreSQL: impostare `DATABASE_URL` compatibile SQLAlchemy.

## Vedere stato

launchctl print gui/$(id -u)/com.trado.backend | grep -E 'state|pid|exit'
launchctl print gui/$(id -u)/com.trado.worker  | grep -E 'state|pid|exit'

## Log live

tail -f ~/Library/Logs/trado-backend.err.log
tail -f ~/Library/Logs/trado-worker.err.log

## Stop/start temporaneo (es. per dev con --reload)

launchctl bootout gui/$(id -u)/com.trado.backend
launchctl bootout gui/$(id -u)/com.trado.worker

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.backend.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.worker.plist

## Disinstallare tutto

bash scripts/launchd/uninstall.sh

## 3. Controlla che il tuner abbia ripreso bene

curl -s <http://localhost:8000/api/auto-tuner/status> | jq

### TODO

§3.1 walk-forward calibration (refactor architetturale) -> **FATTO**
§2.1 vectorization indicators con numpy/pandas-ta -> **NON APPLICATO**
§4.8 nuove strategie (CVD, liquidation cascade, volume profile) -> **FATTO**
§4.9 regime detection alternativo (Hurst, GARCH) -> **FATTO**
§3.10 A/B testing auto-tuner -> **FATTO**

## Cooldown deep defensive auto-calibration

La calibrazione usa `deep_defensive_cooldown_runs` (default `6`) e
`deep_defensive_cooldown_min_new_trades` (default `3`) per evitare oscillazioni
immediate dopo una recovery da deep defensive/starvation. Durante il cooldown i
parametri risk restano al baseline anche se lo slope storico e' ancora negativo.

Lo stato e' salvato in `auto_calibration_state`:

- `deep_defensive_cooldown_remaining`
- `deep_defensive_cooldown_started_at`
- `deep_defensive_cooldown_trade_count`
- `deep_defensive_last_exit_reason`

Quando il cooldown blocca un nuovo irrigidimento, il run registra una change
`target: "calibration_state"`, `key: "deep_defensive_cooldown"` dentro
`auto_calibration_applied`.

## A/B testing auto-tuner (§3.10)

L'auto-tuner storico (`mode: "sequential"`, default) confronta i trade pre e post-attivazione del patch sulla stessa popolazione di mercato. Il problema noto: se le condizioni di regime cambiano fra finestra pre e post, il confronto è confuso da bias temporali.

In `mode: "ab_test"` l'auto-tuner non modifica mai globalmente `risk` finché lo step non viene promosso. Per ogni nuovo segnale `SignalEngine.evaluate()` chiama `active_ab_overlay()`, assegna deterministicamente l'arm via hash stabile di `step_key + market_id + timeframe + source_candle_open_time + ab_test_assignment_salt`, poi mergia l'overlay dell'arm nel `risk_value` locale prima di derivare i filtri. La posizione che ne nasce viene taggata in `metadata_json["_auto_tuner"] = {"step_key", "arm"}` (sia paper sia live).

Quando entrambi gli arm hanno raccolto almeno `ab_test_min_trades_per_arm` posizioni chiuse post-attivazione, l'auto-tuner esegue un Welch's t-test sulla media del PnL realizzato e:

- promuove (`status: "promoted"`, applica `step.patch` a `risk`) se `mean_test > mean_control` *e* `p_one_tailed < ab_test_significance_level`
- rifiuta (`status: "rejected"`, nessuna modifica al setting) altrimenti

In entrambi i casi lo step viene chiuso e la cronologia (`step_history`) include `n_control`, `n_test`, `mean_control`, `mean_test`, `t_statistic`, `degrees_of_freedom`, `p_one_tailed`.

### Configurazione

Si imposta via `PATCH /api/settings`:

```bash
curl -X PATCH http://localhost:8000/api/settings \
  -H "Content-Type: application/json" \
  -d '{
    "auto_tuner_config": {
      "value": {
        "enabled": true,
        "mode": "ab_test",
        "ab_test_min_trades_per_arm": 30,
        "ab_test_significance_level": 0.05,
        "ab_test_allocation": 0.5,
        "ab_test_assignment_salt": "trado-v1",
        "sequence": [
          {"key": "regime_filter", "label": "Regime filter (ADX)",
           "patch": {"regime_filter_enabled": true},
           "revert": {"regime_filter_enabled": false}}
        ]
      }
    }
  }'
```

Parametri:

- `ab_test_min_trades_per_arm` (default 30): minimo di trade chiusi per arm prima di valutare. Sotto questa soglia `run()` ritorna `status: "in_progress"`.
- `ab_test_significance_level` (default 0.05): α one-tailed per promuovere il patch.
- `ab_test_allocation` (default 0.5): P(test). 0.5 = 50/50; valori più bassi (es. 0.2) consentono di limitare l'esposizione al rischio della variante non testata.
- `ab_test_assignment_salt` (default `"trado-v1"`): sale per l'assegnazione deterministica degli arm. Cambiarlo rimescola le assegnazioni future.

Il p-value è calcolato con approssimazione normale del CDF di Student (sufficiente per n ≥ ~20 per arm). Per popolazioni più piccole o distribuzioni molto fat-tailed l'errore di approssimazione è conservativo (sotto-stima la significatività).

### Stato e controllo

`GET /api/auto-tuner/status` espone, in modalità AB:

```json
{
  "enabled": true,
  "mode": "ab_test",
  "current_step_key": "regime_filter",
  "current_step_mode": "ab_test",
  "ab_test_state": {
    "control_overlay": {"regime_filter_enabled": false},
    "test_overlay":    {"regime_filter_enabled": true},
    "allocation": 0.5,
    "assignment_method": "deterministic_hash"
  },
  "ab_progress": {
    "trades_control": 18,
    "trades_test": 21,
    "trades_required_per_arm": 30,
    "sample_balance": 0.86,
    "mean_control": -1.2,
    "mean_test": 0.4,
    "mean_delta": 1.6,
    "alpha": 0.05,
    "assignment_method": "deterministic_hash",
    "control_overlay": {"regime_filter_enabled": false},
    "test_overlay": {"regime_filter_enabled": true},
    "active_overlay": {"regime_filter_enabled": true}
  }
}
```

`POST /api/auto-tuner/rollback` chiude lo step corrente senza alcuna modifica a `risk` (in AB mode il patch non era stato applicato). `POST /api/auto-tuner/reset` azzera anche la history.

### Source test guidati

Per evitare patch manuali su `auto_tuner_config`, la UI usa endpoint dedicati:

- `GET /api/auto-tuner/source-tests` mostra i preset disponibili, lo step attivo e se l'avvio è bloccato.
- `POST /api/auto-tuner/source-tests/whale_flow/start` avvia un A/B test che rimuove solo `whale_flow` da `disabled_signal_sources` nell'arm test.
- `POST /api/auto-tuner/source-tests/fear_greed/start` fa lo stesso per `fear_greed`.

Se esiste già uno step attivo, l'endpoint di start ritorna `409` con `recommended_actions: ["wait", "rollback", "reset"]`. I preset preservano le altre source già disabilitate e non modificano globalmente `risk` finché l'A/B non viene promosso.

### Limitazioni note

- Gli overlay attualmente in `DEFAULT_SEQUENCE` (regime, MTF, volatility, participation ratio) toccano solo filtri di signal-generation. Patch che modifichino parametri di sizing/execution (es. `max_risk_per_trade_pct`) non sono pienamente A/B-isolati: `OrderExecutionEngine` legge `risk` direttamente al momento dell'apertura della posizione e userebbe il valore "globale" (= control), creando un sottile leak fra arm. Per quei casi servirebbe propagare anche `_auto_tuner` overlay all'esecuzione.
- L'assegnazione arm è deterministica quando il caller passa market/timeframe/candela. Il fallback random resta solo per chiamate isolate senza contesto di segnale.
- L'auto-tuner valuta solo posizioni paper (`SignalPerformanceAnalyzer.closed_paper_positions`). Il tag viene comunque scritto anche su posizioni live per coerenza.

`GET /api/analytics/equity-slope` espone la diagnostica dedicata della slope: finestre 50/100/500 trade, slope normalizzata, soglia deep defensive, stato interpretativo, cooldown, streak di uscita e impatto dei time-stop paper nelle ultime 24h.

## Regime detection alternativo

Oltre al classico ADX, `risk_controls.regime_method` permette di selezionare metodi alternativi per la classificazione di mercato:

- `adx` (default) — soglie su ADX con isteresi `regime_confirm_candles`.
- `hurst` — esponente di Hurst via R/S analysis sui log-returns. `H ≥ regime_hurst_trending` (default 0.55) → trending; `H ≤ regime_hurst_ranging` (default 0.45) → ranging; intervallo intermedio → mixed (random walk). Richiede almeno `regime_hurst_max_lag * 4` candele.
- `garch` — proxy GARCH-lite: autocorrelazione lag-`regime_garch_lag` (default 1) dei rendimenti al quadrato su una finestra di `regime_garch_window` candele (default 50). Valori ≥ `regime_garch_cluster_threshold` (default 0.2) indicano vol clustering → trending; valori ≤ threshold/2 → ranging.
- `hybrid` — esegue tutti i sub-detectors disponibili e prende il consensus a maggioranza; richiede ≥ 2 metodi concordi per dichiarare un regime non-mixed.

Parametri in `AppSetting("risk_controls")`:

```json
{
  "regime_filter_enabled": true,
  "regime_method": "adx",
  "regime_adx_trending": 25.0,
  "regime_adx_ranging": 18.0,
  "regime_confirm_candles": 2,
  "regime_off_weight_multiplier": 0.5,
  "regime_hurst_trending": 0.55,
  "regime_hurst_ranging": 0.45,
  "regime_hurst_min_lag": 2,
  "regime_hurst_max_lag": 20,
  "regime_garch_window": 50,
  "regime_garch_lag": 1,
  "regime_garch_cluster_threshold": 0.2
}
```

Le metriche calcolate (es. `hurst`, `garch`, `adx`) e i sub-regimi finiscono nel breakdown `_regime` del segnale, visibile nella UI di analisi sotto "Filtri di sistema".
