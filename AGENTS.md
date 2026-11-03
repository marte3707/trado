# AGENTS.md

Guida operativa per agenti che lavorano su Trado.
Aggiornato al 28/05/2026.

## Regola principale

Trado e' un sistema personale di trading crypto su Hyperliquid. Ogni modifica
deve essere conservativa: preservare i guardrail di live trading, non esporre
segreti, non cambiare sizing/risk management senza test, e non assumere che il
paper trading e il live trading abbiano gli stessi vincoli.

La worktree puo' contenere modifiche dell'utente. Non fare reset, checkout o
revert di file non modificati da te. Se trovi cambiamenti non tuoi, lavoraci
intorno.

## Stack

- Backend: Python 3.11, FastAPI, SQLAlchemy 2.0, PostgreSQL, psycopg3.
- Worker: processo async standalone con loop continuo.
- Frontend: Next.js 14 App Router, React 18, TypeScript, Tailwind CSS.
- Exchange/data: Hyperliquid per mercati, candele, account e ordini.
- Notifiche: Telegram e Web Push.
- Infra locale: Docker Compose per PostgreSQL, launchd per backend/worker su
  macchina dell'utente.

## Comandi comuni

Eseguire dalla root del progetto, usando il virtualenv `./venv/`.

```bash
npm run backend:test
npm run backend:lint
npm run backend:typecheck
npm run frontend:dev
npm run frontend:build
```

Singolo test backend:

```bash
./venv/bin/python -m pytest backend/tests/test_scoring.py
```

Avvio sviluppo locale in terminali separati:

```bash
docker compose up -d postgres
cd backend && ../venv/bin/python -m uvicorn app.main:app --reload --port 8000
cd backend && ../venv/bin/python -m app.worker.main
cd frontend && npm run dev
```

Il PostgreSQL del compose espone la porta host `5433`. Il backend usa default
FastAPI su `8000`, il frontend Next su `3000`.

## Riavvio servizi launchd

Dopo modifiche a `backend/`, se i servizi launchd sono in uso, riavviare
backend e worker in questo ordine:

```bash
launchctl bootout gui/$(id -u)/com.trado.backend
launchctl bootout gui/$(id -u)/com.trado.marketdata
launchctl bootout gui/$(id -u)/com.trado.worker
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.backend.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.marketdata.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.worker.plist
```

Non usare `--reload` in produzione locale gestita da launchd.

## Architettura

Il monorepo ha tre processi principali che condividono PostgreSQL:

- `backend/app/`: API REST FastAPI, modelli SQLAlchemy, servizi exchange,
  motori trading e notifiche.
- `backend/app/worker/main.py`: loop async che sincronizza mercati/candele,
  genera segnali, apre posizioni consentite, riconcilia live e gestisce stop/TP.
- `frontend/`: dashboard operativa Next.js che legge solo le API backend.

Il backend crea schema e applica migrazioni custom all'avvio:

- modelli in `backend/app/db/models.py`;
- seed default in `backend/app/db/init_db.py`;
- migrazioni custom in `backend/app/db/migrations.py`;
- migrazioni Alembic presenti in `backend/alembic/versions/`.

Per sviluppo locale normalmente non serve eseguire Alembic manualmente.

## Flusso worker

Per ogni ciclo:

1. Determina i timeframe attivi con `effective_enabled_timeframes()`.
2. Scrive heartbeat in `AppSetting("worker_heartbeat")`.
3. Sincronizza mercati e candele chiuse da Hyperliquid.
4. Per ogni mercato x timeframe attivo esegue `SignalEngine.generate()`.
5. Le strategie registrate producono `StrategyResult`.
6. `SignalScoringEngine` combina i risultati con pesi e participation ratio.
7. `RiskManager` calcola SL/TP basati su ATR e risk/reward.
8. Il segnale viene salvato e puo' generare alert.
9. `OrderExecutionEngine` apre paper/live in base a modalita' e guardrail.
10. A fine ciclo gestisce posizioni aperte con trailing stop, TP parziali,
    breakeven, time stop, PnL refresh e reconciliation live.
11. Materializza gli outcome immutabili dell'esperimento forward.

La cadenza nominale del worker e' 60 secondi. L'API considera unhealthy un
heartbeat piu' vecchio di circa 180 secondi.

## Modalita operative

Enum in `backend/app/core/enums.py`:

- `SIGNAL_ONLY`: genera e salva segnali, non apre posizioni.
- `PAPER_TRADING`: apre posizioni simulate.
- `AUTO_TRADE`: apre paper e puo' inviare ordini live Hyperliquid se passano i
  guardrail.

