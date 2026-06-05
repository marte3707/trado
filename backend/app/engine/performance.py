from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload

from app.core.enums import OrderMode
from app.db.models import Position, Signal


def _wilson_bounds(wins: int, trades: int, z: float = 1.96) -> tuple[float, float]:
    if trades <= 0:
        return 0.0, 0.0
    p_hat = wins / trades
    z_sq = z * z
    denom = 1.0 + z_sq / trades
    center = (p_hat + z_sq / (2 * trades)) / denom
    margin = z * math.sqrt((p_hat * (1 - p_hat) + z_sq / (4 * trades)) / trades) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


@dataclass(frozen=True)
class PerformanceStats:
    trades: int
    wins: int
    losses: int
    win_rate: float
    realized_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: float | None


class SignalPerformanceAnalyzer:
    def __init__(self) -> None:
        # Cache per-cycle opt-in: attivata da `begin_cycle()` (chiamato dal
        # SignalEngine all'inizio di ogni worker tick). Gli aggregati *global* e
        # *per-timeframe* sono identici per tutti i market dello stesso timeframe
        # nello stesso tick: senza cache venivano ricalcolati (3 query da 500
        # posizioni) per ogni candidato. Le istanze usa-e-getta (API, calibration)
        # non chiamano begin_cycle → `_cycle_active=False` → comportamento invariato.
        self._cycle_active: bool = False
        self._cycle_global_stats: dict[str, PerformanceStats] | None = None
        self._cycle_timeframe_stats: dict[str, dict[str, PerformanceStats]] = {}
        self._cycle_market_closed_count: dict[int, int] = {}

    def begin_cycle(self) -> None:
        """Abilita e invalida la cache per-cycle all'inizio di ogni worker tick.

        La leggera staleness intra-tick (posizioni chiuse durante lo stesso tick non
        rientrano negli aggregati) è accettabile: il performance adjustment è
        statistico e le chiusure per tick sono poche rispetto al lookback di 500.
        """
        self._cycle_active = True
        self._cycle_global_stats = None
        self._cycle_timeframe_stats = {}
        self._cycle_market_closed_count = {}

    def _global_stats_cached(self, db: Session) -> dict[str, PerformanceStats]:
        if not self._cycle_active:
            return self.strategy_stats(db)
        if self._cycle_global_stats is None:
            self._cycle_global_stats = self.strategy_stats(db)
        return self._cycle_global_stats

    def _timeframe_stats_cached(self, db: Session, timeframe: str) -> dict[str, PerformanceStats]:
        if not self._cycle_active:
            return self.strategy_stats(db, timeframe=timeframe)
        cached = self._cycle_timeframe_stats.get(timeframe)
        if cached is None:
            cached = self.strategy_stats(db, timeframe=timeframe)
            self._cycle_timeframe_stats[timeframe] = cached
        return cached

    def _market_closed_count(self, db: Session, market_id: int) -> int:
        """Conteggio posizioni paper chiuse per market (cached per-cycle).

        Necessario per saltare lo `scoped_stats`: lo scope market_timeframe richiede
        ≥ `min_trades` posizioni *per strategia*, quindi se il market ha meno di
        `min_trades` posizioni chiuse in totale nessuna strategia può qualificarsi
        e l'intera query scoped (la più costosa) è lavoro sprecato.
        """
        if self._cycle_active and market_id in self._cycle_market_closed_count:
            return self._cycle_market_closed_count[market_id]
        count = (
            db.query(Position)
            .filter(
                Position.mode == OrderMode.PAPER.value,
                Position.status == "closed",
                Position.realized_pnl.is_not(None),
                Position.market_id == market_id,
            )
            .count()
        )
        if self._cycle_active:
            self._cycle_market_closed_count[market_id] = count
        return count

    def closed_paper_positions(
        self,
        db: Session,
        timeframe: str | None = None,
        market_id: int | None = None,
        limit: int = 500,
    ) -> list[Position]:
        query = (
            db.query(Position)
            .options(joinedload(Position.market))
            .filter(
                Position.mode == OrderMode.PAPER.value,
                Position.status == "closed",
                Position.realized_pnl.is_not(None),
            )
        )
        if market_id is not None:
            query = query.filter(Position.market_id == market_id)
        query = query.order_by(Position.closed_at.desc()).limit(limit)
        rows = query.all()
        if timeframe is None:
            return rows
        return [
            row
            for row in rows
            if isinstance(row.metadata_json, dict)
            and row.metadata_json.get("source_timeframe") == timeframe
        ]

    def summarize_positions(self, positions: list[Position]) -> PerformanceStats:
        pnl_values = [Decimal(position.realized_pnl or 0) for position in positions]
        wins = sum(1 for pnl in pnl_values if pnl > 0)
        losses = sum(1 for pnl in pnl_values if pnl <= 0)
        gross_profit = sum((pnl for pnl in pnl_values if pnl > 0), Decimal("0"))
        gross_loss = abs(sum((pnl for pnl in pnl_values if pnl <= 0), Decimal("0")))
        profit_factor = float(gross_profit / gross_loss) if gross_loss else None
        realized_pnl = sum(pnl_values, Decimal("0"))
        trades = len(pnl_values)
        return PerformanceStats(
            trades=trades,
            wins=wins,
            losses=losses,
            win_rate=(wins / trades) if trades else 0.0,
            realized_pnl=float(realized_pnl),
            gross_profit=float(gross_profit),
            gross_loss=float(gross_loss),
            profit_factor=profit_factor,
        )

    def strategy_positions(
        self,
        db: Session,
        timeframe: str | None = None,
        market_id: int | None = None,
        limit: int = 500,
    ) -> dict[str, list[Position]]:
        """Posizioni chiuse raggruppate per strategia che ha contribuito al segnale.

        Why expose this: la holdout validation in `AutoCalibrationEngine` ha bisogno
        delle posizioni *individuali* per fare lo split train/test temporale. Ricavare
        gli stats da `summarize_positions(...)` mantiene la composizione semplice.
        Ritorna le posizioni nello stesso ordine di `closed_paper_positions` (closed_at desc).
        """
        positions = self.closed_paper_positions(db, timeframe, market_id, limit)
        signal_ids = {
            int(position.metadata_json["source_signal_id"])
            for position in positions
            if isinstance(position.metadata_json, dict)
            and position.metadata_json.get("source_signal_id") is not None
        }
        signals = {
            signal.id: signal
            for signal in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()
        } if signal_ids else {}
        by_strategy: dict[str, list[Position]] = {}
        for position in positions:
            metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
            signal_id_raw = metadata.get("source_signal_id")
            if signal_id_raw is None:
                continue
            try:
                signal_id = int(signal_id_raw)
                signal = signals.get(signal_id)
            except (TypeError, ValueError):
                signal = None
            if signal is None:
                continue
            for strategy_name, breakdown in (signal.strategy_breakdown or {}).items():
                if strategy_name.startswith("_") or not isinstance(breakdown, dict):
                    continue
                if breakdown.get("direction") == "NEUTRAL" or breakdown.get("contribution", 0) <= 0:
                    continue
                by_strategy.setdefault(strategy_name, []).append(position)
        return by_strategy

    def strategy_stats(
        self,
        db: Session,
        timeframe: str | None = None,
        market_id: int | None = None,
        limit: int = 500,
    ) -> dict[str, PerformanceStats]:
        return {
            name: self.summarize_positions(positions)
            for name, positions in self.strategy_positions(
                db, timeframe=timeframe, market_id=market_id, limit=limit
            ).items()
        }

    # ------------------------------------------------------------------
    # Fix 3 — contribution-weighted attribution
    # ------------------------------------------------------------------

    def strategy_contribution_fractions(
        self,
        db: Session,
        timeframe: str | None = None,
        limit: int = 500,
    ) -> dict[str, list[tuple[float, float]]]:
        """Per ogni strategia: lista di (realized_pnl, contribution_fraction).

        contribution_fraction = contribution[strategy] / voting_weight del segnale.
        Permette di pesare vittorie/perdite in base a quanto quella strategia ha guidato
        il segnale invece di creditar la egualmente a tutte le strategie presenti.
        """
        positions = self.closed_paper_positions(db, timeframe=timeframe, limit=limit)
        signal_ids = {
            int(p.metadata_json["source_signal_id"])
            for p in positions
            if isinstance(p.metadata_json, dict) and p.metadata_json.get("source_signal_id") is not None
        }
        signals: dict[int, Signal] = (
            {s.id: s for s in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
            if signal_ids else {}
        )
        result: dict[str, list[tuple[float, float]]] = {}
        for position in positions:
            if position.realized_pnl is None:
                continue
            metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
            signal_id_raw = metadata.get("source_signal_id")
            if signal_id_raw is None:
                continue
            try:
                signal = signals.get(int(signal_id_raw))
            except (TypeError, ValueError):
                signal = None
            if signal is None:
                continue
            breakdown = signal.strategy_breakdown or {}
            score_meta = breakdown.get("_score")
            voting_weight = (
                float(score_meta.get("voting_weight", 0))
                if isinstance(score_meta, dict) else 0.0
            )
            pnl = float(position.realized_pnl)
            for name, strat_data in breakdown.items():
                if name.startswith("_") or not isinstance(strat_data, dict):
                    continue
                if strat_data.get("direction") == "NEUTRAL" or strat_data.get("contribution", 0) <= 0:
                    continue
                contrib = float(strat_data.get("contribution", 0))
                fraction = contrib / voting_weight if voting_weight > 0 else 0.0
                result.setdefault(name, []).append((pnl, fraction))
        return result

    def strategy_weighted_stats(
        self,
        db: Session,
        timeframe: str | None = None,
        limit: int = 500,
    ) -> dict[str, PerformanceStats]:
        """Stats per strategia pesate per la frazione di contributo al segnale.

        Corregge il bias dell'attribuzione uniforme: se una strategia ha contribuito
        il 20% del segnale, ottiene credito 0.20 per quella vittoria/perdita invece di 1.
        """
        fractions_map = self.strategy_contribution_fractions(db, timeframe=timeframe, limit=limit)
        result: dict[str, PerformanceStats] = {}
        for name, entries in fractions_map.items():
            if not entries:
                continue
            eff_trades = sum(f for _, f in entries)
            if eff_trades <= 0:
                continue
            eff_wins = sum(f for pnl, f in entries if pnl > 0)
            gross_profit = sum(pnl * f for pnl, f in entries if pnl > 0)
            gross_loss = abs(sum(pnl * f for pnl, f in entries if pnl <= 0))
            realized_pnl = sum(pnl * f for pnl, f in entries)
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
            result[name] = PerformanceStats(
                trades=int(round(eff_trades)),
                wins=int(round(eff_wins)),
                losses=int(round(eff_trades - eff_wins)),
                win_rate=eff_wins / eff_trades,
                realized_pnl=realized_pnl,
                gross_profit=gross_profit,
                gross_loss=gross_loss,
                profit_factor=profit_factor,
            )
        return result

    # ------------------------------------------------------------------
    # Fix 4 — regime-conditioned performance tracking
    # ------------------------------------------------------------------

    def positions_by_regime(
        self,
        db: Session,
        timeframe: str | None = None,
        limit: int = 500,
    ) -> dict[str, list[Position]]:
        """Posizioni chiuse raggruppate per regime al momento dell'apertura."""
        positions = self.closed_paper_positions(db, timeframe=timeframe, limit=limit)
        result: dict[str, list[Position]] = {}
        for p in positions:
            metadata = p.metadata_json if isinstance(p.metadata_json, dict) else {}
            regime = metadata.get("regime_at_open")
            if regime:
                result.setdefault(str(regime), []).append(p)
        return result

    def recent_regime(self, db: Session, window: int = 40) -> str | None:
        """Regime dominante nelle ultime `window` posizioni con regime tracciato."""
        positions = self.closed_paper_positions(db, limit=window * 3)
        counts: dict[str, int] = {}
        seen = 0
        for p in positions:
            metadata = p.metadata_json if isinstance(p.metadata_json, dict) else {}
            regime = metadata.get("regime_at_open")
            if regime:
                counts[str(regime)] = counts.get(str(regime), 0) + 1
                seen += 1
                if seen >= window:
                    break
        return max(counts, key=lambda r: counts[r]) if counts else None

    def regime_strategy_positions(
        self,
        db: Session,
        regime: str,
        timeframe: str | None = None,
        limit: int = 500,
    ) -> dict[str, list[Position]]:
        """strategy_positions() filtrato per regime specifico."""
        all_by_regime = self.positions_by_regime(db, timeframe=timeframe, limit=limit)
        regime_positions = all_by_regime.get(regime, [])
        if not regime_positions:
            return {}
        # Ricostruisce la stessa struttura di strategy_positions() per i soli trade di quel regime
        signal_ids = {
            int(p.metadata_json["source_signal_id"])
            for p in regime_positions
            if isinstance(p.metadata_json, dict) and p.metadata_json.get("source_signal_id") is not None
        }
        signals: dict[int, Signal] = (
            {s.id: s for s in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
            if signal_ids else {}
        )
        by_strategy: dict[str, list[Position]] = {}
        for position in regime_positions:
            metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
            signal_id_raw = metadata.get("source_signal_id")
            if signal_id_raw is None:
                continue
            try:
                signal = signals.get(int(signal_id_raw))
            except (TypeError, ValueError):
                signal = None
            if signal is None:
                continue
            for name, bd in (signal.strategy_breakdown or {}).items():
                if name.startswith("_") or not isinstance(bd, dict):
                    continue
                if bd.get("direction") == "NEUTRAL" or bd.get("contribution", 0) <= 0:
                    continue
                by_strategy.setdefault(name, []).append(position)
        return by_strategy

    @staticmethod
    def holdout_split_stats(
        positions: list[Position],
        train_ratio: float = 0.7,
        min_samples_per_side: int = 5,
    ) -> tuple[PerformanceStats, PerformanceStats] | None:
        """Split temporale train/test per holdout validation.

        Why: la calibration usa le stesse posizioni che ha contribuito a generare per
        decidere i factor futuri (look-ahead bias). Lo split temporale crea un naive
        out-of-sample check: la più vecchia frazione `train_ratio` è il "training",
        la più recente è il "test". Se la decisione regge su entrambi → segnale è robusto.
        Se diverge → probabile overfitting al rumore.

        Le posizioni in input sono attese in ordine *desc* per closed_at (come ritorna
        `closed_paper_positions`). Ritorna `None` se non ci sono abbastanza campioni
        per uno split significativo.
        """
        if not positions:
            return None
        n = len(positions)
        cutoff = int(n * train_ratio)
        # input desc → ribaltiamo per avere asc temporale
        chronological = list(reversed(positions))
        train = chronological[:cutoff]
        test = chronological[cutoff:]
        if len(train) < min_samples_per_side or len(test) < min_samples_per_side:
            return None
        analyzer = SignalPerformanceAnalyzer()
        return analyzer.summarize_positions(train), analyzer.summarize_positions(test)

    @staticmethod
    def time_weighted_summarize(
        positions: list[Position],
        half_life: int = 50,
    ) -> PerformanceStats:
        """PerformanceStats con peso esponenzialmente decrescente per posizioni vecchie.

        Le posizioni più recenti (in fondo all'ordinamento desc) contano di più. Utile
        quando il regime di mercato è cambiato: gli stats "puri" su lookback 500 trade
        possono essere dominati da un periodo non più rappresentativo.

        Le posizioni in input sono attese in ordine *desc* per closed_at. `half_life`
        è espresso in numero di trade: dopo `half_life` trade dal più recente il peso
        è 0.5; dopo 2× è 0.25; ecc.

        I conteggi `trades/wins/losses` restano interi (per compatibilità) ma riflettono
        la somma ponderata arrotondata; PnL/profit_factor sono floats pesati.
        """
        if not positions:
            return PerformanceStats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, None)
        # decay: position[0] è la più recente (desc), peso 1.0 → posizioni più vecchie scendono
        decay = math.log(2.0) / max(1, half_life)
        weighted_wins = 0.0
        weighted_losses = 0.0
        weighted_gross_profit = 0.0
        weighted_gross_loss = 0.0
        weighted_pnl = 0.0
        weighted_count = 0.0
        for index, position in enumerate(positions):
            if position.realized_pnl is None:
                continue
            weight = math.exp(-decay * index)
            pnl = float(position.realized_pnl)
            weighted_pnl += pnl * weight
            weighted_count += weight
            if pnl > 0:
                weighted_wins += weight
                weighted_gross_profit += pnl * weight
            else:
                weighted_losses += weight
                weighted_gross_loss += abs(pnl) * weight
        if weighted_count == 0:
            return PerformanceStats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, None)
        win_rate = weighted_wins / weighted_count
        profit_factor = (
            weighted_gross_profit / weighted_gross_loss if weighted_gross_loss > 0 else None
        )
        # Per i conteggi interi: scala mantenendo le proporzioni rispetto al campione reale
        n = len(positions)
        scale = n / weighted_count if weighted_count else 0.0
        return PerformanceStats(
            trades=n,
            wins=int(round(weighted_wins * scale)),
            losses=int(round(weighted_losses * scale)),
            win_rate=win_rate,
            realized_pnl=weighted_pnl,
            gross_profit=weighted_gross_profit,
            gross_loss=weighted_gross_loss,
            profit_factor=profit_factor,
        )

    def adjusted_weights(
        self,
        db: Session,
        weights: dict[str, int],
        timeframe: str,
        market_id: int,
        min_trades: int = 20,
    ) -> tuple[dict[str, int], dict[str, dict]]:
        # Scoped (market_timeframe) calcolato solo se il market ha abbastanza
        # posizioni chiuse da poter qualificare almeno una strategia (≥ min_trades).
        # In assenza, `scoped_stats` resta vuoto e la selezione scope ricade su
        # timeframe/global — identico al comportamento precedente, ma senza pagare
        # la query scoped per la stragrande maggioranza dei market a bassa attività.
        if self._market_closed_count(db, market_id) >= min_trades:
            scoped_stats = self.strategy_stats(db, timeframe=timeframe, market_id=market_id)
        else:
            scoped_stats = {}
        timeframe_stats = self._timeframe_stats_cached(db, timeframe)
        global_stats = self._global_stats_cached(db)
        adjusted = dict(weights)
        notes: dict[str, dict] = {}
        for strategy_name, base_weight in weights.items():
            scope = "market_timeframe"
            strategy_stats = scoped_stats.get(strategy_name)
            if strategy_stats is None or strategy_stats.trades < min_trades:
                scope = "timeframe"
                strategy_stats = timeframe_stats.get(strategy_name)
            if strategy_stats is None or strategy_stats.trades < min_trades:
                scope = "global"
                strategy_stats = global_stats.get(strategy_name)
            if strategy_stats is None or strategy_stats.trades < min_trades:
                continue
            # Wilson bounds del WR per ridurre il rumore campionario; le stesse soglie
            # sono usate in AutoCalibrationEngine._strategy_factor → coerenza tra i due
            # adjuster (era una fonte di inconsistenza segnalata in audit).
            wr_lb, wr_ub = _wilson_bounds(strategy_stats.wins, strategy_stats.trades)
            factor = 1.0
            if wr_ub < 0.40:
                factor = 0.55
            elif wr_ub < 0.50:
                factor = 0.75
            elif wr_lb >= 0.55 and (
                strategy_stats.profit_factor is None or strategy_stats.profit_factor >= 1.2
            ):
                factor = 1.15
            adjusted[strategy_name] = max(1, round(base_weight * factor))
            notes[strategy_name] = {
                "base_weight": base_weight,
                "adjusted_weight": adjusted[strategy_name],
                "factor": factor,
                "scope": scope,
                "trades": strategy_stats.trades,
                "win_rate": strategy_stats.win_rate,
                "wr_lower_bound": wr_lb,
                "wr_upper_bound": wr_ub,
                "realized_pnl": strategy_stats.realized_pnl,
                "profit_factor": strategy_stats.profit_factor,
            }
        return adjusted, notes


def serialize_stats(stats: PerformanceStats) -> dict:
    return {
        "trades": stats.trades,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate": stats.win_rate,
        "realized_pnl": stats.realized_pnl,
        "gross_profit": stats.gross_profit,
        "gross_loss": stats.gross_loss,
        "profit_factor": stats.profit_factor,
    }
