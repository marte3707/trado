# Trado

Web app personale per generare segnali crypto spot e perpetual su Hyperliquid.

Stato attuale: scaffold MVP con backend FastAPI, worker Python, PostgreSQL via SQLAlchemy, client Hyperliquid REST, dashboard Next.js e moduli per indicatori, strategie, scoring, risk management, paper trading e alert Telegram.

Il default e' sempre non-live: `SIGNAL_ONLY`. Il live trading richiede sia `OPERATION_MODE=AUTO_TRADE` sia `ENABLE_LIVE_TRADING=true`.

## Avvio locale

```bash
cp .env.example .env
docker compose up -d postgres
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

In un secondo terminale:

```bash
cd frontend
npm install
npm run dev
```

Worker:

```bash
cd backend
. .venv/bin/activate
python -m app.worker.main
```

Documentazione:

- [Architettura](docs/ARCHITECTURE.md)
- [Guida utilizzo](docs/USAGE.md)