La modalita effettiva viene letta prima da `AppSetting("operation_mode")`, poi
da env var come fallback. Usare sempre helper in
`backend/app/core/runtime_settings.py`, non leggere direttamente env/DB quando
esiste un `effective_*`.

## Guardrail live trading

Il live trading deve rimanere bloccato salvo opt-in esplicito:

1. `operation_mode.mode == "AUTO_TRADE"`.
2. `enable_live_trading` true in runtime settings/env.
3. `kill_switch.enabled == false`.
4. `HYPERLIQUID_ACCOUNT_ADDRESS` presente.
5. `HYPERLIQUID_PRIVATE_KEY` presente.
6. Su mainnet: `enable_mainnet_live_trading == true`.
7. `OrderValidator` approva equity minima, notional min/max, slippage, TTL e
   max posizioni live.

Non salvare private key nel DB. La chiave deve stare solo nell'ambiente del
backend/worker.

Regola importante: `max_open_positions` e i guardrail di conteggio posizioni si
applicano solo alle posizioni `mode == "live"`. Non bloccare paper trading con
un count generico delle posizioni aperte.

## AppSetting principali

Runtime configuration vive in `app_settings` come JSONB:

- `operation_mode`: modalita e live toggle.
- `kill_switch`: kill switch live.
- `paper`: equity simulata.
- `live_execution`: account floor, notional cap, slippage, mainnet opt-in,
  `close_companion_paper_on_live_reconcile`.
- `risk`: `min_signal_score`, `min_risk_reward`, risk pct, max daily loss,
  max positions, exposure, leverage, filtri segnale e Unified Account Ratio.
- `position_management`: trailing stop, take profit, time stop.
- `timeframes`: timeframe abilitati.
- `alerts`: dashboard/Telegram/browser push e soglia alert.
- `research_experiment`: versione, assignment e policy della ricerca forward.
- `btc_macro_protection`: protezione macro BTC e auto-reverse.
- `worker_heartbeat`: heartbeat e fase corrente worker.

## Tabelle chiave

- `markets`: mercati spot/perp, con `metadata_json["hl_coin"]` per mapping HL.
- `candles`: OHLCV + open interest per market/timeframe.
- `indicators`: snapshot indicatori calcolati.
- `signals`: segnali con score, SL/TP, breakdown strategie e status.
- `positions`: posizioni paper/live, PnL, funding e metadata.
- `position_stop_updates`: storico aggiornamenti stop.
- `orders`: ordini paper/live, cloid, fee, raw response.
- `strategy_configs`: strategia x timeframe, enabled, params e weight.
- `app_settings`: configurazione runtime.
- `alerts`: alert dashboard/Telegram/push.
- `account_snapshots`: equity/margin/PnL live.
- `push_subscriptions`: Web Push browser.
- `decision_logs`: log decisionale worker/sistema.

## Strategie

Registry in `backend/app/strategies/registry.py`. Strategie attuali:

- `ema_crossover`
- `rsi`
- `macd`
- `volume_spike`
- `fibonacci`
- `support_resistance`
- `supertrend`
- `bollinger_bands`
- `vwap`
- `candlestick_patterns`
- `wyckoff_spring_upthrust`
- `oi_trend`
- `cvd_divergence`
- `liquidation_cascade`
- `volume_profile`

Ogni strategia implementa il protocollo `Strategy` con `name` e
`evaluate(context, params) -> StrategyResult`.

Per aggiungere una strategia:

1. Creare file in `backend/app/strategies/`.
2. Implementare `Strategy`.
3. Registrarla in `registry.py`.
4. Aggiungere default params e peso in `backend/app/db/init_db.py`.
5. Aggiungere test mirati.

## Scoring e risk

Il signal engine combina risultati strategia, scoring, regime/MTF/volatility
filter e risk plan. Le soglie operative vivono in `AppSetting("risk")`.

Campi importanti:

- `min_signal_score`
- `min_risk_reward`
- `min_average_quote_volume_usd`
- `max_risk_per_trade_pct`
- `max_daily_loss_pct`
- `max_open_positions`
- `max_total_exposure_pct`
- `max_leverage`
- `participation_ratio`
- `regime_filter_enabled`
- `mtf_confluence_enabled`
- `volatility_filter_enabled`
- Unified Account Ratio settings da `DEFAULT_UAR_RISK_SETTINGS`

Non introdurre magic number nuovi per soglie trading se possono essere runtime
settings.

## Position management

File principale: `backend/app/engine/position_management.py`.

Default in `DEFAULT_POSITION_MANAGEMENT_SETTINGS`:

