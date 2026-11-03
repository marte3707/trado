# Trado — Conoscenza del Progetto

> Documento di riferimento per Claude Code. Aggiornato al 28/05/2026.

---

## Panoramica

**Trado** è un sistema di trading automatizzato su [Hyperliquid](https://hyperliquid.xyz) (perpetui e spot).
Genera segnali tecnici combinando 15+ strategie, supporta paper trading e live trading con guardrail multipli.

Stack:
- **Backend**: Python 3.11, FastAPI, SQLAlchemy 2.0, PostgreSQL (psycopg3)
- **Worker**: processo asincrono standalone (loop 60s)
- **Frontend**: Next.js 14 (App Router), TypeScript, Tailwind CSS
- **Infrastruttura locale**: Docker Compose per PostgreSQL, launchd per backend e worker

---

## Architettura — Tre processi

```
PostgreSQL (docker, porta host 5433)
    ├── backend/  → FastAPI REST API (porta 8000)
    └── worker/   → ciclo infinito ogni 60s, genera segnali e gestisce posizioni
frontend/          → Next.js, legge solo le API backend
```

---

## Flusso Worker (ogni ciclo)

Per ogni mercato × timeframe attivo:

1. `CandleSyncService.sync_market()` — scarica candele chiuse da Hyperliquid
2. `SignalEngine.generate()` — esegue tutte le strategie del registry
3. `SignalScoringEngine.score()` — combina i risultati pesati per strategy weight
4. `RiskManager` — calcola SL/TP basati su ATR
5. Salva il segnale; notifica Telegram se score ≥ soglia
6. `OrderExecutionEngine` — apre paper/live position in base a `OperationMode`
7. Alla fine del ciclo: `PositionManagementEngine` gestisce le posizioni aperte (trailing stop, TP parziali, breakeven)

---

## Modalità Operative (`OperationMode`)

| Enum | Comportamento |
|------|---------------|
| `SIGNAL_ONLY` | Solo segnali, nessuna posizione |
| `PAPER_TRADING` | Posizioni simulate con equity virtuale |
| `AUTO_TRADE` | Paper + live su Hyperliquid (richiede credenziali + guardrail) |

La modalità effettiva viene letta da `AppSetting("operation_mode")` nel DB, poi da env var come fallback.
Stessa logica per tutti i parametri runtime → `app/core/runtime_settings.py`.

---

## Guardrail Live Trading (5 livelli)

1. `ENABLE_LIVE_TRADING=true` in env
2. `OPERATION_MODE=AUTO_TRADE`
3. `KILL_SWITCH_ENABLED=false`
4. Credenziali `HYPERLIQUID_PRIVATE_KEY` e `HYPERLIQUID_ACCOUNT_ADDRESS` presenti
5. Mainnet richiede anche `ENABLE_MAINNET_LIVE_TRADING=true`
6. `OrderValidator` verifica: equity minima, notional cap, max posizioni aperte (solo live)

**Importante**: `max_open_positions` si applica SOLO alle posizioni `mode == "live"`, mai a paper.

---

## Modelli DB Chiave

| Tabella | Scopo |
|---------|-------|
| `markets` | Mercati attivi (perp + spot), con `metadata_json.hl_coin` per il mapping Hyperliquid |
| `candles` | OHLCV + OI per market × timeframe, fonte primaria per le strategie |
| `indicators` | Risultati indicatori calcolati, JSONB |
| `signals` | Segnali generati, con score, SL/TP, strategy_breakdown JSONB, status |
| `positions` | Posizioni paper e live; `mode` = `"paper"` o `"live"` |
| `position_stop_updates` | Storico aggiornamenti trailing stop per posizione |
| `orders` | Ordini di apertura/chiusura, con `cloid`, fee, raw_response |
| `strategy_configs` | Peso e parametri per strategia × timeframe |
| `app_settings` | Configurazione runtime JSONB (chiavi predefinite sotto) |
| `alerts` | Feed alert con channel, status, payload |
| `account_snapshots` | Snapshot periodici equity/margin/PnL live |
| `push_subscriptions` | Endpoint Web Push attivi |
| `decision_logs` | Log decisionale del worker (event_type, message, data JSONB) |

### Chiavi `AppSetting` predefinite

`operation_mode`, `timeframes`, `paper`, `live_execution`,
`position_management`, `alerts`, `risk`, `research_experiment`,
`btc_macro_protection`

---

## Strategie Registrate (15)

`ema_crossover`, `rsi`, `macd`, `volume_spike`, `fibonacci`,
`support_resistance`, `supertrend`, `bollinger_bands`, `vwap`,
`candlestick_patterns`, `wyckoff_spring_upthrust`, `oi_trend`,
`cvd_divergence`, `liquidation_cascade`, `volume_profile`

Ogni strategia implementa il protocollo `Strategy` (`evaluate(context, params) → StrategyResult`, attributo `name`).
Per aggiungerne una nuova: creare file in `backend/app/strategies/`, registrare in `registry.py`, aggiungere `StrategyConfig` in `init_db.py`.

---

## Engine: Ricerca forward

`backend/app/engine/experiment.py` e
`backend/app/engine/experiment_outcomes.py`

Ogni segnale viene attribuito alla versione e all'hash della configurazione
attiva. Gli outcome paper sono immutabili e alimentano Expected R e pre-TP1
validation solo dopo il campione minimo. La promozione resta manuale.

---

## Engine: Position Management

`backend/app/engine/position_management.py`

Configurazione in `AppSetting("position_management")`, con defaults in `runtime_settings.py`:

- **Trailing stop**: ibrido (Chandelier Exit + Swing + EMA trailing), attivazione a R=1.0
- **Take profit**: TP1 @R=1.0 (30%), TP2 @R=2.0 (30%), runner 40% con trailing stop
- **Time stop**: per timeframe (es. 5m → 12h, 1h → 96h, 1d → 336h)
- **Breakeven**: spostamento SL a breakeven @R=1.0, lock profit @R=2.0

---

## Engine: Diagnostica

`backend/app/engine/diagnostics.py`

Equity slope, outcome profile e statistiche per strategia/timeframe sono
osservative. Il runtime non applica aggiustamenti automatici a pesi o rischio.

---

## Filtri Risk

Configurati in `AppSetting("risk_controls")`:

- `min_signal_score` — soglia minima per aprire posizione
- `regime_filter_enabled` — filtra segnali in regime non-trend (ADX)
- `mtf_confluence_enabled` — richiede conferma multi-timeframe
- `volatility_filter_enabled` — filtra ATR percentile estremo
- `participation_ratio` — frazione equity per posizione
- `max_open_positions` — solo per posizioni **live** (non paper)

---

## API REST (prefix `/api`)

Gruppi principali:

| Gruppo | Endpoint notevoli |
|--------|------------------|
| Mercati | `GET /markets`, `POST /markets/sync` |
| Segnali | `GET /signals`, `POST /signals/{id}/execute-paper`, `POST /signals/{id}/execute-live` |
| Posizioni | `GET /positions`, `POST /positions/{id}/close` |
| Ordini | `GET /orders` |
| Analytics | `/analytics/signals`, `/analytics/diagnostics`, `/analytics/equity-slope`, `/analytics/outcome-profile`, `/analytics/strategy-edge` |
| Ricerca forward | `GET /analytics/experiments`, `GET /analytics/expected-r-calibration`, `GET /analytics/pre-tp1-validation` |
| Settings | `GET /settings`, `PATCH /settings` |
| Strategie | `GET /strategies`, `PATCH /strategies/{id}` |
| Account | `GET /account/snapshot`, ordini e posizioni live da Hyperliquid |
| Sistema | `POST /kill-switch/enable|disable`, `GET /system/error-logs` |

---

## Frontend — Schermate

| Route | Screen | Contenuto |
|-------|--------|-----------|
| `dash` | DashboardScreen | Panoramica operativa |
| `signals` | SignalsScreen | Feed segnali live |
| `coin` | CoinScreen | Chart + indicatori per mercato |
| `account` | AccountMonitorScreen | Posizioni e ordini Hyperliquid |
| `strategy-lab` | StrategyAnalysisScreen | Ranking e combinazioni strategie |
| `calibration` | CalibrationScreen | Evidenza forward, equity slope, diagnostics, score alignment |
| `history` | HistoryScreen | Posizioni chiuse + exit orders |
| `strategies` | StrategiesScreen | Pesi e parametri per timeframe |
| `alerts` | AlertsScreen | Feed alert, push, Telegram |
| `settings` | SettingsScreen | Operation, risk, services, position management |

### Componenti ricerca

- `EquitySlopeTab` — lettura osservativa della curva equity
- `DiagnosticsTab` — report diagnostico dettagliato
- `SignalFunnelChart` — funnel segnali generati → filtrati → eseguiti
- `RejectionReasonsList` — breakdown motivi riezione segnali

---

## Hyperliquid Asset ID

- **Perpetual**: `asset_id = index` nell'array `meta.universe`
- **Spot**: `asset_id = 10000 + spotMeta.universe[index].index`
- Candele spot usano `metadata_json.hl_coin` (es. `PURR/USDC` o `@index`)
- Mapping tenuto in `market.metadata_json["hl_coin"]`; fallback a `market.symbol`

---

## Servizi Esterni

| Servizio | Uso |
|----------|-----|
| Hyperliquid | Exchange principale (perp + spot), candele, ordini, account |
| CoinMarketCap | Market intelligence (prezzi, ranking) |
| Telegram | Notifiche segnali e report diagnostici |
| Web Push | Notifiche browser via `pywebpush` |
| Whale Alert | Segnali di flow whale (opzionale) |

---

## Regole Importanti

1. **Risk controls live-only**: qualsiasi query che conta posizioni aperte per guardrail live deve filtrare `Position.mode == "live"`. Non usare count generico.
2. **Dipendenze pinned**: non aggiornare senza verificare che `pytest` passi completamente.
3. **Riavvio backend/worker**: dopo modifiche a `backend/`, usare launchctl bootout + bootstrap (non `--reload` uvicorn).
4. **Migrazioni**: i modelli personalizzati sono in `app/db/migrations.py`, applicate automaticamente all'avvio. Non serve `alembic upgrade` manuale in sviluppo.
5. **AppSetting come runtime override**: leggere sempre tramite `runtime_settings.py`, mai direttamente da env var senza passare per `effective_*`.

---

## File Chiave (riferimento rapido)

| File | Scopo |
|------|-------|
| `backend/app/core/enums.py` | `OperationMode`, `OrderMode`, `Direction`, `SignalStatus`, `MarketType` |
| `backend/app/core/runtime_settings.py` | Lettura impostazioni runtime da DB con fallback env |
| `backend/app/db/models.py` | Tutti i modelli SQLAlchemy |
| `backend/app/db/init_db.py` | Seed defaults strategie e impostazioni |
| `backend/app/engine/execution.py` | `OrderExecutionEngine` — apertura/chiusura posizioni |
| `backend/app/engine/experiment.py` | Attribuzione e policy dell'esperimento forward |
| `backend/app/engine/experiment_outcomes.py` | Materializzazione outcome forward |
| `backend/app/engine/diagnostics.py` | Diagnostica osservativa ed equity slope |
| `backend/app/engine/position_management.py` | Trailing stop, TP parziali, breakeven |
| `backend/app/engine/order_validation.py` | Validazione pre-esecuzione live (`OrderValidator`) |
| `backend/app/engine/risk.py` | Calcolo position size, SL/TP ATR-based |
| `backend/app/engine/regime.py` | Regime filter (ADX) |
| `backend/app/engine/mtf.py` | Multi-timeframe confluence |
| `backend/app/engine/diagnostics.py` | `build_diagnostics_report`, `equity_slope` |
| `backend/app/api/routes.py` | Tutti gli endpoint REST |
| `backend/app/worker/main.py` | Loop principale worker |
| `backend/app/strategies/registry.py` | Registry delle 15 strategie |
| `backend/app/services/hyperliquid.py` | Client Hyperliquid (read + trading) |
| `frontend/lib/api.ts` | Tutte le chiamate API dal frontend |
| `frontend/lib/hooks/mutations.ts` | React Query mutations |
| `frontend/components/shell/routes.ts` | Definizione route + navigazione |
