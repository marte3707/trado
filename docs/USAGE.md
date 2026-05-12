# Guida utilizzo

## Modalita'

- `SIGNAL_ONLY`: genera e salva segnali, non apre posizioni.
- `PAPER_TRADING`: permette posizioni simulate.
- `AUTO_TRADE`: riservata alla fase successiva; richiede anche `ENABLE_LIVE_TRADING=true`.

Non salvare private key nel database. Se serve live trading, impostare `HYPERLIQUID_PRIVATE_KEY` solo come variabile ambiente del backend/worker.

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
```

Non inserire private key per l'MVP. Il live trading non e' implementato nella prima versione.

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
- apre paper trade solo se `OPERATION_MODE=PAPER_TRADING`.

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
