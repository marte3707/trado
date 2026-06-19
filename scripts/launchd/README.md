# LaunchAgent di Trado

Questo folder contiene gli `.plist` di macOS `launchd` che fanno partire
automaticamente backend (`uvicorn`), market-data WebSocket
(`python -m app.market_data.main`) e worker (`python -m app.worker.main`) al
login dell'utente, e li rilanciano in caso di crash.

## Comportamento

- **All'accensione del Mac**: i servizi partono dopo il login del tuo utente
  (`RunAtLoad: true`).
- **Ritorno da standby**: i processi non vengono uccisi durante lo sleep — al
  wake riprendono l'esecuzione da dove erano (asyncio `sleep(60)` del worker e
  socket server di uvicorn rimangono vivi). Se per qualsiasi motivo un processo
  fosse morto, `KeepAlive.Crashed=true` lo rilancia entro pochi secondi (con
  `ThrottleInterval` minimo fra tentativi).
- **Crash**: rilancio automatico. `SuccessfulExit=false` evita di rilanciare se
  il processo termina con `exit(0)` (es. graceful shutdown via SIGTERM).

## Installazione

```bash
bash scripts/launchd/install.sh
```

Lo script:

1. Copia i `.plist` in `~/Library/LaunchAgents/`.
2. Fa `launchctl bootout` di eventuale versione precedente.
3. Fa `launchctl bootstrap` + `enable` per caricarli e attivarli.

I servizi partono immediatamente. Verifica:

```bash
launchctl print gui/$(id -u)/com.trado.backend | head -20
launchctl print gui/$(id -u)/com.trado.marketdata | head -20
launchctl print gui/$(id -u)/com.trado.worker | head -20
```

Cerca le righe `state = running` e `last exit code = 0`.

## ATTENZIONE — conflitti con processi esistenti

Prima di installare, **ferma le istanze di uvicorn/worker lanciate a mano**,
altrimenti `bootstrap` fallirà o ti ritroverai due processi sulla stessa porta
8000. Per fermarle:

```bash
pkill -f "uvicorn app.main:app"
pkill -f "python -m app.worker.main"
```

## Disinstallazione

```bash
bash scripts/launchd/uninstall.sh
```

Rimuove `.plist` e ferma i servizi.

## Log

I servizi scrivono su:

- `~/Library/Logs/trado-backend.out.log` / `.err.log`
- `~/Library/Logs/trado-marketdata.out.log` / `.err.log`
- `~/Library/Logs/trado-worker.out.log`  / `.err.log`

Per seguire in real time:

```bash
tail -f ~/Library/Logs/trado-backend.err.log
tail -f ~/Library/Logs/trado-marketdata.err.log
tail -f ~/Library/Logs/trado-worker.err.log
```

(L'app usa `structlog`; molti messaggi finiscono su stderr, da lì
`.err.log`.)

## Sviluppo: come disattivare temporaneamente

Quando vuoi sviluppare con `uvicorn --reload`:

```bash
launchctl bootout gui/$(id -u)/com.trado.backend
# ora lancia uvicorn a mano con --reload
# quando hai finito:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.backend.plist
```

Comodo: il worker continua a girare via launchd anche mentre rimpiazzi solo
il backend.

## Caratteristiche delle plist

| Chiave             | Backend           | Market data           | Worker            |
|--------------------|-------------------|-----------------------|-------------------|
| Label              | com.trado.backend | com.trado.marketdata  | com.trado.worker  |
| RunAtLoad          | true              | true                  | true              |
| KeepAlive.Crashed  | true              | true                  | true              |
| ThrottleInterval   | 10 s              | 15 s                  | 15 s              |
| ProcessType        | Interactive       | Background            | Background        |
| WorkingDirectory   | `backend/`        | `backend/`            | `backend/`        |

`Interactive` per il backend evita aggressivo throttling I/O che macOS
applicherebbe ai `Background`; il worker invece è giustamente Background
(non serve responsiveness).

Il `.env` viene letto da pydantic-settings tramite il path `../.env`
relativo a `WorkingDirectory=backend/`, quindi `<repo>/.env`.

## Postgres

Postgres è già gestito come servizio `homebrew.mxcl.postgresql@*`
(`brew services list`) e parte da solo al login. Non serve un altro
LaunchAgent.

Il flusso al boot del Mac è quindi:

1. `brew services` avvia Postgres.
2. `com.trado.backend` avvia uvicorn → tabelle migrate e seedate al startup.
3. `com.trado.marketdata` apre le subscription candle e persiste gli eventi chiusi.
4. `com.trado.worker` consuma gli eventi, genera segnali e mantiene il fallback REST.
5. AutoTuner riprende lo stato dal DB (se era attivo).
