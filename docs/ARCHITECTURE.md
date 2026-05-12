# Architettura

## Componenti

- `frontend`: dashboard Next.js + TypeScript. Legge le API FastAPI e mostra segnali, mercati, strategie, risk controls e stato trading.
- `backend`: FastAPI con endpoint REST, persistence SQLAlchemy e streaming SSE interno.
- `worker`: processo Python async sempre attivo. Sincronizza mercati, aggiorna candles, calcola indicatori, genera segnali, invia alert e passa al motore ordini solo se consentito.
- `postgres`: storage per mercati, candles, indicatori, segnali, posizioni, ordini, configurazioni, alert, snapshot account e decision log.

## Hyperliquid

Il client in `backend/app/services/hyperliquid.py` usa gli endpoint ufficiali:

- `POST /info` per `meta`, `spotMeta`, `allMids`, `candleSnapshot`, account state e ordini aperti.
- `POST /exchange` e' incapsulato in `OrderExecutionEngine`, ma il live e' disabilitato finche' non sono presenti tutte le protezioni esplicite.

Regole asset:

- Perpetual: `asset_id = index` nell'array `meta.universe`.
- Spot: `asset_id = 10000 + spotMeta.universe[index].index`.
- Per le candle spot si usa il coin name Hyperliquid (`PURR/USDC` oppure `@index`) salvato in `metadata.hl_coin`.

## Flusso segnale

1. Il worker scarica solo candele chiuse.
2. Le strategie indipendenti implementano l'interfaccia comune `Strategy`.
3. Ogni strategia produce `StrategyResult` con direzione, confidence, reason, indicator values, entry, SL/TP suggeriti e timestamp.
4. `SignalScoringEngine` combina i risultati e applica soglie/penalita'.
5. `RiskManager` calcola SL/TP con ATR e risk/reward.
6. Il segnale viene salvato e notificato.
7. `OrderExecutionEngine` puo' aprire paper trade; live trading richiede `AUTO_TRADE`, flag live, kill switch spento e validazione rischio.

## Estensioni previste

- Fibonacci automatico
- Supporti/resistenze
- Supertrend
- Bollinger Bands
- VWAP
- Pattern candlestick
- Live execution completa tramite SDK ufficiale Hyperliquid
- Performance analytics sui segnali

