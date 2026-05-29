# Audit Trado — Verifica completa strategie, calibrazione, gestione posizioni e protezione macro

**Data:** 2026-05-19
**Autore:** audit interno
**Branch:** `main`
**Scope:** strategie, scoring, calibrazione, auto-tuner, take-profit/trailing/stop, BTC macro protection, worker.

---

## Aggiornamento operativo — 2026-05-29

- `mtf_confluence` è stato verificato nello stato AutoTuner come `reverted`: dopo l'attivazione ha prodotto WR 22.2%, PnL `-4224.1587` e PF `0.2840`, contro baseline WR 58%, PnL `+4052.5030` e PF `2.5292`.
- La configurazione runtime deve tenerlo disattivato globalmente (`risk.mtf_confluence_enabled=false`) salvo nuovo test A/B isolato.
- L'aggressività live è intenzionale e non è un finding: non ridurre leva, esposizione, cap posizioni, `operation_mode` o `enable_live_trading` in remediation automatiche.
- Risolti i blocchi statici emersi dall'audit (`ruff`/`mypy`) e aggiunta protezione contro loop di re-submit sugli ordini live di protezione: ogni ordine mantiene storico persistente `raw_response.resubmits`, cooldown su retry riusciti recenti e cap totale di tentativi.

---

## Executive Summary

Lo stato corrente del sistema mostra un anomalia operativa: dal 16/05/2026 il deep defensive è attivo, il min_signal_score è salito a 72 e da diversi cicli non viene generato alcun segnale. L'auto-tuner è abilitato ma non riesce a completare alcuno step perché aspetta posizioni chiuse che non arrivano.

L'audit ha identificato **35 finding** distribuiti tra correttezza, robustezza, performance e osservabilità. Tre sono i pattern chiave:

1. **Lock-in deep defensive ↔ starvation** — la calibrazione alza la soglia, blocca i segnali, e impiega 30–60 cicli per allentare. È un circolo vizioso.
2. **Stato corrotto nella BTC Macro Protection** — i reversal attraverso uno stato NEUTRAL vengono persi, e un `db.rollback()` su una posizione cancella anche lo stato che era stato già flushed.
3. **Magic number diffusi** — partecipation ratio, soglia STRONG, soglia funding, min_risk_reward minimo: tutti hardcoded, alcuni in conflitto con la config DB.

Tutte le correzioni descritte in questo documento verranno implementate sul branch corrente. I finding sono numerati progressivamente per facilitare il riferimento nei commit.

---

## CRITICI — Correttezza

### F-01. BTC Macro Protection: reversal persi attraverso NEUTRAL

