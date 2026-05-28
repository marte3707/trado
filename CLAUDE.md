# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Comandi comuni

Tutti i comandi dal virtualenv Python nella root del progetto (`./venv/`).

```bash
# Test backend
npm run backend:test
# oppure un singolo test:
./venv/bin/python -m pytest backend/tests/test_scoring.py

# Lint backend
npm run backend:lint

# Typecheck backend
npm run backend:typecheck

# Frontend dev server
npm run frontend:dev

# Frontend build
npm run frontend:build
```

Avvio sviluppo locale (terminali separati):
```bash
# 1. PostgreSQL (porta host 5433)
docker compose up -d postgres

# 2. Backend API
cd backend && ../venv/bin/python -m uvicorn app.main:app --reload --port 8000

# 3. Worker
cd backend && ../venv/bin/python -m app.worker.main

# 4. Frontend
cd frontend && npm run dev
```

Il backend crea schema e applica migrazioni automaticamente all'avvio. Non servono comandi Alembic manuali per lo sviluppo locale.

## Architettura

Monorepo con tre processi distinti che condividono PostgreSQL:

- **backend/app/** — FastAPI, serve REST API su porta 8000
- **backend/app/worker/main.py** — processo async a ciclo infinito (60s sleep), esegue sincronizzazione candele, generazione segnali, gestione posizioni
- **frontend/** — Next.js 14 + Tailwind, legge le API backend

### Flusso di generazione segnale (worker)

Il worker per ogni mercato × timeframe attivo:

1. `CandleSyncService.sync_market()` — scarica candele chiuse da Hyperliquid
2. `SignalEngine.generate()` — esegue tutte le strategie del registry
3. `SignalScoringEngine.score()` — combina i risultati pesati per strategy weight
4. `RiskManager` — calcola SL/TP basati su ATR
5. Salva il segnale e notifica via Telegram se score ≥ soglia
6. `OrderExecutionEngine` — apre paper/live position in base a `OperationMode`
7. Alla fine del ciclo, `PositionManagementEngine` gestisce le posizioni aperte (trailing stop, TP parziali, breakeven)

### Modalità operative (`OperationMode`)

- `SIGNAL_ONLY` — solo segnali, nessuna posizione
- `PAPER_TRADING` — posizioni simulate con equity virtuale
- `AUTO_TRADE` — paper + live su Hyperliquid (richiede credenziali + guardrail)

La modalità effettiva viene letta da `AppSetting("operation_mode")` nel DB (runtime override) prima di usare la variabile d'ambiente. Stessa logica per `enable_live_trading`, timeframe attivi e altri parametri runtime — vedere `app/core/runtime_settings.py`.

### Guardrail live trading

Il live trading su Hyperliquid è bloccato da più livelli:

1. `ENABLE_LIVE_TRADING=true` in env
2. `OPERATION_MODE=AUTO_TRADE`
3. `KILL_SWITCH_ENABLED=false`
4. Credenziali `HYPERLIQUID_PRIVATE_KEY` e `HYPERLIQUID_ACCOUNT_ADDRESS` presenti
5. Mainnet richiede anche `ENABLE_MAINNET_LIVE_TRADING=true`
6. `OrderValidator` verifica equity minima, notional cap, max posizioni aperte (solo live)

**Attenzione**: `max_open_positions` in risk control si applica solo alle posizioni live, non a quelle paper.

### Aggiungere una nuova strategia

1. Creare `backend/app/strategies/nome_strategy.py` implementando il protocollo `Strategy` (metodo `evaluate(context, params) -> StrategyResult`, attributo `name`)
2. Registrare in `backend/app/strategies/registry.py`
3. Aggiungere entry `StrategyConfig` tramite `seed_defaults` in `backend/app/db/init_db.py`

### DB e migrazioni

I modelli SQLAlchemy sono in `backend/app/db/models.py`. Le migrazioni personalizzate (non Alembic standard) sono in `backend/app/db/migrations.py` e vengono applicate automaticamente all'avvio tramite `apply_migrations()`.

Le impostazioni runtime persistono in `AppSetting` (JSONB) con chiavi predefinite:
`operation_mode`, `timeframes`, `paper`, `live_trading`, `live_execution`, `position_management`, `alerts`, `auto_calibration`, `risk_controls`.

### Hyperliquid asset ID

- Perpetual: `asset_id = index` nell'array `meta.universe`
- Spot: `asset_id = 10000 + spotMeta.universe[index].index`
- Le candele spot usano il campo `metadata.hl_coin` del market (es. `PURR/USDC` o `@index`)

### Frontend

Next.js 14 (App Router) + TypeScript + Tailwind. Tutte le chiamate API passano per `frontend/lib/api.ts`. Il base URL si configura con `NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`). Non usa state manager esterno: polling diretto alle API.

## Riavvio servizi dopo modifiche al backend

Dopo ogni modifica a file in `backend/` riavviare backend e worker tramite launchctl:

```bash
launchctl bootout gui/$(id -u)/com.trado.backend
launchctl bootout gui/$(id -u)/com.trado.worker

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.backend.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.worker.plist
```

Eseguire i comandi nell'ordine indicato (prima bootout di entrambi, poi bootstrap di entrambi). Non usare `--reload` uvicorn in produzione: i servizi sono gestiti da launchd.

## Dipendenze pinned

Le versioni in `backend/pyproject.toml` sono bloccate intenzionalmente. Prima di aggiornare qualsiasi dipendenza verificare che `pytest` passi completamente.
