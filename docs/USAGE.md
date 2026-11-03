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

Quando una posizione live viene riconciliata come chiusa su Hyperliquid, la posizione companion paper resta aperta di default per preservare le statistiche paper e la ricerca forward. La reconciliation registra `live_companion_paper_left_open`. Per usare la paper come mirror operativo della live, impostare `live_execution.close_companion_paper_on_live_reconcile=true`.

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

### Segnali esterni The Bull

L'integrazione con il canale pubblico Telegram `@thebull_crypto` si configura in
**Impostazioni → Configurazione avanzata → Servizi e integrazioni**. Nasce
disattivata e in modalita' paper.

Il monitor applica regole fail-closed:

- al primo avvio salva il messaggio piu' recente come cursor e non esegue lo storico;
- accetta solo call LONG esplicite come `Buying $XPL`, `Buy $XPL`,
  `$XPL Buy Setup` o `$XPL is a BUY`;
- recupera una finestra paginata degli ultimi 25 messaggi pubblici, così il
  cursore non dipende dalla sola prima pagina Telegram;
- rileva buy zone, buy-the-dip, supporto, breakout/retest, double bottom,
  bullish bias e watchlist. Con **Esegui call estese** seguono il normale
  flusso. Le frasi `Buying more $TICKER` e `$TICKER dip is for buying`
  possono aggiungere solo a una posizione live The Bull già aperta;
- ogni posizione perp può ricevere al massimo due aggiunte, ciascuna non oltre
  il 30% della quantità iniziale. Il sistema non aumenta l'ordine per superare
  il minimo dell'exchange: in quel caso lo scarta. L'aggiunta richiede stop
  live verificato e aggiornabile, margine, profondità e tutti i guardrail live.
  Le aggiunte spot e quelle a posizioni di altre fonti restano escluse;
- l'opzione **Analisi delle zone nel grafico** usa localmente il framework
  macOS Vision: estrae esclusivamente range di prezzo numerici e leggibili dalle
  immagini pubbliche Telegram, senza API né costi ricorrenti. Con **Esegui zone
  lette dal grafico** una zona è eleggibile solo quando il midpoint corrente è
  dentro il range; poi passa allo stesso piano rischio, cap e guardrail delle
  altre call. Analizza al massimo due grafici per ciclo;
- scarta messaggi oltre l'eta' massima configurata e token senza perpetual
  Hyperliquid attivo;
- costruisce SL/TP dalla struttura e dall'ATR del timeframe scelto (default 4h),
  poi usa TP parziali, breakeven e trailing standard di Trado;
- applica solo una frazione del rischio globale (default 50%) e non puo' mai
  aumentarlo, con massimo 3 posizioni della fonte aperte contemporaneamente;
- puo' trattare un reply finale (`+N% from entry`, stop o chiusura esplicita)
  come uscita aggiuntiva dalla posizione correlata.

Se si seleziona `Paper + live`, restano comunque obbligatori `AUTO_TRADE`,
l'opt-in live/mainnet, credenziali valide, kill switch disarmato e tutti i
guardrail dell'`OrderValidator`. Un segnale esterno non puo' aggirarli.

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

## Ricerca forward

Il runtime attribuisce ogni nuovo segnale a `research_experiment.active_version`
e materializza gli outcome paper senza modificare automaticamente i parametri.
La promozione di una configurazione è manuale.

Endpoint utili:

- `GET /api/analytics/experiments`
- `GET /api/analytics/expected-r-calibration`
- `GET /api/analytics/pre-tp1-validation`
- `GET /api/analytics/live-eligibility`
- `GET /api/analytics/equity-slope`

Il runtime non contiene tuning automatico: i parametri restano congelati finché
non vengono modificati esplicitamente. Per dettagli e procedura di promozione
vedere `docs/AUTO_TUNING.md`.

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


grep -i "_liquidation_pressure\|liquidation_pressure_failed" ~/Library/Logs/trado-worker.out.log
