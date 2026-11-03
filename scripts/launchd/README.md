# LaunchAgent di Trado

Questo folder contiene gli `.plist` di macOS `launchd` che fanno partire
automaticamente backend (`uvicorn`), market-data WebSocket
(`python -m app.market_data.main`), worker (`python -m app.worker.main`) e un
watchdog di health al login dell'utente.

L'avvio e' mediato da uno script wrapper che:

- apre Docker Desktop se non e' gia' attivo;
- esegue `docker compose up -d postgres`;
- aspetta che PostgreSQL del compose sia `healthy` su `localhost:5433`;
- solo dopo esegue il processo Python del servizio.

## Comportamento

- **All'accensione del Mac**: i servizi partono dopo il login del tuo utente
  (`RunAtLoad: true`).
- **Dipendenza da Docker**: backend, market-data e worker non vanno piu' in
  crash se Docker Desktop impiega tempo a salire; restano in preflight finche'
  il container `postgres` non e' pronto.
- **Ritorno da standby**: i processi non vengono uccisi durante lo sleep — al
  wake riprendono l'esecuzione da dove erano (asyncio `sleep(60)` del worker e
  socket server di uvicorn rimangono vivi). Se per qualsiasi motivo un processo
  fosse morto, `KeepAlive.Crashed=true` lo rilancia entro pochi secondi (con
  `ThrottleInterval` minimo fra tentativi).
- **Crash**: rilancio automatico. `SuccessfulExit=false` evita di rilanciare se
  il processo termina con `exit(0)` (es. graceful shutdown via SIGTERM).
- **Processo vivo ma unhealthy**: il watchdog interroga `/api/health` ogni 30
  secondi. Dopo tre failure consecutive riavvia solo il servizio interessato;
  rileva anche quattro stati unhealthy in una finestra di 10 minuti, anche
  alternati a recuperi brevi. Un cooldown di 5 minuti impedisce restart loop.
- **Backend irraggiungibile**: viene riavviato solo il backend; worker e
  market-data restano in stato sconosciuto per evitare restart a cascata.

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
launchctl print gui/$(id -u)/com.trado.watchdog | head -20
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
- `~/Library/Logs/trado-watchdog.out.log` / `.err.log`

Per seguire in real time:

```bash
tail -f ~/Library/Logs/trado-backend.err.log
tail -f ~/Library/Logs/trado-marketdata.err.log
tail -f ~/Library/Logs/trado-worker.err.log
tail -f ~/Library/Logs/trado-watchdog.err.log
```

(L'app usa `structlog`; molti messaggi finiscono su stderr, da lì
`.err.log`.)

## Sviluppo: come disattivare temporaneamente

Quando vuoi sviluppare con `uvicorn --reload`:

```bash
launchctl bootout gui/$(id -u)/com.trado.backend
# sospende l'auto-recovery finche' il file esiste
touch .local/watchdog.pause
# ora lancia uvicorn a mano con --reload
# quando hai finito:
rm .local/watchdog.pause
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.backend.plist
```

Comodo: il worker continua a girare via launchd anche mentre rimpiazzi solo
il backend.

## Caratteristiche delle plist

| Chiave             | Backend           | Market data           | Worker            | Watchdog           |
|--------------------|-------------------|-----------------------|-------------------|--------------------|
| Label              | com.trado.backend | com.trado.marketdata  | com.trado.worker  | com.trado.watchdog |
| RunAtLoad          | true              | true                  | true              | true               |
| KeepAlive.Crashed  | true              | true                  | true              | true               |
| ThrottleInterval   | 10 s              | 15 s                  | 15 s              | 15 s               |
| ProcessType        | Interactive       | Background            | Background        | Background         |
| WorkingDirectory   | `backend/`        | `backend/`            | `backend/`        | `backend/`         |

`Interactive` per il backend evita aggressivo throttling I/O che macOS
applicherebbe ai `Background`; il worker invece è giustamente Background
(non serve responsiveness).

Il `.env` viene letto da pydantic-settings tramite il path `../.env`
relativo a `WorkingDirectory=backend/`, quindi `<repo>/.env`.

Per questa modalita', `DATABASE_URL` deve puntare al PostgreSQL del compose:

```bash
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/trado
```

## Postgres

Postgres gira nel container Docker definito in `docker-compose.yml`.
I LaunchAgent provano ad avviare Docker Desktop e il servizio `postgres`
automaticamente. Se Docker Desktop non e' installato in
`/Applications/Docker.app`, il preflight fallira' e il motivo comparira' nei
file `trado-*.err.log`.

Il flusso al boot del Mac è quindi:

1. `com.trado.*` parte al login.
2. Il wrapper apre Docker Desktop se serve.
3. Il wrapper esegue `docker compose up -d postgres`.
4. Il wrapper aspetta `postgres` healthy su `localhost:5433`.
5. Solo allora parte uvicorn / market-data / worker.
6. AutoTuner riprende lo stato dal DB (se era attivo).
