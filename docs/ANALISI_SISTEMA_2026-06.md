# Analisi di sistema e piano di miglioramento — Giugno 2026

> Documento di revisione su efficienza, performance e qualità del motore di trading Trado.
> Basato su lettura del codice (`backend/app/**`) e su metriche reali estratte dal DB di
> produzione (PostgreSQL, snapshot 2026-06-05).

---

## 1. Executive summary

Il sistema è funzionalmente ricco e ben strutturato (motore segnali multi-strategia,
calibrazione automatica, auto-tuner A/B, guardrail live a più livelli, reconciliation
Hyperliquid). La copertura test è ampia (32 file). Tuttavia emergono **due problemi
strutturali** che limitano scalabilità e qualità:

1. **Il ciclo del worker non scala con il numero di mercati.** Con 524 mercati attivi un
   tick dura in media **839 secondi (~14 min), con picchi a 45 min**, contro un periodo di
   sleep nominale di 60s. I tick vanno costantemente in overlap-skip: il timeframe 5m, che
   dovrebbe rigenerare segnali ogni 5 minuti, di fatto gira con cadenza molto più lenta e
   irregolare. **I segnali arrivano in ritardo rispetto alla candela che li ha generati.**

2. **Ridondanza pesante sul DB nel percorso di generazione segnale.** Per ogni candidato
   (centinaia per tick) `SignalPerformanceAnalyzer.adjusted_weights` esegue 3 query che
   caricano fino a 500 posizioni ciascuna con `joinedload` + una query su `signals`. Gli
   aggregati *global* e *per-timeframe* sono identici per tutti i mercati dello stesso
   timeframe nel medesimo tick, ma vengono **ricalcolati da zero ogni volta**.

Lato qualità trading i numeri sono incoraggianti ma migliorabili:

| Modalità | Trade chiusi | PnL totale | Win rate | Durata media |
|----------|-------------:|-----------:|---------:|-------------:|
| Paper    | 532          | **+1346 USD** | 42.7%  | 54.5 h |
| Live     | 218          | **+39 USD**   | 47.2%  | 22.9 h |

Il paper trading è chiaramente profittevole (winner > loser nonostante WR < 50%). Il **live
è sostanzialmente in pareggio**: stesso edge direzionale ma i costi (fee, slippage,
funding) erodono quasi tutto il margine. Questo è il segnale più importante da indagare
sul piano economico.

---

## 2. Stato attuale (metriche reali)

**Volumi dati**
- Mercati attivi: **524** · Candele: **4.71 M** (840 MB) · Indicatori: 167 k righe (407 MB)
- Segnali: 1260 · Posizioni: 830 (paper 587, live 243)
- Timeframe attivi: `5m, 15m, 1h, 4h`

**Throughput segnali (ultimi giorni):** 3 → 9 → 45 → 103 → 87 al giorno. Score medio ~70.
La generazione è cresciuta molto il 3-4 giugno (probabile allentamento filtri/config).

**Dove muoiono i segnali (skip_reasons aggregati per ciclo, esempi reali):**
```
volatility_filter : 229–486   ← di gran lunga il filtro più aggressivo
low_score         : 55–175
low_liquidity     : 64–171
insufficient_candles: 77–152
risk_reward       : 1–4
directional_*     : 3–8
```
Su ~500–1000 valutazioni per tick, i segnali emessi sono **0–5**. Il filtro di volatilità da
solo scarta tra il 40% e il 60% dei candidati: va verificato che stia tagliando rumore e non
edge reale (vedi §4).

**Tabella dati orfani:** esistono candele `30m` (452 k) e `1d` (134 k) non incluse nei
timeframe attivi — residui storici, potati lentamente dalla retention (60/730 giorni).

---

## 3. Analisi di efficienza e performance

### 3.1 — [CRITICO] Il loop del worker è interamente sequenziale