**File:** [backend/app/engine/btc_macro_protection.py:82-92](backend/app/engine/btc_macro_protection.py#L82-L92)

`_save_state` viene chiamato sempre con `current_trend`, anche quando vale `NEUTRAL` (es. volume sotto soglia, delta sotto threshold). Se il flusso reale è `BULLISH → NEUTRAL → BEARISH`, dopo la candela neutrale `_last_btc_trend = NEUTRAL`; al successivo ciclo `BEARISH` la condizione `last_trend != NEUTRAL and current_trend != NEUTRAL and last_trend != current_trend` è falsa e il reversal **non viene rilevato**.

**Impatto:** in regime volatile, molti reversal BTC vengono ignorati. La protezione perde la sua funzione principale.

**Fix:** mantenere un campo `_last_directional_trend` che si aggiorna solo se `current_trend != NEUTRAL`, separato da `_last_observed_trend` (storico osservazione). Confrontare il directional contro il nuovo trend per il check di reversal.

### F-02. BTC Macro Protection: `db.rollback()` cancella lo stato già flushed

**File:** [backend/app/engine/btc_macro_protection.py:118-134](backend/app/engine/btc_macro_protection.py#L118-L134)

A riga 83 viene fatto `_save_state` (che internamente fa `db.flush()`). Successivamente, se `_apply_to_position` solleva eccezione (riga 126), `db.rollback()` annulla **anche** il `flush` dello stato. Risultato: al ciclo successivo `_last_btc_trend` è ancora il vecchio valore, il reversal viene rilevato di nuovo e si tentano altre operazioni sulle stesse posizioni.

**Impatto:** loop di tentativi su una posizione problematica; metriche di "btc_protection_updates" duplicate; possibile ordine SL doppio sull'exchange se il primo tentativo era andato a buon fine ma con eccezione successiva.

**Fix:** persistere lo stato del trend in una transazione dedicata (commit immediato dopo `_save_state`), oppure rifare `_save_state` dopo il `db.rollback()` con un sub-commit isolato.

### F-03. Scoring: discretizzazione `round` perde contributi deboli

**File:** [backend/app/engine/scoring.py:44](backend/app/engine/scoring.py#L44)

```python
contribution = round((max(0, min(result.confidence, 100)) / 100) * weight)
```

Con `weight=10` (default), `confidence < 5` produce `contribution = 0`. Il `voting_weight` però viene incrementato a riga 47-50 perché la direzione non è NEUTRAL → la strategia "partecipa" senza apportare nulla, falsando la normalizzazione e il participation ratio.

**Impatto:** segnali deboli ma reali (es. confidenza 4 su 10 strategie) spariscono dal calcolo. Lo score finale è sottostimato.

**Fix:** mantenere `contribution` come `float` fino alla normalizzazione finale; arrotondare solo `normalized_long_score` e `normalized_short_score`.

### F-04. Scoring: parità tra long e short produce score > 0 ma direzione NEUTRAL

**File:** [backend/app/engine/scoring.py:73-75](backend/app/engine/scoring.py#L73-L75)

```python
if normalized_long_score == normalized_short_score:
    direction = Direction.NEUTRAL
    score = normalized_long_score
```

In caso di parità, `direction = NEUTRAL` ma `score` resta uguale al valore comune. Decisioni a valle che filtrano per `score < min_signal_score` (es. [signal_engine.py:515](backend/app/engine/signal_engine.py#L515)) trattano il segnale come "skipped_low_score" anche se in realtà è "skipped_neutral_balanced", confondendo i log.

**Fix:** quando parity, forzare `score = 0` e `confidence_label = "debole"`. Garantisce coerenza dei filtri downstream e migliora la diagnosi nei log.

### F-05. Calibrazione: lock-in deep defensive con starvation hardcoded

**File:** [backend/app/engine/calibration.py:336-388](backend/app/engine/calibration.py#L336-L388)

La sequenza patologica:

1. `equity_slope < -2.0` → deep defensive ON → `score_delta = +12` → `min_score = 72`.
2. Min_score 72 è quasi irraggiungibile sugli asset calmi → 0 segnali generati.
3. Senza nuovi trade chiusi, equity_slope **non cambia** (resta ≈ stesso valore) → deep defensive resta attivo.
4. Lo starvation_soft (default 30 cicli) entra in azione: attenuazione a `score_delta = 6` → `min_score = 66`. Ancora alto.
5. Lo starvation_hard (default 60 cicli) finalmente disattiva il deep defensive. Sono passati ≈60 minuti.

**Impatto:** la situazione reale del 16/05/2026 (0 segnali, deep defensive attivo) è esattamente questa pattern. Il sistema impiega un'ora a riprendersi anche quando il mercato è già tornato favorevole.

**Fix multipli:**
- Ridurre `starvation_soft_cycles` di default da 30 a 10 e `starvation_hard_cycles` da 60 a 20.
- Aggiungere un trigger "equity_slope ambiguo" (NaN, <5 trade chiusi): allenta automaticamente al baseline.
- Emettere `DecisionLog` `deep_defensive_lock_warning` quando lo stato persiste >5 cicli senza nuovi trade.

### F-06. Calibrazione: `KeyError` su `min_risk_per_trade_pct`

**File:** [backend/app/engine/calibration.py:376,384,394](backend/app/engine/calibration.py#L376)

```python
risk_pct = max(float(settings["min_risk_per_trade_pct"]), base_risk * 0.75)
```

Se `settings` (merge di `auto_calibration` + default) non contiene la chiave (config legacy o seed_defaults rimosso), solleva `KeyError`. L'eccezione è catturata dal try/except in [worker/main.py:165-179](backend/app/worker/main.py#L165-L179) come `auto_calibration_failed`, ma silenzia un fallimento sistemico.

**Fix:** usare `settings.get("min_risk_per_trade_pct", DEFAULT_MIN_RISK_PER_TRADE_PCT)` con default coerente (es. 0.25).

### F-07. Position Management: division-by-zero quando `current_stop == 0`

**File:** [backend/app/engine/position_management.py:322](backend/app/engine/position_management.py#L322)

```python
distance_pct = abs(new_stop - current_stop) / current_stop * Decimal("100")
```

Edge case improbabile ma reale: se un bug a monte salva SL=0 (es. fallback nel risk plan), questa divisione esplode. L'eccezione non è catturata localmente, sale al worker.

**Fix:** guard clause `if current_stop is None or current_stop <= 0: return candidate` (lascia passare l'aggiornamento senza min-distance check, l'unica via di uscita da uno stato corrotto).

### F-08. Position Management: `updateOnlyOnCandleClose` non verifica close effettivo

**File:** [backend/app/engine/position_management.py:194-201](backend/app/engine/position_management.py#L194-L201)

Il check è solo "ho già aggiornato in questa `open_time`?". Non verifica che la candela sia effettivamente chiusa (`open_time + timeframe <= now()`). Se il worker tick arriva mentre la candela è ancora in formazione, il trailing aggiorna su dati parziali.

**Impatto:** trailing stop spostato troppo presto, valore ATR/EMA calcolato su candela in formazione e quindi instabile, rischio di trigger SL su movimenti intra-candela poi rientrati.

**Fix:** aggiungere check di close usando `TIMEFRAME_MS` da `candle_sync`:

```python
if trailing.get("updateOnlyOnCandleClose", True):
    expected_close = candle.open_time + timedelta(milliseconds=TIMEFRAME_MS[candle.timeframe])
    if datetime.now(timezone.utc) < expected_close:
        return None
```

### F-09. Signal Engine: `min_risk_reward` config ignorata (forzata a >= 2.0)

**File:** [backend/app/engine/signal_engine.py:566](backend/app/engine/signal_engine.py#L566)

```python
min_risk_reward = max(min_risk_reward, 2.0)
```

L'utente può configurare `min_risk_reward=1.5` nel DB, ma poco prima del build del risk plan il valore è forzato a `>= 2.0`. La config DB viene silenziosamente sovrascritta.

**Impatto:** strategie short-term con R:R 1.5 vengono sempre scartate; il calibration può abbassare `min_risk_reward` ma non sotto 2.0 a causa di questa linea.

**Fix:** rimuovere la riga. Il floor è già imposto da `min_risk_reward_floor` nel calibration ([calibration.py:421](backend/app/engine/calibration.py#L421)).

### F-10. Auto-tuner: `_positions_after` con `activated_at=None` ritorna tutte le posizioni

**File:** [backend/app/engine/auto_tuner.py:363-374](backend/app/engine/auto_tuner.py#L363-L374)

```python
def _positions_after(self, positions, activated_at_iso):
    if not activated_at_iso:
        return positions  # ← bug
```

Se per qualsiasi motivo `activated_at` viene perso (es. crash dopo `_start_step` ma prima di flush), `_evaluate_and_advance` confronta `metrics_before` con metriche calcolate su tutte le posizioni, incluse quelle pre-attivazione → metric corrotta, step "success" falso.

**Fix:** ritornare `[]` se `activated_at_iso` è None, e segnalare con `DecisionLog` "auto_tuner_state_corrupt".

---

## MEDI — Robustezza e qualità segnali

### F-11. BTC Macro Protection: soglie troppo aggressive su 5m × 6 candele

**File:** [backend/app/engine/btc_macro_protection.py:174-200](backend/app/engine/btc_macro_protection.py#L174-L200)

Default: `delta = 1.5% in 30 minuti` con `volume_multiplier = 1.2`. Su BTC questi movimenti sono frequenti — soprattutto in regime volatile produce diversi falsi reversal al giorno. Manca confluence con trend di lungo periodo (EMA200 4h).

**Proposta:**
- Aggiungere confluence con EMA200 sul timeframe `1h` o `4h`: il reversal è valido solo se la candela attuale chiude **dalla parte opposta** rispetto alla candela di inizio finestra rispetto a EMA200.
- Richiedere conferma su 2 candele consecutive sopra/sotto threshold.
- Aumentare il default `reversal_pct_threshold` a 2.0% per ridurre falsi positivi.

### F-12. BTC Macro Protection: `unrealized_pnl` può essere stale

**File:** [backend/app/engine/btc_macro_protection.py:216-217](backend/app/engine/btc_macro_protection.py#L216-L217)

`position.unrealized_pnl` viene rinfrescato in `refresh_open_position_pnls` (worker tick), ma BTC protection viene chiamato **dopo** `reconcile_live_positions` e `refresh_open_position_pnls` — quindi in teoria è fresco. Tuttavia il refresh potrebbe fallire silenziosamente (eccezione transient gestita), lasciando il valore vecchio. La decisione "in_profit vs in_loss" può essere errata.

**Fix:** ricalcolare `unrealized_pnl` in-line:

```python
last_price = self._latest_market_price(db, position.market)
unrealized = compute_unrealized(position, last_price)
```

### F-13. Scoring: magic number non configurabili

**File:** [backend/app/engine/scoring.py:17,78,81,100-108](backend/app/engine/scoring.py#L17)

- `_MIN_PARTICIPATION_RATIO = 0.35` (riga 17): configurabile via `risk.participation_ratio`, OK.
- Soglia `STRONG = 75` (riga 78): hardcoded.
- Soglie label `40/60/75/90` (righe 100-108): hardcoded.

**Fix:** spostare in `app_settings.scoring`:

```json
{
  "strong_score_threshold": 75,
  "label_thresholds": {"debole": 40, "medio": 60, "buono": 75, "forte": 90}
}
```

### F-14. Regime filter: filtro binario senza isteresi

**File:** [backend/app/engine/regime.py:44-49](backend/app/engine/regime.py#L44-L49)

```python
if regime == "ranging" and name in TREND_FOLLOWING_STRATEGIES:
    filtered[name] = 0
```

ADX su singolo close-of-candle: oscillazioni attorno alla soglia (es. 24 → 26 → 24) causano flip continui dei trend-followers ON/OFF. Inoltre il filtro è binario: o azzera completamente, o lascia invariato. Manca uno stato di "transizione" con peso ridotto.

**Proposte:**
- Isteresi: per attivare "trending" servono N candele consecutive sopra `adx_trending`; per uscire servono N candele sotto `adx_ranging`. N configurabile (default 2-3).
- Soft filter: invece di `peso → 0`, applicare `peso → peso × 0.5` per le strategie "out of regime".

### F-15. Auto-tuner: `improvement_threshold=0.0` causa promozione automatica

**File:** [backend/app/engine/auto_tuner.py:43,172](backend/app/engine/auto_tuner.py#L43)

```python
"improvement_threshold": 0.0,
# ...
improved = slope_after >= slope_before + threshold
```

Con threshold zero, basta che lo slope **non peggiori** per promuovere lo step. Su 50 trade casuali, `slope_before ≈ slope_after` con margine di rumore: l'A/B è virtualmente sempre "success", il che vanifica la funzione di test.

**Fix:** soglia adattiva. Calcolare `std_pnl` sui trade di confronto e usare `threshold = 0.5 * std_pnl`. Esporre comunque `improvement_threshold` come override nel config.

### F-16. Auto-tuner: `metrics_before` calcolato sulle prime 50 posizioni

**File:** [backend/app/engine/auto_tuner.py:119-120](backend/app/engine/auto_tuner.py#L119-L120)

```python
positions = self.analyzer.closed_paper_positions(db, limit=2000)
metrics_before = self._metrics_for(positions[: int(config.get("min_trades_per_step", 50))])
```

L'ordine dipende da `closed_paper_positions`: serve verificare la query. Se ritorna in ordine `closed_at DESC`, le prime 50 sono le **più recenti** (corretto come baseline). Se invece è `closed_at ASC`, sono le più vecchie (errato).

**Azione:** controllare e annotare nel commit; se errato, usare `positions[-min_trades:]` o ordinare esplicitamente.

### F-17. Auto-tuner: `_positions_after` confronta `closed_at > cutoff` ma `cutoff` è timezone-naive

**File:** [backend/app/engine/auto_tuner.py:367-374](backend/app/engine/auto_tuner.py#L367-L374)

```python
cutoff = datetime.fromisoformat(activated_at_iso)
```

`activated_at` viene salvato con `datetime.now(timezone.utc).isoformat()` (riga 123) — produce stringa con timezone. `fromisoformat` la parsa correttamente. Tuttavia `position.closed_at` può essere timezone-naive in alcuni driver DB: confronto può sollevare `TypeError`.

**Fix:** normalizzare entrambi a UTC-aware prima del confronto.

### F-18. Worker: `_consecutive_zero_signal_cycles_from_log` con limit hardcoded 120

**File:** [backend/app/worker/main.py:138-148](backend/app/worker/main.py#L138-L148)

```python
.limit(120)
```

Se ci sono >120 cicli consecutivi a zero, lo streak è cappato a 120. Inoltre il `limit` è duplicato anche in [calibration.py](backend/app/engine/calibration.py) con valore proprio — possibili disallineamenti.

**Fix:** rendere il limit configurabile (`starvation_lookback_cycles`, default 120) e condividerlo via helper.

### F-19. Signal Engine: `min_stop_distance_pct = 0.020` (2%) hardcoded come override

**File:** [backend/app/engine/signal_engine.py:285](backend/app/engine/signal_engine.py#L285)

Default 0.020 = 2%. Il subagent ha riportato 0.2% — è invece 2%. Questo è il minimo distance % tra entry e stop. Per asset volatili può essere ok; per asset calmi (es. stable, large-cap) è probabilmente troppo largo e impedisce SL stretti.

**Azione:** verificare nel codice di `RiskManager.build_plan` come viene applicato. Se è una soglia rigida, considerare di renderla adattiva per ATR-percentile del market.

### F-20. TP plan: TP3 può andare negativo o sotto SL

**File:** [backend/app/engine/execution.py:980-999](backend/app/engine/execution.py#L980-L999)

Per short con R molto stretto, `entry - 3 * risk` può scendere sotto 0 (specialmente su asset low-cap). Codice già skippa correttamente (`if price <= 0`), ma silenziosamente — la posizione perde un livello di gestione utile.

**Proposta:** rilevare TP3 negativo a monte e ridimensionare automaticamente i livelli (es. 1R / 2R / 2.5R invece di 1R / 2R / 3R). Aggiungere `DecisionLog` `take_profit_rescaled` con motivo.

### F-21. Funding rate: doppia natura (strategia interna + filtro)

**File:** [backend/app/engine/signal_engine.py:119-152,388-392](backend/app/engine/signal_engine.py#L119-L152)

Il funding rate viene trattato come una "strategia" che contribuisce allo scoring (vota short se positivo, long se negativo). Non viene **anche** usato come filtro di blocco diretto (anche se ci sono pesi configurabili). Tuttavia, sopra soglia 0.05%/8h (config) potrebbe avere senso un hard-block diretto del long. Attualmente il sistema si affida solo allo scoring.

**Proposte:**
- Mantenere lo scoring contribution attuale.
- Aggiungere un filtro hard opzionale `funding_block_threshold_pct` (default 0.05%): se funding eccede, bloccare hard la direzione corrispondente.

### F-22. Strategy regime: nessun ruolo per VWAP, supertrend, volume_spike

**File:** [backend/app/engine/regime.py:8-9](backend/app/engine/regime.py#L8-L9)

```python
TREND_FOLLOWING_STRATEGIES = {"ema_crossover", "macd", "supertrend"}
MEAN_REVERTING_STRATEGIES = {"rsi", "bollinger_bands"}
```

Strategie come `vwap`, `volume_spike`, `fibonacci`, `support_resistance`, `candlestick_pattern` non sono classificate: il regime filter le lascia passare a peso pieno in ogni regime. È un design choice (non tutte sono mean-revert o trend), ma andrebbe documentato. Alcune (es. `supertrend` è nelle trend-following ma è già regime-aware internamente) possono essere ridondanti.

**Proposta:** classificazione esplicita nel registry strategie con campo `regime_role: {trend, mean_revert, neutral}`.

### F-23. Risk plan: `min_atr_multiplier=1.5` hardcoded fallback

**File:** [backend/app/engine/signal_engine.py:284,305-307](backend/app/engine/signal_engine.py#L284)

Default 1.5 viene applicato se la chiave manca dal DB. È un valore aggressivo per timeframe brevi (5m, 15m). Su asset volatili e timeframe corti, una distanza SL = 1.5 × ATR può tradurre in stop sotto-soglia.

**Azione:** considerare default 2.0 per timeframe ≤ 15m, 1.5 per timeframe ≥ 1h.

### F-24. `_persist_indicators` viene chiamato anche per segnali poi scartati

**File:** [backend/app/engine/signal_engine.py:405](backend/app/engine/signal_engine.py#L405)

`_persist_indicators` è chiamato **prima** dei filtri downstream (low score, low rr, whale contra, mtf, ...). Quindi anche per segnali scartati si paga la scrittura DB di N indicator rows × N strategie × N timeframe × N market. Su 100 market × 4 timeframe × 10 strategie = 4000 row scritte per tick anche se nessun segnale generato.

**Fix:** spostare `_persist_indicators` dopo l'accettazione del segnale, oppure renderlo configurabile (`risk.persist_indicators_always: bool`, default false).

### F-25. Hyperliquid: nessun backoff esponenziale su 429

**File:** [backend/app/services/hyperliquid.py](backend/app/services/hyperliquid.py)

Circuit breaker presente per errori transient, ma nessun backoff progressivo su 429 (rate limit). Sotto carico (es. 200 market + multiple timeframes), il worker può perdere ordini.

**Fix:** introdurre `tenacity` con `wait_exponential(min=1, max=30)` su `HyperliquidTransientError`.

---

## MINORI — Performance e osservabilità

### F-26. Worker: query candle ripetute per stesso market+timeframe

**File:** [backend/app/worker/main.py:480-490](backend/app/worker/main.py#L480-L490)

Ogni timeframe ricarica candele per: `close_paper_positions_for_timeframe`, `manage_live_positions_for_timeframe`, `signal_engine.generate`. Stesse candele lette 3 volte per tick. Un cache in-memory per-tick ridurrebbe le query del ~70%.

### F-27. BTC Macro Protection: 20 candele extra caricate ma mai usate

**File:** [backend/app/engine/btc_macro_protection.py:62](backend/app/engine/btc_macro_protection.py#L62)

```python
.limit(lookback + 20)
```

I 20 candele extra non sono usate. Sembra legacy. Limite corretto è `lookback + 1` (per old_close + new_close).

### F-28. Heartbeat 30s intra-tick già presente

**File:** [backend/app/worker/main.py:446-452](backend/app/worker/main.py#L446-L452)

**Correzione subagent precedente:** il subagent aveva flaggato l'assenza di heartbeat intra-tick. In realtà è già implementato: durante il loop sui market, ogni 30s viene chiamato `_record_heartbeat("running")`. Non è un bug — il subagent ha sbagliato.

### F-29. `_persist_indicators` non distingue successi/fallimenti

**File:** [backend/app/engine/signal_engine.py:222-249](backend/app/engine/signal_engine.py#L222-L249)

Cancella e re-inserisce indicator rows per la stessa key. Se la transazione fallisce dopo il delete ma prima dell'insert, gli indicator vengono persi. Possibile fonte di lacune nelle visualizzazioni grafiche.

**Fix:** usare upsert (`ON CONFLICT DO UPDATE`) invece di delete+insert.

### F-30. Auto-tuner: progress non considera reset di stato

**File:** [backend/app/engine/auto_tuner.py:331-334](backend/app/engine/auto_tuner.py#L331-L334)

Se l'utente fa `rollback_current` mentre uno step è in corso, `step_history` viene aggiornato e `current_step_index` resetato. Tuttavia il prossimo `_start_step` riparte dall'inizio della sequence con il primo step non in `completed_keys` — corretto. Ma se l'utente fa `reset()` e poi vuole skip uno step già fatto, non c'è un meccanismo.

**Proposta:** comando `skip_step(key)` che aggiunge un'entry con status="skipped" senza eseguire.

### F-31. Stuck stop order detection: assente

**File:** [backend/app/engine/position_management.py](backend/app/engine/position_management.py)

Quando `replace_live_stop_order` fallisce per errore exchange, l'errore è loggato in `PositionStopUpdate` ma non c'è retry automatico né alert. Una posizione può rimanere con SL stale per ore se l'errore è persistente.

**Fix:** aggiungere field `replace_attempts` su `PositionStopUpdate`; se >= 3, emettere alert Telegram `stuck_stop_order`.

### F-32. `worker_cycle_failed` non distinto da `worker_cycle` (gen=0)

**File:** [backend/app/worker/main.py:645-657](backend/app/worker/main.py#L645-L657)

`_record_worker_failure` logga `worker_cycle_failed` ma `_consecutive_zero_signal_cycles_from_log` considera solo `event_type == "worker_cycle"` (con generated=0). Un crash del cycle non incrementa lo streak di starvation. È un side-effect strano: se il worker crasha, lo starvation override non si attiva mai.

**Fix:** trattare anche `worker_cycle_failed` come "zero signals" nel calcolo dello streak.

### F-33. `signal_engine.generate` esegue `db.commit()` multipli per segnale scartato

**File:** [backend/app/engine/signal_engine.py:334,422,513,525,564,584](backend/app/engine/signal_engine.py#L334)

Ogni filtro downstream fa `db.commit()` prima di ritornare None. Ok per liberare le transazioni, ma per market × timeframe × cycle = molti commit "vuoti". Performance impact su DB postgres.

**Proposta:** unificare il commit a fine generate() con un try/finally; non committare per ogni filtro.

### F-34. UI: stato calibrazione non espone `consecutive_zero_signal_cycles`

**File:** `frontend/components/screens/` (CalibrationScreen)

Lo stato corrente (deep defensive, starvation streak, eq slope) non è visualizzato in modo coerente in UI. L'utente non ha visibilità sul motivo per cui non vengono generati segnali.

**Proposta:** aggiungere widget "Diagnostic" nel CalibrationScreen con: stato attuale, motivo, eq_slope, zero_cycles, prossimo cambio previsto.

### F-35. Dry-run mode mancante

**File:** N/A (gap progettuale)

Esiste solo `signal/paper/live`. Per testare nuove strategie senza inquinare le statistiche del paper trading, manca una modalità `replay` o `backtest`.

**Proposta (low-priority, lungo termine):** mode `replay` che riproduce candele storiche su un set di market simulato.

---

## Miglioramenti strategici (non bug)

### M-01. BTC Macro Protection: estendere a SOL/ETH come "market drivers"

L'engine si applica solo a BTC. Tuttavia ETH e SOL guidano la rotazione altcoin. Considerare engine paralleli `EthMacroProtection`, `SolMacroProtection` con pesi configurabili.

### M-02. Aggiungere score "fitness" alla calibrazione

Oggi la calibrazione tara solo via win_rate/profit_factor/realized_pnl. Aggiungere `expectancy` (avg_win × win_rate - avg_loss × loss_rate), `max_drawdown_pct`, `recovery_factor`. Permette di distinguere strategie "fortunate" (alto pnl, alto drawdown) da quelle realmente robuste.

### M-03. Trailing stop: aggiungere modalità "parabolic SAR"

Algoritmo trailing già supporta atr/chandelier/swing/ema. Parabolic SAR sarebbe utile per trend forti con accelerazione (riduce slack rispetto a ATR).

### M-04. Indicator engine: passare a TA-Lib o vectorbt

Gli indicatori sono implementazioni custom in Python puro. Una migrazione a TA-Lib (C) ridurrebbe il tempo di calcolo per tick del 40-60%.

### M-05. Auto-tuner: A/B test con bandit invece di sequence

Sequence lineare con 4 step. Multi-armed bandit (es. Thompson sampling) permetterebbe di esplorare combinazioni di flag invece di sequenze fisse.

---

## Prioritizzazione

| Priorità | Finding | Motivazione |
| --- | --- | --- |
| P0 (oggi) | F-01, F-02, F-05, F-09 | Spiegano direttamente lo stato corrente "0 segnali" |
| P0 (oggi) | F-03, F-04, F-07, F-08 | Bug di correttezza, fix piccoli e a basso rischio |
| P1 (1-2gg) | F-10, F-11, F-12, F-14, F-15, F-25 | Robustezza |
| P1 (1-2gg) | F-13, F-23, F-24 | Magic numbers configurabili |
| P2 (settimana) | F-16/17/18/19/20/21/22 | Refinement |
| P2 (settimana) | F-26 → F-35 | Performance/osservabilità |
| Backlog | M-01 → M-05 | Strategici |

---

## Verification plan

1. **Test suite:** `cd backend && pytest -x -v` deve passare 100%.
2. **Unit test mirati** (aggiunti in `backend/tests/engine/`):
   - `test_btc_macro_protection_reversal_through_neutral.py` (F-01)
   - `test_btc_macro_protection_rollback_state.py` (F-02)
   - `test_scoring_parity_and_contribution.py` (F-03, F-04)
   - `test_calibration_deep_defensive_escape.py` (F-05)
   - `test_position_management_div_by_zero.py` (F-07)
   - `test_signal_engine_min_rr_respects_config.py` (F-09)
   - `test_auto_tuner_state_corrupt.py` (F-10)
3. **Dry-run worker 10 minuti in `SIGNAL_ONLY`:**
   - Verificare assenza di `auto_calibration_failed` / `btc_macro_protection_failed`.
   - Verificare `decision_logs.worker_cycle` con `generated > 0`.
   - Verificare che `auto_calibration_state.deep_defensive_active` non resti True senza motivo.
4. **Snapshot pre/post:** `app_settings`, ultimi 50 `signals`, ultime 100 `decision_logs` salvati in `/tmp/audit-pre.json` e `/tmp/audit-post.json` per confronto.
5. **Lint/type check:** `ruff check backend/ && mypy backend/app/` (se configurato).