- trailing stop ibrido con Chandelier Exit, Swing trailing ed EMA trailing;
- attivazione trailing a `R=1.0`;
- TP1 a `R=1.0` chiude 30%;
- TP2 a `R=2.0` chiude 30%;
- runner 40% gestito da trailing stop;
- breakeven a `R=1.0`;
- lock profit a `R=2.0`;
- time stop per timeframe.

Paper trading simula TP parziali, trailing, breakeven e time stop. Live trading
aggiorna stop solo se la modalita effettiva e' `AUTO_TRADE`, live e' abilitato,
le credenziali sono presenti e il kill switch e' spento.

Quando una live viene riconciliata chiusa su Hyperliquid, la companion paper
rimane aperta di default per preservare statistiche paper e ricerca forward. Per
chiuderla automaticamente usare:
`live_execution.close_companion_paper_on_live_reconcile=true`.

## Diagnostica osservativa

File principale: `backend/app/engine/diagnostics.py`.

Equity slope, cicli consecutivi senza segnali, distribuzione degli exit e
performance per strategia/timeframe sono read-only. Non aggiornano pesi,
soglie, sizing o stato operativo. L'AutoCalibration mutante e i suoi endpoint
sono stati rimossi; eventuali chiavi e log storici nel database sono archivio
inerte.

## Ricerca forward

File principali:

- `backend/app/engine/experiment.py`
- `backend/app/engine/experiment_outcomes.py`

Ogni nuovo segnale riceve una versione e un hash della configurazione. Gli
outcome dei trade paper vengono materializzati senza modificare sizing,
eligibility o impostazioni. La promozione di una configurazione e' manuale.

Endpoint:

- `GET /api/analytics/experiments`
- `GET /api/analytics/expected-r-calibration`
- `GET /api/analytics/pre-tp1-validation`
- `GET /api/analytics/live-eligibility`

## BTC macro protection

File: `backend/app/engine/btc_macro_protection.py`.

Protegge posizioni da reversal macro BTC. Configurazione in
`AppSetting("btc_macro_protection")`. Include soglia reversal, timeframe di
osservazione, conferma EMA, breakeven buffer, loss exit e reverse mode con
cooldown.

Modifiche a questa area richiedono test per reversal through NEUTRAL, stato
persistito, rollback/commit e idempotenza degli interventi sulle posizioni.

## Unified Account Ratio

File: `backend/app/services/unified_account_ratio.py`.

Calcola una vista unificata del rischio account usando live/perp/spot/USDC e
settings in `risk`. Il worker applica guard periodici; l'API espone dati account
e controlli. Trattare Decimal/quantizzazione con cura, evitando float dove
serve precisione contabile.

## Hyperliquid

Client: `backend/app/services/hyperliquid.py`.

Regole asset:

- Perpetual: `asset_id = index` nell'array `meta.universe`.
- Spot: `asset_id = 10000 + spotMeta.universe[index].index`.
- Candele spot usano `metadata_json["hl_coin"]`, per esempio `PURR/USDC` o
  `@index`; fallback a `market.symbol`.

Live order flow usa SDK ufficiale Hyperliquid incapsulato
dall'`OrderExecutionEngine`.

## API REST

Router prefix: `/api` in `backend/app/api/routes.py`.

Endpoint principali:

- Health/sistema: `GET /health`, `POST /system/services/{service}/{action}`,
  `GET /system/error-logs`, `GET /logs`.
- Market data: `GET /markets`, `POST /markets/sync`, `GET /candles`,
  `GET /indicators`, `GET /market-intelligence`.
- Segnali: `GET /signals`, `GET /signals/{id}`, `POST /signals/{id}/ignore`,
  `POST /signals/{id}/reopen`, `DELETE /signals/{id}`,
  `POST /signals/{id}/execute-paper`, `POST /signals/{id}/execute-live`.
- Posizioni: `GET /positions`, `POST /positions/{id}/close`,
  `DELETE /positions/{id}`.
- Ordini: `GET /orders`.
- Analytics: `/analytics/signals`, `/analytics/diagnostics`,
  `/analytics/equity-slope`, `/analytics/outcome-profile`,
  `/analytics/experiments`, `/analytics/strategy-edge`,
  `/analytics/worker-cycles`, `/analytics/signal-rejections`.
- Settings/strategie: `GET /settings`, `PATCH /settings`, `GET /strategies`,
  `PATCH /strategies/{id}`.