In [worker/main.py:953-1023](backend/app/worker/main.py#L953-L1023) il ciclo è:

```python
for market in markets:              # ~524 mercati
    for timeframe in paper_active_timeframes:   # fino a 4
        await candle_sync.sync_market(...)      # 1 chiamata HTTP, seriale
        ...
        signal = await asyncio.to_thread(signal_engine.generate, ...)  # seriale
        await asyncio.sleep(0.05)
```

Ogni `sync_market` ([candle_sync.py:34-102](backend/app/services/candle_sync.py#L34-L102))
fa: query `latest_open` → **1 chiamata HTTP Hyperliquid** → N upsert → commit. Con
500–1000 coppie market×timeframe per tick, sono **500–1000 round-trip HTTP eseguiti uno
dopo l'altro**, più i `sleep(0.05)` interposti (≈25–50 s di sleep puro), più la generazione
segnale. Da qui i 14 minuti medi.

**Conseguenze misurate:**
- Tick medio 839 s, picco 2705 s → overlap-skip costante (`_TICK_LOCK`).
- I segnali 5m non rispettano la cadenza a 5 minuti: latenza segnale-candela elevata.
- Lo `sleep(0.05)`/`sleep(0.01)` per ogni iterazione è un costo fisso da centinaia di
  secondi/tick introdotto per fare da rate-limiter rudimentale.

**Leve di miglioramento (in ordine di impatto):**

1. **Parallelizzare i sync HTTP con concorrenza limitata.** `candle_sync.client.candles`
   è già async: raggruppare i fetch con un `asyncio.Semaphore(N)` (es. 8–16) e
   `asyncio.gather` invece del `for ... await` seriale. Anche solo con concorrenza 10 il
   tempo di sync scende di ~10×. La scrittura DB resta serializzata (una sessione), ma il
   costo dominante è la latenza di rete, non l'upsert.

2. **Disaccoppiare sync candele da generazione segnali.** Oggi sync e generazione sono
   intrecciati nello stesso doppio loop. Separandoli in due fasi (prima tutti i sync in
   parallelo, poi la generazione che legge solo dal DB) si elimina il `to_thread` per
   market e si può batchare.

3. **Batch fetch dell'open interest già fatto** (`fetch_perp_open_interests`, una sola
   call) — buon pattern, da replicare per le candele dove possibile.

4. **Rimuovere/ridurre gli `sleep` interni** una volta introdotto un rate-limiter reale
   (semaforo + backoff sui 429), invece di sleep fissi per ogni iterazione.

### 3.2 — [CRITICO] `adjusted_weights`: 3 query pesanti ridondanti per candidato

In [signal_engine.py:672-677](backend/app/engine/signal_engine.py#L672-L677)
`self.performance.adjusted_weights(...)` viene chiamato per **ogni** market×timeframe che
supera i primi filtri (volatilità, liquidità, non-duplicato). Internamente
([performance.py:417-419](backend/app/engine/performance.py#L417-L419)):

```python
scoped_stats   = self.strategy_stats(db, timeframe=timeframe, market_id=market_id)
timeframe_stats = self.strategy_stats(db, timeframe=timeframe)
global_stats   = self.strategy_stats(db)          # identico per tutti i market!
```

Ogni `strategy_stats` → `strategy_positions` → `closed_paper_positions` carica fino a **500
posizioni con `joinedload(Position.market)`** + una query `Signal.id.in_(...)` con parsing
del JSONB `strategy_breakdown`. Con ~150–200 candidati/tick parliamo di **~600 query
pesanti per tick**, la maggior parte delle quali ricalcola gli **stessi** aggregati
global/timeframe.

Nota aggiuntiva: con solo 532 posizioni paper distribuite su 524 mercati, lo `scoped_stats`
per singolo `market_id` ha quasi sempre `< min_trades (20)` → ricade su timeframe/global.
**La query scoped è quindi quasi sempre lavoro sprecato.**

**Fix proposto (alto impatto, basso rischio):**
- Cachare per-cycle `global_stats` e `timeframe_stats[timeframe]` nel `SignalEngine`,
  invalidandoli in `begin_cycle()` (stesso pattern già usato per fundings/L2 book).
- Calcolare lo `scoped_stats` solo quando il mercato ha effettivamente ≥ `min_trades`
  posizioni (pre-check `COUNT` leggero o conteggio in memoria), altrimenti saltarlo.
- Atteso: da ~600 query/tick a **1 (global) + ≤4 (timeframe) + pochi scoped** → riduzione
  di oltre il 95% del carico DB di questo percorso.

### 3.3 — [MEDIO] Indicatori ricalcolati da ogni strategia sugli stessi candle

Le 18 strategie ricevono lo stesso `StrategyContext.candles` e ognuna ricalcola
indipendentemente EMA/RSI/ATR/ADX/Bollinger/... su 250 candele in puro Python
([indicators/core.py](backend/app/indicators/core.py)). ATR in particolare viene calcolato
più volte (strategie + `risk.build_plan` + supertrend). Non è il collo di bottiglia
primario (domina la rete), ma su 150+ candidati/tick è overhead CPU evitabile.

**Fix:** un `IndicatorCache` per `(market_id, timeframe, latest_open_time)` che memoizza gli
indicatori comuni e li passa alle strategie. Da affrontare **dopo** §3.1/§3.2.

### 3.4 — [MEDIO] `db.commit()` ad ogni skip nel percorso di generazione

`generate()` esegue `db.commit()` su molti rami di skip (volatility, funding_block,
whale_contra, low_score, mtf, risk_reward…). La commit serve a persistere `_persist_indicators`
e gli effetti collaterali, ma su centinaia di skip/tick genera molte transazioni brevi.
Valutare un commit unico a fine valutazione del market (o `flush` + commit batch).

### 3.5 — [BASSO] Tabella `indicators` scrive ad ogni valutazione

[signal_engine.py:670](backend/app/engine/signal_engine.py#L670) fa upsert su `indicators`
per **ogni** market×timeframe valutato, anche quando il segnale verrà scartato subito dopo.
407 MB / 167 k righe. Se gli indicatori storici servono solo per i segnali emessi (o per
debug), si può scrivere solo quando il segnale supera la soglia, riducendo I/O e dimensione
tabella. Se invece servono per analisi offline, lasciare ma valutare retention più stretta.

### 3.6 — Cose già fatte bene (da preservare)

- Cache per-cycle di **fundings** e **L2 orderbook** (`begin_cycle`) — ottimo pattern.
- Cache TTL su **CoinMarketCap** e **Whale Alert** — evita hammering API esterne.
- **Early-exit duplicate check** spostato in cima alla funzione — buona ottimizzazione.
- Indice composito `uq_candle_market_tf_time` copre perfettamente la query candele
  (`market_id, timeframe, open_time DESC LIMIT 250`).
- Batch `fetch_perp_open_interests` (una call per tutto l'OI perp).
- Heartbeat rinfrescato durante tick lunghi per non far flippare `/health` a "stale".

---

## 4. Analisi qualità segnali / trading

### 4.1 — Il gap paper vs live è il problema economico n.1
Stesso edge direzionale (WR 42.7% vs 47.2%) ma PnL **+1346** paper contro **+39** live. La
differenza non è il segnale: è **costo di esecuzione** (fee taker, slippage di mercato,
funding pagato su hold di ~23h). Azioni:
- Misurare per ogni trade live: slippage entry/exit, fee pagate, funding cumulato (i campi
  esistono: `Order.fee`, `Position.cum_funding_since_open`).
- Confrontare entry teorico (segnale) vs entry reale → quantificare lo slippage medio.
- Valutare ordini limit/post-only o riduzione frequenza su mercati illiquidi.

### 4.2 — Il filtro volatilità è il più aggressivo: validarlo
`volatility_filter` scarta 40–60% dei candidati. Esiste già un bypass automatico in
starvation ([signal_engine.py:500-509](backend/app/engine/signal_engine.py#L500-L509)).
Va verificato con un backtest A/B (i trade scartati dal filtro avrebbero perso?) che il
filtro stia rimuovendo rumore e non edge. La banda `atr_pct_min/max = 0.25/0.90` è un
default ragionevole ma andrebbe calibrata sui dati reali per timeframe.

### 4.3 — Coerenza dei due aggiustatori di peso
`adjusted_weights` (performance) e `AutoCalibrationEngine._strategy_factor` usano entrambi i
Wilson bounds — buona coerenza dichiarata. Da tenere monitorato che non si sovrappongano
(doppia penalizzazione della stessa strategia).

---

## 5. Manutenibilità e rischio

- **File molto grandi**: `api/routes.py` (2574), `live_reconciliation.py` (2321),
  `execution.py` (1806), `worker/main.py` (1284). `generate()` da sola è ~740 righe con
  decine di rami di filtro. Difficile da testare a unità e da modificare senza regressioni.
  Candidati a refactor in pipeline di "filter step" componibili.
- **`worker/main.py`** mescola orchestrazione, notifiche Telegram, guardrail UAR, recap.
  Estrarre i blocchi schedulati (recap/diagnostic/retention/guard) in uno scheduler dedicato.
- Dipendenze pinnate e `pip-audit` in CI — buona igiene supply chain (da `pyproject.toml`).

---

## 6. Piano di intervento prioritizzato

Legenda impatto/effort: 🔴 alto · 🟡 medio · 🟢 basso.

### Fase 1 — Performance critica (1–2 giorni) — sblocca la scalabilità

| # | Intervento | Impatto | Effort | File |
|---|-----------|:-------:|:------:|------|
| 1.1 | **Cache per-cycle di `global_stats` e `timeframe_stats`** in `SignalEngine`, invalidate in `begin_cycle()`; skip dello `scoped_stats` sotto `min_trades` | 🔴 | 🟢 | `performance.py`, `signal_engine.py` |
| 1.2 | **Parallelizzare i sync candele** con `asyncio.Semaphore` + `gather` (concorrenza 8–16) | 🔴 | 🟡 | `worker/main.py`, `candle_sync.py` |
| 1.3 | Rimuovere gli `sleep(0.05)/(0.01)` per-iterazione dopo aver introdotto il semaforo | 🟡 | 🟢 | `worker/main.py` |

> Atteso fine Fase 1: tick da ~14 min a **1–3 min**, carico DB del percorso segnale −95%,
> cadenza 5m rispettata.

### Fase 2 — Qualità dati e I/O (1–2 giorni)

| # | Intervento | Impatto | Effort |
|---|-----------|:-------:|:------:|
| 2.1 | Commit batch per market invece di commit ad ogni skip | 🟡 | 🟢 |
| 2.2 | Scrivere `indicators` solo per segnali emessi (o retention più stretta) | 🟡 | 🟢 |
| 2.3 | Separare fase *sync* da fase *generate* nel worker | 🟡 | 🟡 |
| 2.4 | `IndicatorCache` condiviso tra strategie per market×tf | 🟡 | 🟡 |

### Fase 3 — Edge economico (analisi, 2–3 giorni)

| # | Intervento | Impatto | Effort |
|---|-----------|:-------:|:------:|
| 3.1 | Strumentare slippage/fee/funding per trade live e produrre report costo-per-trade | 🔴 | 🟡 |
| 3.2 | Backtest A/B del `volatility_filter` (edge scartato vs rumore) | 🟡 | 🟡 |
| 3.3 | Valutare ordini limit/post-only e filtro liquidità più severo per il live | 🟡 | 🟡 |

### Fase 4 — Manutenibilità (continuo)

| # | Intervento | Impatto | Effort |
|---|-----------|:-------:|:------:|
| 4.1 | Refactor `generate()` in pipeline di filter-step testabili | 🟡 | 🔴 |
| 4.2 | Estrarre scheduler dei job periodici da `worker/main.py` | 🟢 | 🟡 |
| 4.3 | Spezzare `api/routes.py` per dominio | 🟢 | 🟡 |

---

## 7. Raccomandazione

Iniziare dalla **Fase 1** (in particolare 1.1 e 1.2): sono i due interventi a massimo
rapporto impatto/rischio, non cambiano la logica di trading e sbloccano la scalabilità del
sistema all'aumentare dei mercati. La **Fase 3.1** (misura dei costi live) è la più
importante sul piano economico ed è puramente additiva (strumentazione), quindi può
procedere in parallelo senza rischi.

---

## 8. Stato di implementazione e risultati (aggiornamento)

### Fase 1 — ✅ implementata (commit `8352d24`)
Cache per-cycle degli aggregati di performance + parallelizzazione dei fetch candele
(`prefetch` con `Semaphore`) + rimozione degli sleep per-iterazione.

### Fase 2 — ✅ implementata (commit `ed3a992`)
`_persist_indicators` spostato a valle di tutti i filtri (scrittura solo per segnali
emessi); riparati gli errori preesistenti di mypy/ruff in `live_reconciliation.py`
(backend ora pulito su 72 file).

### Fase 3 — ✅ implementata (strumentazione costi)

**3.1 — Report costo-per-trade live.** Aggiunti: setting `live_taker_fee_rate`
(default 0.045%), tracciamento della **fee reale** dei fill nella reconciliation
(`Order.fee`, finora ignorata), modulo `services/trade_cost.py` e endpoint
`GET /analytics/live-trade-costs`.

Risultati sui **218 trade live chiusi** (dati reali di produzione):

| Voce | Valore |
|------|-------:|
| PnL lordo (da `closedPnl`) | +39.35 USD |
| Notional totale | 8 512 USD (≈39 USD/trade) |
| Fee stimate (0.045% × 2 lati) | −7.66 USD |
| Funding | ≈0 USD |
| **PnL netto stimato** | **≈ +31.70 USD** |

Finding principali:
- Le **fee non erano né tracciate né sottratte**: erodono ~19% del PnL lordo. Il
  PnL netto resta positivo ma sottile (~0.145 USD/trade).
- Lo **slippage di entry** è già incorporato nel `closedPnl` (usa l'entry reale),
  quindi è una metrica *informativa*, non sottratta dal netto. In aggregato è stato
  lievemente **favorevole** (≈ −15 USD di "costo", cioè a beneficio): l'esecuzione
  market non sta penalizzando.
- Il **notional medio è molto piccolo** (~39 USD contro un cap di 250): il sistema
  opera in size ridotta.

**3.2 — Validazione del filtro di volatilità.** Analisi dei 115 trade paper con dato
`atr_percentile_rank` registrato:

| Fascia ATR rank | Trade | Win rate | PnL |
|-----------------|------:|---------:|----:|
| 0.25–0.50 | 42 | 50.0% | +1242 |
| 0.50–0.75 | 36 | 50.0% | +810 |
| 0.75–0.90 | 34 | **55.9%** | **+1946** |

- L'ATR percentile rank **non discrimina** vincenti da perdenti (rank medio 0.579 vs
  0.594): tra i segnali emessi non c'è segnale predittivo nel livello di volatilità.
- I trade ad **alta volatilità** (vicino al cap 0.90) hanno il **WR e il PnL migliori**
  → il cap superiore a 0.90 rischia di tagliare *edge*, non rumore.
- **Limite metodologico**: i candidati scartati dal filtro non vengono loggati, quindi
  l'A/B diretto non è possibile sui dati esistenti.

Raccomandazioni 3.2 (non ancora implementate — decisioni di prodotto):
1. Loggare un campione dei candidati scartati dal `volatility_filter` con esito
   ipotetico, per abilitare un vero A/B.
2. Valutare di **alzare/rimuovere il cap superiore** (0.90): l'alta volatilità correla
   con performance migliore.
3. Il taglio inferiore (~0.25) appare ragionevole (rimuove mercati morti).

**3.3 — Esecuzione live.** Dato lo slippage favorevole e il notional ridotto, gli ordini
limit/post-only non sono una priorità immediata; il margine maggiore viene dal ridurre il
peso relativo delle fee (size più grandi o meno trade su mercati a basso edge). Resta una
decisione di prodotto.