- Kill switch: `POST /kill-switch/enable`, `POST /kill-switch/disable`.
- Account live: `GET /account/snapshot`, `POST /account/orders/{oid}/cancel`,
  `POST /account/positions/{coin}/close`,
  `POST /account/positions/patch-missing-exit-data`.
- Push: `POST /push/subscribe`, `DELETE /push/subscribe`, `POST /push/test`.

API auth e redazione log sono importanti: non ritornare eccezioni raw con
segreti. Usare pattern `_log_and_redact()` dove applicabile.

## Frontend

Frontend in `frontend/`, Next.js App Router.

- Entry: `frontend/app/page.tsx`, `frontend/app/layout.tsx`,
  `frontend/app/providers.tsx`.
- API client: `frontend/lib/api.ts`.
- Query hooks: `frontend/lib/hooks/queries.ts`, `mutations.ts`,
  `queryKeys.ts`.
- Shell: `frontend/components/AppShell.tsx` e `frontend/components/shell/*`.
- Dashboard principale: `frontend/components/Dashboard.tsx`.
- UI primitives: `frontend/components/ui/*`.

Schermate:

- `DashboardScreen`: panoramica operativa.
- `SignalsScreen`: feed segnali.
- `CoinScreen`: chart e indicatori mercato.
- `AccountMonitorScreen`: account, posizioni e ordini live Hyperliquid.
- `StrategyAnalysisScreen`: strategy lab.
- `CalibrationScreen`: evidenza forward, diagnostics, equity slope e score alignment.
- `HistoryScreen`: posizioni chiuse e exit orders.
- `StrategiesScreen`: pesi e params per timeframe.
- `AlertsScreen`: alert, push, Telegram.
- `SettingsScreen`: operation, risk, services e position settings.

Tutte le chiamate passano da `frontend/lib/api.ts`. Base URL:
`NEXT_PUBLIC_API_BASE_URL`, default `http://localhost:8000`.

Non introdurre state manager esterno senza necessita: il progetto usa React
Query/polling diretto.

## Stile frontend

Mantenere UI da tool operativo: densa, leggibile, orientata a scansione e
azione ripetuta. Evitare landing page o hero marketing. Usare componenti e
pattern gia' presenti. Per icone usare `lucide-react` se disponibili.

Assicurarsi che testo e controlli non si sovrappongano su mobile/desktop.

## Dipendenze

Le dipendenze backend in `backend/pyproject.toml` sono pinned intenzionalmente.
Aggiornare una libreria alla volta e solo dopo verifica test.

Frontend ha workspace npm dalla root e package dedicato in `frontend/package.json`.
Non rigenerare lockfile se non necessario alla modifica.

## Test e verifica

Scegliere test in base al rischio:

- backend core trading/risk: test mirati + suite rilevante;
- DB/migrazioni/settings: test API o worker correlati;
- frontend API shape: build/typecheck frontend se cambiano tipi o payload;
- live execution: test esistenti e verifica guardrail.

Test utili per aree sensibili:

- `backend/tests/test_execution.py`
- `backend/tests/test_execute_live.py`
- `backend/tests/test_order_validation.py`
- `backend/tests/test_live_reconciliation.py`
- `backend/tests/test_position_management.py`
- `backend/tests/test_position_pnl.py`
- `backend/tests/test_risk.py`
- `backend/tests/test_scoring.py`
- `backend/tests/test_experiment.py`
- `backend/tests/test_experiment_outcomes.py`
- `backend/tests/test_btc_macro_protection.py`
- `backend/tests/test_unified_account_ratio.py`
- `backend/tests/test_api_auth.py`
- `backend/tests/test_logging_scrub.py`

## Regole di sicurezza e qualita

- Non abbassare i guardrail live per "far passare" test o demo.
- Non loggare private key, API key, auth headers o payload sensibili raw.
- Non usare `float` per importi se il codice circostante usa `Decimal`.
- Non cambiare default trading aggressivi senza motivazione e test.
- Non bloccare paper positions con limiti pensati per live.
- Non cancellare dati locali o volumi Docker senza richiesta esplicita.
- Non introdurre migrazioni distruttive.
- Non fare chiamate di rete nei test unitari; mockare servizi esterni.
- Non fare refactor ampi mentre risolvi bug puntuali.

## Documenti di riferimento

- `CLAUDE.md`: comandi e note operative sintetiche.
- `MEMORY.md`: conoscenza progetto estesa.
- `docs/USAGE.md`: guida avvio e uso.
- `docs/ARCHITECTURE.md`: architettura e flussi.
- `docs/AUTO_TUNING.md`: ricerca forward e promozione manuale.
- `docs/AUDIT.md`: audit storico e finding tecnici.
