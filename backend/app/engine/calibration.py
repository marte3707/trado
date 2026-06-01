from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AppSetting, DecisionLog, Position, StrategyConfig
from app.engine.diagnostics import equity_slope_normalized
from app.engine.edge_profile import (
    DEFAULT_MAX_COMBINED_SCORE_ADJUSTMENT,
    compute_edge_adjustments,
)
from app.engine.performance import PerformanceStats, SignalPerformanceAnalyzer, serialize_stats


@dataclass(frozen=True)
class CalibrationChange:
    target: str
    key: str
    before: float | int | dict[str, Any] | None
    after: float | int | dict[str, Any]
    reason: str


@dataclass(frozen=True)
class CalibrationResult:
    applied: bool
    eligible: bool
    reason: str
    overall: PerformanceStats
    changes: list[CalibrationChange]
    strategy_adjustments: dict[str, dict[str, Any]]
    settings: dict[str, Any]


class AutoCalibrationEngine:
    def __init__(self, analyzer: SignalPerformanceAnalyzer | None = None) -> None:
        self.analyzer = analyzer or SignalPerformanceAnalyzer()

    def run(self, db: Session, *, force: bool = False, commit: bool = True) -> CalibrationResult:
        settings = self._settings(db)
        positions = self.analyzer.closed_paper_positions(
            db,
            limit=int(settings["lookback_trades"]),
        )
        overall = self.analyzer.summarize_positions(positions)
        if not settings["enabled"] and not force:
            if commit:
                self._record_run(
                    db,
                    force=force,
                    overall=overall,
                    changes=[],
                    event_type="auto_calibration_skipped",
                    reason="auto calibration disabled",
                    status="skipped",
                )
            return self._result(False, False, "auto calibration disabled", overall, [], {}, settings)
        if overall.trades < int(settings["min_closed_trades"]):
            if commit:
                self._record_run(
                    db,
                    force=force,
                    overall=overall,
                    changes=[],
                    event_type="auto_calibration_skipped",
                    reason="not enough closed trades",
                    status="skipped",
                )
            return self._result(
                False,
                False,
                "not enough closed trades",
                overall,
                [],
                {},
                settings,
            )

        changes: list[CalibrationChange] = []
        # Cache condivisa tra _calibrate_strategy_weights e _calibrate_strategy_params:
        # cachata `positions per strategy` permette di derivare sia stats sia holdout
        # split per la validazione walk-forward. Era un raddoppio di query al DB.
        shared_positions: dict[str, dict[str, list[Position]]] = {}
        changes.extend(
            self._calibrate_strategy_weights(
                db, settings, total_closed=overall.trades, positions_cache=shared_positions
            )
        )
        changes.extend(
            self._calibrate_strategy_params(db, settings, positions_cache=shared_positions)
        )
        # Usa la slope normalizzata per le decisioni deep-defensive: la soglia diventa
        # indipendente dalla scala dell'equity/size, evitando che configurazioni con
        # equity grandi entrino in deep defensive solo perché lo slope grezzo è alto.
        slope_value = equity_slope_normalized(positions)
        recent_window = int(settings.get("recent_window", 50))
        recent_positions = positions[:recent_window]
        recent_stats = self.analyzer.summarize_positions(recent_positions)
        recent_slope = equity_slope_normalized(recent_positions)
        changes.extend(
            self._calibrate_risk_settings(
                db,
                overall,
                settings,
                equity_slope_value=slope_value,
                recent_stats=recent_stats,
                recent_slope_value=recent_slope,
            )
        )
        changes.extend(self._calibrate_edge_adjustments(db, settings))
        strategy_changes = {
            change.key: {
                "target": change.target,
                "before": change.before,
                "after": change.after,
                "reason": change.reason,
            }
            for change in changes
            if change.target in {"strategy_weight", "strategy_params"}
        }

        if commit:
            self._record_run(
                db,
                force=force,
                overall=overall,
                changes=changes,
                event_type="auto_calibration_applied" if changes else "auto_calibration_checked",
                reason="calibration completed",
                status="applied" if changes else "checked",
            )
        return self._result(bool(changes), True, "calibration completed", overall, changes, strategy_changes, settings)

    def preview(self, db: Session) -> CalibrationResult:
        # `run(..., commit=False)` muta comunque l'ORM (strategy.weight, risk_setting.value,
        # baseline flush). Avvolgiamo in un SAVEPOINT in modo che TUTTE le scritture
        # (incluso il baseline auto-creato e l'eventuale `db.commit()` di `_record_run`
        # in caso futuro) vengano annullate quando il caller esce dal blocco.
        savepoint = db.begin_nested()
        try:
            return self.run(db, force=True, commit=False)
        finally:
            if savepoint.is_active:
                savepoint.rollback()

    def _settings(self, db: Session) -> dict[str, Any]:
        current = db.get(AppSetting, "auto_calibration")
        value = dict(current.value if current and isinstance(current.value, dict) else {})
        defaults = {
            "enabled": True,
            "lookback_trades": 500,
            "min_closed_trades": 20,
            "min_strategy_trades": 20,
            "min_weight": 1,
            "max_weight": 30,
            "min_signal_score_floor": 55,
            "min_signal_score_ceiling": 80,
            "min_risk_reward_floor": 2.0,
            "min_risk_reward_ceiling": 3.0,
            "min_risk_per_trade_pct": 0.25,
            "auto_disable_enabled": True,
            "auto_disable_min_trades": 25,
            "auto_disable_win_rate": 0.30,
            "deep_defensive_slope_threshold": -0.1,
            "starvation_soft_cycles": 10,
            "starvation_hard_cycles": 15,
            "min_trades_for_slope": 10,
            "recent_window": 50,
            "recent_slope_exit_cycles": 3,
            "deep_defensive_cooldown_runs": 6,
            "deep_defensive_cooldown_min_new_trades": 3,
        }
        return {**defaults, **value}

    def _baseline_state(self, db: Session) -> dict[str, Any]:
        state = db.get(AppSetting, "auto_calibration_baseline")
        if state is not None and isinstance(state.value, dict):
            return dict(state.value)

        settings = get_settings()
        risk_setting = db.get(AppSetting, "risk")
        risk = dict(risk_setting.value if risk_setting and isinstance(risk_setting.value, dict) else {})
        strategies = db.query(StrategyConfig).all()
        baseline = {
            "risk": {
                "min_signal_score": int(risk.get("min_signal_score", settings.min_signal_score)),
                "min_risk_reward": float(risk.get("min_risk_reward", max(settings.min_risk_reward, 2.0))),
                "max_risk_per_trade_pct": float(
                    risk.get("max_risk_per_trade_pct", settings.max_risk_per_trade_pct)
                ),
            },
            "strategy_weights": {
                f"{strategy.timeframe}:{strategy.strategy_name}": int(strategy.weight)
                for strategy in strategies
            },
            "strategy_params": {
                f"{strategy.timeframe}:{strategy.strategy_name}": dict(strategy.params or {})
                for strategy in strategies
            },
        }
        db.add(AppSetting(key="auto_calibration_baseline", value=baseline))
        db.flush()
        return baseline

    def _baseline_strategy_params(self, db: Session) -> dict[str, dict[str, Any]]:
        baseline = self._baseline_state(db)
        if isinstance(baseline.get("strategy_params"), dict):
            return baseline["strategy_params"]

        strategies = db.query(StrategyConfig).all()
        strategy_params = {
            f"{strategy.timeframe}:{strategy.strategy_name}": dict(strategy.params or {})
            for strategy in strategies
        }
        baseline["strategy_params"] = strategy_params
        state = db.get(AppSetting, "auto_calibration_baseline")
        if state is not None:
            state.value = baseline
            db.add(state)
        return strategy_params

    def _calibrate_strategy_weights(
        self,
        db: Session,
        settings: dict[str, Any],
        *,
        total_closed: int = 0,
        positions_cache: dict[str, dict[str, list[Position]]] | None = None,
    ) -> list[CalibrationChange]:
        baseline = self._baseline_state(db)
        baseline_weights: dict[str, int] = baseline.get("strategy_weights", {})
        min_trades = int(settings["min_strategy_trades"])
        min_weight = int(settings["min_weight"])
        max_weight = int(settings["max_weight"])
        auto_disable_enabled = bool(settings.get("auto_disable_enabled", True))
        auto_disable_min_trades = int(settings.get("auto_disable_min_trades", 25))
        auto_disable_win_rate = float(settings.get("auto_disable_win_rate", 0.30))
        retest_after_trades = int(settings.get("auto_disable_retest_after_trades", 200))
        changes: list[CalibrationChange] = []
        strategies = db.query(StrategyConfig).all()
        active_per_timeframe: dict[str, int] = {}
        for strategy in strategies:
            if strategy.enabled:
                active_per_timeframe[strategy.timeframe] = (
                    active_per_timeframe.get(strategy.timeframe, 0) + 1
                )

        # Auto-disable bookkeeping: traccia il total_closed_trades al momento del disable.
        # Quando il counter di trade chiusi è cresciuto di `retest_after_trades` dal disable
        # rispetto alla data odierna, riabilita la strategia in modalità "probe" — eviterà
        # di rimanere intrappolata per sempre se il regime è cambiato.
        state_setting = db.get(AppSetting, "auto_calibration_state")
        state_value = dict(
            state_setting.value
            if state_setting is not None and isinstance(state_setting.value, dict)
            else {}
        )
        disabled_at: dict[str, int] = dict(state_value.get("auto_disabled_at_trade_count") or {})
        # Backfill: strategie disabilitate prima che il tracking venisse introdotto
        # non hanno entry in disabled_at — senza di essa il retest non scatterebbe mai.
        # Le aggiungiamo con il trade count attuale così il retest si attiva dopo
        # `retest_after_trades` nuovi trade chiusi a partire da ora.
        for strategy in strategies:
            if not strategy.enabled:
                key = f"{strategy.timeframe}:{strategy.strategy_name}"
                if key not in disabled_at:
                    disabled_at[key] = int(total_closed)
        for strategy in strategies:
            if strategy.enabled:
                continue
            key = f"{strategy.timeframe}:{strategy.strategy_name}"
            disabled_count = disabled_at.get(key)
            if disabled_count is None:
                continue
            if total_closed - int(disabled_count) >= retest_after_trades:
                strategy.enabled = True
                active_per_timeframe[strategy.timeframe] = (
                    active_per_timeframe.get(strategy.timeframe, 0) + 1
                )
                disabled_at.pop(key, None)
                changes.append(
                    CalibrationChange(
                        target="strategy_enabled",
                        key=key,
                        before=False,
                        after=True,
                        reason=(
                            f"auto-retest: re-enabled after "
                            f"{total_closed - int(disabled_count)} new trades closed"
                        ),
                    )
                )

        positions_by_timeframe: dict[str, dict[str, list[Position]]] = (
            positions_cache if positions_cache is not None else {}
        )

        # Estrai impostazioni walk-forward per la validazione holdout
        wf_enabled = bool(settings.get("walk_forward_enabled", True))
        wf_train_ratio = float(settings.get("walk_forward_train_ratio", 0.7))
        wf_min_per_side = int(settings.get("walk_forward_min_per_side", 5))
        factor_history_window = int(settings.get("factor_history_window", 5))
        factor_history_threshold = float(settings.get("factor_history_std_threshold", 0.25))
        # Storico dei factor decisi per strategia (serve per detectare instabilità)
        factor_history: dict[str, list[float]] = dict(state_value.get("factor_history") or {})

        for strategy in strategies:
            if strategy.timeframe not in positions_by_timeframe:
                positions_by_timeframe[strategy.timeframe] = self.analyzer.strategy_positions(
                    db,
                    timeframe=strategy.timeframe,
                    limit=int(settings["lookback_trades"]),
                )
            strategy_positions = positions_by_timeframe[strategy.timeframe].get(
                strategy.strategy_name, []
            )
            stats = self.analyzer.summarize_positions(strategy_positions)
            if stats.trades < min_trades:
                continue
            key = f"{strategy.timeframe}:{strategy.strategy_name}"
            # Auto-disable usa Wilson upper bound: disabilita solo se *anche* nel migliore
            # dei casi (UB del 95% CI) la strategia non raggiunge la soglia. Evita di
            # disabilitare strategie con campione piccolo ma potenziale promettente.
            wr_ub = _wilson_upper_bound(stats.wins, stats.trades)
            if (
                auto_disable_enabled
                and strategy.enabled
                and stats.trades >= auto_disable_min_trades
                and wr_ub < auto_disable_win_rate
                and active_per_timeframe.get(strategy.timeframe, 0) > 1
            ):
                strategy.enabled = False
                active_per_timeframe[strategy.timeframe] -= 1
                disabled_at[key] = int(total_closed)
                changes.append(
                    CalibrationChange(
                        target="strategy_enabled",
                        key=key,
                        before=True,
                        after=False,
                        reason=(
                            f"auto-disabled: WR UB={wr_ub:.2f} < {auto_disable_win_rate:.2f} "
                            f"over {stats.trades} trades"
                        ),
                    )
                )
                continue
            baseline_weight = int(baseline_weights.get(key, strategy.weight))
            factor, reason = self._strategy_factor_holdout_validated(
                strategy_positions,
                enabled=wf_enabled,
                train_ratio=wf_train_ratio,
                min_per_side=wf_min_per_side,
            )
            # Stability tracking: registra il factor calcolato e congela il cambio se
            # i factor recenti per questa strategia variano troppo. La instabilità
            # è un segnale di overfitting al rumore — meglio non muovere i pesi.
            history = list(factor_history.get(key) or [])
            history.append(factor)
            history = history[-factor_history_window:]
            factor_history[key] = history
            if len(history) >= 3:
                mean = sum(history) / len(history)
                variance = sum((v - mean) ** 2 for v in history) / len(history)
                std_dev = math.sqrt(variance)
                if std_dev > factor_history_threshold:
                    factor = 1.0
                    reason = (
                        f"stability freeze: factor std={std_dev:.2f} over last "
                        f"{len(history)} runs above threshold {factor_history_threshold}"
                    )
            calibrated = max(min_weight, min(max_weight, round(baseline_weight * factor)))
            if calibrated == strategy.weight:
                continue
            changes.append(
                CalibrationChange(
                    target="strategy_weight",
                    key=key,
                    before=strategy.weight,
                    after=calibrated,
                    reason=reason,
                )
            )
            strategy.weight = calibrated

        # Persisti il bookkeeping dei disable + storico dei factor (stability tracking).
        # Il merge con il resto del state avviene in _calibrate_risk_settings (che scrive
        # lo stesso oggetto) o in _record_run.
        state_value["auto_disabled_at_trade_count"] = disabled_at
        state_value["factor_history"] = factor_history
        persisted = state_setting or AppSetting(key="auto_calibration_state", value={})
        persisted.value = state_value
        db.add(persisted)
        return changes

    def _calibrate_strategy_params(
        self,
        db: Session,
        settings: dict[str, Any],
        *,
        positions_cache: dict[str, dict[str, list[Position]]] | None = None,
    ) -> list[CalibrationChange]:
        baseline_params = self._baseline_strategy_params(db)
        min_trades = int(settings["min_strategy_trades"])
        wf_enabled = bool(settings.get("walk_forward_enabled", True))
        wf_train_ratio = float(settings.get("walk_forward_train_ratio", 0.7))
        wf_min_per_side = int(settings.get("walk_forward_min_per_side", 5))
        changes: list[CalibrationChange] = []
        strategies = db.query(StrategyConfig).all()
        positions_by_timeframe: dict[str, dict[str, list[Position]]] = (
            positions_cache if positions_cache is not None else {}
        )

        for strategy in strategies:
            if strategy.timeframe not in positions_by_timeframe:
                positions_by_timeframe[strategy.timeframe] = self.analyzer.strategy_positions(
                    db,
                    timeframe=strategy.timeframe,
                    limit=int(settings["lookback_trades"]),
                )
            strategy_positions = positions_by_timeframe[strategy.timeframe].get(
                strategy.strategy_name, []
            )
            stats = self.analyzer.summarize_positions(strategy_positions)
            if stats.trades < min_trades:
                continue

            key = f"{strategy.timeframe}:{strategy.strategy_name}"
            base_params = dict(baseline_params.get(key, strategy.params or {}))
            next_params, reason = self._strategy_params_holdout_validated(
                strategy.strategy_name,
                base_params,
                strategy_positions,
                enabled=wf_enabled,
                train_ratio=wf_train_ratio,
                min_per_side=wf_min_per_side,
            )
            current_params = dict(strategy.params or {})
            if next_params == current_params:
                continue
            changes.append(
                CalibrationChange(
                    target="strategy_params",
                    key=key,
                    before=current_params,
                    after=next_params,
                    reason=reason,
                )
            )
            strategy.params = next_params
        return changes

    def _consecutive_zero_signal_cycles(self, db: Session) -> int:
        if not hasattr(db, "query"):
            return 0
        recent = (
            db.query(DecisionLog)
            .filter(DecisionLog.event_type.in_(["worker_cycle", "worker_cycle_failed"]))
            .order_by(DecisionLog.timestamp.desc())
            .limit(120)
            .all()
        )
        streak = 0
        for entry in recent:
            if entry.event_type == "worker_cycle_failed":
                streak += 1
                continue
            if not isinstance(entry.data, dict):
                break
            # I tick "no-op" (nessun timeframe attivo nel tick) non rappresentano vera
            # starvation: il worker non ha nemmeno provato a generare segnali. Saltarli
            # evita che la cadenza dei tick gonfi artificialmente il counter.
            active_tf = entry.data.get("active_timeframes")
            if isinstance(active_tf, list) and not active_tf:
                continue
            if int(entry.data.get("generated", 0)) == 0:
                streak += 1
            else:
                break
        return streak

    def _calibrate_risk_settings(
        self,
        db: Session,
        overall: PerformanceStats,
        settings: dict[str, Any],
        *,
        equity_slope_value: float = 0.0,
        recent_stats: PerformanceStats | None = None,
        recent_slope_value: float = 0.0,
    ) -> list[CalibrationChange]:
        baseline = self._baseline_state(db).get("risk", {})
        app_settings = get_settings()
        risk_setting = db.get(AppSetting, "risk") or AppSetting(key="risk", value={})
        risk = dict(risk_setting.value if isinstance(risk_setting.value, dict) else {})

        base_score = int(baseline.get("min_signal_score", app_settings.min_signal_score))
        base_rr = float(baseline.get("min_risk_reward", max(app_settings.min_risk_reward, 2.0)))
        base_risk = float(baseline.get("max_risk_per_trade_pct", app_settings.max_risk_per_trade_pct))

        state_setting = db.get(AppSetting, "auto_calibration_state")
        state_value = dict(
            state_setting.value
            if state_setting is not None and isinstance(state_setting.value, dict)
            else {}
        )
        prev_streak = int(state_value.get("deep_defensive_positive_streak", 0))
        prev_recent_streak = int(state_value.get("deep_defensive_recent_positive_streak", 0))
        prev_in_deep = bool(state_value.get("deep_defensive_active", False))
        deep_threshold = float(settings.get("deep_defensive_slope_threshold", -2.0))
        recent_slope_exit_cycles = int(settings.get("recent_slope_exit_cycles", 3))
        cooldown_runs = max(0, int(settings.get("deep_defensive_cooldown_runs", 6)))
        cooldown_min_new_trades = max(
            0, int(settings.get("deep_defensive_cooldown_min_new_trades", 3))
        )
        cooldown_remaining = max(
            0, int(state_value.get("deep_defensive_cooldown_remaining", 0) or 0)
        )
        cooldown_trade_count_raw = state_value.get(
            "deep_defensive_cooldown_trade_count",
            overall.trades,
        )
        try:
            cooldown_trade_count = int(cooldown_trade_count_raw)
        except (TypeError, ValueError):
            cooldown_trade_count = overall.trades

        min_risk_floor = float(settings.get("min_risk_per_trade_pct", 0.25))

        # Aggiorna streak per slope recente (ultimi N trade)
        new_recent_streak = (prev_recent_streak + 1) if recent_slope_value >= 0 else 0

        in_deep_defensive = False
        new_streak = 0
        if equity_slope_value < deep_threshold:
            in_deep_defensive = True
            new_streak = 0
            new_recent_streak = 0
        elif equity_slope_value >= 0:
            new_streak = prev_streak + 1
            in_deep_defensive = prev_in_deep and new_streak < 2
        else:
            new_streak = 0
            in_deep_defensive = prev_in_deep

        # Uscita via slope recente: se il trend degli ultimi N trade è positivo
        # per abbastanza cicli consecutivi, il recupero è in corso — usciamo dal deep
        # defensive senza aspettare che l'intera finestra storica si aggiusti.
        if in_deep_defensive and new_recent_streak >= recent_slope_exit_cycles:
            in_deep_defensive = False
            new_streak = 0
            state_value["deep_defensive_last_exit_reason"] = "recent_slope_recovery"

        # Starvation recovery: se troppi cicli consecutivi senza segnali il deep
        # defensive non sta proteggendo nulla — allentare progressivamente.
        zero_cycles = self._consecutive_zero_signal_cycles(db)
        starvation_soft = int(settings.get("starvation_soft_cycles", 10))
        starvation_hard = int(settings.get("starvation_hard_cycles", 15))
        starvation_override = False
        if in_deep_defensive and zero_cycles >= starvation_hard:
            in_deep_defensive = False
            new_streak = 0
            starvation_override = True
            state_value["deep_defensive_last_exit_reason"] = "starvation_recovery"

        # Equity-slope "ambiguo": se la calibrazione è in deep defensive ma il numero
        # di trade chiusi nella finestra è troppo basso per stimare lo slope in modo
        # affidabile, il vincolo non sta proteggendo nulla — usciamo dal deep defensive
        # per non bloccare i segnali artificialmente.
        min_slope_trades = int(settings.get("min_trades_for_slope", 10))
        ambiguous_slope = overall.trades < min_slope_trades
        if in_deep_defensive and ambiguous_slope:
            in_deep_defensive = False
            new_streak = 0
            starvation_override = True
            state_value["deep_defensive_last_exit_reason"] = "ambiguous_slope"
        state_value["consecutive_zero_signal_cycles"] = zero_cycles
        state_value["starvation_override_active"] = starvation_override
        state_value["ambiguous_slope_override"] = ambiguous_slope and starvation_override

        cooldown_started = False
        if prev_in_deep and not in_deep_defensive and cooldown_runs > 0:
            cooldown_remaining = cooldown_runs
            cooldown_trade_count = overall.trades
            cooldown_started = True
            state_value["deep_defensive_cooldown_started_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            state_value["deep_defensive_cooldown_trade_count"] = cooldown_trade_count

        new_trades_since_cooldown = max(0, overall.trades - cooldown_trade_count)
        cooldown_active = (
            cooldown_remaining > 0
            and new_trades_since_cooldown < cooldown_min_new_trades
        )
        next_cooldown_remaining = cooldown_remaining
        if cooldown_active and not cooldown_started:
            next_cooldown_remaining = max(0, cooldown_remaining - 1)
        if new_trades_since_cooldown >= cooldown_min_new_trades:
            cooldown_active = False
            next_cooldown_remaining = 0

        if cooldown_active:
            in_deep_defensive = False

        # Trend recente positivo: profit_factor > 1 sugli ultimi N trade indica che
        # il sistema sta guadagnando più di quanto perde, indipendentemente dal win rate.
        # È la metrica corretta perché incorpora già il rapporto RR reale conseguito:
        # un 40% WR con RR 1:3 dà PF=2.0 — ben sopra la soglia — mentre un WR del 50%
        # con RR 1:1 dà PF=1.0. La soglia fissa sul win rate ignorerebbe questa distinzione.
        recent_min_trades = int(settings.get("recent_window", 50)) // 2
        recent_recovering = (
            recent_stats is not None
            and recent_stats.trades >= recent_min_trades
            and recent_stats.profit_factor is not None
            and recent_stats.profit_factor > 1.0
        )

        score_delta = 0
        rr_delta = 0.0
        risk_pct = base_risk
        reason = "historical performance near baseline"
        if cooldown_active:
            score_delta = 0
            rr_delta = 0.0
            risk_pct = base_risk
            reason = (
                "deep defensive cooldown: baseline mantenuto "
                f"({next_cooldown_remaining} run rimanenti, "
                f"{new_trades_since_cooldown}/{cooldown_min_new_trades} nuovi trade)"
            )
        elif starvation_override and ambiguous_slope and overall.trades < min_slope_trades:
            score_delta = 0
            rr_delta = 0.0
            risk_pct = base_risk
            reason = (
                f"ambiguous slope ({overall.trades} trade chiusi < {min_slope_trades}): "
                "parametri al baseline finché non si raccolgono più dati"
            )
        elif starvation_override:
            score_delta = 0
            rr_delta = 0.0
            risk_pct = base_risk
            reason = (
                f"starvation recovery: {zero_cycles} cicli consecutivi senza segnali, "
                "parametri ripristinati al baseline"
            )
        elif in_deep_defensive and zero_cycles >= starvation_soft:
            score_delta = 6
            rr_delta = 0.2
            risk_pct = max(min_risk_floor, base_risk * 0.75)
            reason = (
                f"deep defensive attenuato: {zero_cycles} cicli senza segnali, "
                f"equity slope {equity_slope_value:.4f}"
            )
        elif in_deep_defensive:
            score_delta = 12
            rr_delta = 0.4
            risk_pct = max(min_risk_floor, base_risk * 0.5)
            reason = (
                f"deep defensive mode: equity slope {equity_slope_value:.4f} "
                f"below threshold {deep_threshold}"
            )
        elif overall.realized_pnl < 0 or overall.win_rate < 0.4 or (
            overall.profit_factor is not None and overall.profit_factor < 0.9
        ):
            if recent_recovering:
                # Il trend recente è positivo: il recupero è in corso.
                # Usciamo dal defensive senza aspettare che l'intera finestra storica
                # (fino a 500 trade) ritorni positiva — sarebbe un requisito irraggiungibile.
                score_delta = 0
                rr_delta = 0.0
                risk_pct = base_risk
                reason = (
                    f"recovering: ultimi {recent_stats.trades} trade positivi "  # type: ignore[union-attr]
                    f"(wr={recent_stats.win_rate:.2f}), ripristino baseline"  # type: ignore[union-attr]
                )
            else:
                score_delta = 8
                rr_delta = 0.25
                risk_pct = max(min_risk_floor, base_risk * 0.75)
                reason = "defensive mode after weak historical results"
        elif recent_recovering or (
            overall.win_rate >= 0.58
            and overall.realized_pnl > 0
            and (overall.profit_factor is None or overall.profit_factor >= 1.2)
        ):
            score_delta = 0
            rr_delta = 0.0
            risk_pct = base_risk
            reason = (
                "restore baseline after strong recent results"
                if recent_recovering
                else "restore baseline after strong historical results"
            )

        state_value["deep_defensive_active"] = in_deep_defensive
        state_value["deep_defensive_positive_streak"] = new_streak
        state_value["deep_defensive_recent_positive_streak"] = new_recent_streak
        state_value["deep_defensive_cooldown_remaining"] = next_cooldown_remaining
        state_value["deep_defensive_cooldown_trade_count"] = cooldown_trade_count
        state_value["last_equity_slope"] = equity_slope_value
        state_value["last_recent_slope"] = recent_slope_value
        state_setting_to_persist = state_setting or AppSetting(
            key="auto_calibration_state", value={}
        )
        state_setting_to_persist.value = state_value
        db.add(state_setting_to_persist)

        next_score = int(
            max(
                int(settings["min_signal_score_floor"]),
                min(int(settings["min_signal_score_ceiling"]), base_score + score_delta),
            )
        )
        next_rr = round(
            max(
                float(settings["min_risk_reward_floor"]),
                min(float(settings["min_risk_reward_ceiling"]), base_rr + rr_delta),
            ),
            2,
        )
        next_risk_pct = round(min(base_risk, risk_pct), 4)

        changes: list[CalibrationChange] = []
        if cooldown_active:
            changes.append(
                CalibrationChange(
                    target="calibration_state",
                    key="deep_defensive_cooldown",
                    before=cooldown_remaining,
                    after=next_cooldown_remaining,
                    reason=reason,
                )
            )
        for key, value in {
            "min_signal_score": next_score,
            "min_risk_reward": next_rr,
            "max_risk_per_trade_pct": next_risk_pct,
        }.items():
            before = risk.get(key)
            if before == value:
                continue
            risk[key] = value
            changes.append(
                CalibrationChange(
                    target="risk_setting",
                    key=key,
                    before=before,
                    after=value,
                    reason=reason,
                )
            )
        risk_setting.value = risk
        db.add(risk_setting)
        return changes

    def _calibrate_edge_adjustments(
        self,
        db: Session,
        settings: dict[str, Any],
    ) -> list[CalibrationChange]:
        risk_setting = db.get(AppSetting, "risk") or AppSetting(key="risk", value={})
        risk = dict(risk_setting.value if isinstance(risk_setting.value, dict) else {})
        lookback = int(settings.get("lookback_trades", 500))
        edge_adjustments = compute_edge_adjustments(
            db,
            lookback=lookback,
            analyzer=self.analyzer,
        )

        defaults: dict[str, Any] = {
            "directional_score_adjustments": {},
            "score_bucket_adjustments": {},
            "market_score_adjustments": {},
            "timeframe_score_adjustments": {},
            "allow_ob_imbalance_only_signals": False,
            "entry_quality_filter_enabled": True,
            "entry_quality_ema_period": 21,
            "entry_quality_long_ob_imbalance_min": 0.05,
            "entry_quality_short_ob_imbalance_max": 0.0,
            "max_combined_score_adjustment": DEFAULT_MAX_COMBINED_SCORE_ADJUSTMENT,
        }
        changes: list[CalibrationChange] = []
        for key, default in defaults.items():
            if key not in risk:
                risk[key] = default
                changes.append(
                    CalibrationChange(
                        target="risk_setting",
                        key=key,
                        before=None,
                        after=default,
                        reason="edge calibration defaults initialized",
                    )
                )

        for key, value in edge_adjustments.items():
            before = risk.get(key)
            if before == value:
                continue
            risk[key] = value
            changes.append(
                CalibrationChange(
                    target="risk_setting",
                    key=key,
                    before=before if isinstance(before, dict) else None,
                    after=value,
                    reason="edge calibration from closed paper outcomes",
                )
            )

        if changes:
            risk_setting.value = risk
            db.add(risk_setting)
        return changes

    def _strategy_factor(self, stats: PerformanceStats) -> tuple[float, str]:
        profit_factor = stats.profit_factor
        # Why: il win rate puntuale su poche decine di trade è rumoroso (binomial CI ampio).
        # Usiamo Wilson lower bound al 95% CI come stima conservativa: solo se il LB
        # supera la soglia consideriamo la strategia confermata sopra (o sotto) di essa.
        wr_lb = _wilson_lower_bound(stats.wins, stats.trades)
        wr_ub = _wilson_upper_bound(stats.wins, stats.trades)
        if stats.realized_pnl < 0 and (
            wr_ub < 0.40 or (profit_factor is not None and profit_factor < 0.8)
        ):
            return 0.6, (
                f"strategy underperformed historically "
                f"(WR LB={wr_lb:.2f}, UB={wr_ub:.2f}, n={stats.trades})"
            )
        if wr_ub < 0.50 or (profit_factor is not None and profit_factor < 1.0):
            return 0.8, (
                f"strategy below-target historical performance "
                f"(WR LB={wr_lb:.2f}, UB={wr_ub:.2f}, n={stats.trades})"
            )
        if wr_lb >= 0.55 and stats.realized_pnl > 0 and (
            profit_factor is None or profit_factor >= 1.25
        ):
            return 1.2, f"strategy outperformed historically (WR LB={wr_lb:.2f}, n={stats.trades})"
        if wr_lb >= 0.50 and stats.realized_pnl > 0:
            return 1.1, f"strategy positive historical edge (WR LB={wr_lb:.2f}, n={stats.trades})"
        return 1.0, "strategy performance near baseline"

    def _strategy_factor_holdout_validated(
        self,
        positions: list[Position],
        *,
        enabled: bool,
        train_ratio: float,
        min_per_side: int,
    ) -> tuple[float, str]:
        """Calcola il factor con validazione walk-forward holdout.

        Logica: il factor "full" (su tutte le posizioni) è applicato SOLO se concorda
        con il factor calcolato sulla sola finestra di test (più recente). Se train e
        test divergono di un livello (es. train 0.6, test 1.0) → factor 1.0 + reason
        "unstable across holdout split" — meglio non fare un cambio guidato dal rumore
        in-sample che non si conferma fuori campione.
        """
        full_stats = self.analyzer.summarize_positions(positions)
        full_factor, full_reason = self._strategy_factor(full_stats)
        if not enabled:
            return full_factor, full_reason
        split = SignalPerformanceAnalyzer.holdout_split_stats(
            positions, train_ratio=train_ratio, min_samples_per_side=min_per_side
        )
        if split is None:
            # Non abbastanza dati per holdout. Applichiamo il factor full ma
            # marchiamo la decisione come "no-holdout" per trasparenza.
            return full_factor, f"{full_reason} (no holdout: n={len(positions)})"
        train_stats, test_stats = split
        train_factor, _ = self._strategy_factor(train_stats)
        test_factor, _ = self._strategy_factor(test_stats)
        # "Concordi" = factor sullo stesso lato di 1.0 (entrambi <= 1.0 o entrambi >= 1.0).
        # Train che dice 0.6 e test che dice 1.1 = regime cambiato → no cambio peso.
        concordant = (train_factor <= 1.0 and test_factor <= 1.0) or (
            train_factor >= 1.0 and test_factor >= 1.0
        )
        if not concordant:
            return 1.0, (
                f"holdout instability: train_factor={train_factor:.2f} vs "
                f"test_factor={test_factor:.2f} (n_train={train_stats.trades}, "
                f"n_test={test_stats.trades})"
            )
        return full_factor, f"{full_reason} (holdout ok: train={train_factor:.2f}, test={test_factor:.2f})"

    def _strategy_params_for_stats(
        self,
        strategy_name: str,
        baseline_params: dict[str, Any],
        stats: PerformanceStats,
    ) -> tuple[dict[str, Any], str]:
        if not baseline_params:
            return baseline_params, "strategy has no tunable parameters"
        wr_lb, wr_ub = _wilson_bounds(stats.wins, stats.trades)
        pf = stats.profit_factor
        # Strategie low-WR / high-R sono profittevoli con WR anche del 35-40% se hanno
        # PF > 1.1 — non vanno trattate come underperformer solo perché il WR è basso.
        # Restituiamo il baseline anche quando WR è modesto ma PF e PnL sono positivi.
        if stats.realized_pnl > 0 and pf is not None and pf >= 1.2:
            return dict(baseline_params), (
                f"strategy profitable: PF={pf:.2f} su {stats.trades} trade — baseline"
            )
        if stats.realized_pnl < 0 and (
            wr_ub < 0.40 or (pf is not None and pf < 0.8)
        ):
            return (
                self._defensive_strategy_params(strategy_name, baseline_params, intensity=1.0),
                f"strategy underperformed (WR UB={wr_ub:.2f}, PF={pf}); more selective",
            )
        if wr_ub < 0.50 or (pf is not None and pf < 1.0):
            return (
                self._defensive_strategy_params(strategy_name, baseline_params, intensity=0.5),
                f"strategy below-target (WR UB={wr_ub:.2f}, PF={pf}); slightly more selective",
            )
        if wr_lb >= 0.50 and stats.realized_pnl > 0 and (pf is None or pf >= 1.1):
            return dict(baseline_params), (
                f"strategy recovered (WR LB={wr_lb:.2f}, PF={pf}); restored to baseline"
            )
        return dict(baseline_params), "strategy performance near baseline"

    def _strategy_params_holdout_validated(
        self,
        strategy_name: str,
        baseline_params: dict[str, Any],
        positions: list[Position],
        *,
        enabled: bool,
        train_ratio: float,
        min_per_side: int,
    ) -> tuple[dict[str, Any], str]:
        """Variante params con walk-forward holdout: applica il cambio solo se
        train e test concordano nel "verso" dei params (defensive vs baseline)."""
        full_stats = self.analyzer.summarize_positions(positions)
        full_params, full_reason = self._strategy_params_for_stats(
            strategy_name, baseline_params, full_stats
        )
        if not enabled:
            return full_params, full_reason
        split = SignalPerformanceAnalyzer.holdout_split_stats(
            positions, train_ratio=train_ratio, min_samples_per_side=min_per_side
        )
        if split is None:
            return full_params, f"{full_reason} (no holdout: n={len(positions)})"
        train_stats, test_stats = split
        train_params, _ = self._strategy_params_for_stats(
            strategy_name, baseline_params, train_stats
        )
        test_params, _ = self._strategy_params_for_stats(
            strategy_name, baseline_params, test_stats
        )
        # Concordi = stessa direzione rispetto al baseline (baseline / defensive).
        train_is_baseline = train_params == baseline_params
        test_is_baseline = test_params == baseline_params
        if train_is_baseline != test_is_baseline:
            return dict(baseline_params), (
                f"holdout instability: params differ between train (baseline={train_is_baseline}) "
                f"and test (baseline={test_is_baseline}) — keeping baseline"
            )
        return full_params, f"{full_reason} (holdout ok)"

    def _defensive_strategy_params(
        self,
        strategy_name: str,
        baseline_params: dict[str, Any],
        *,
        intensity: float,
    ) -> dict[str, Any]:
        params = dict(baseline_params)

        def number(key: str, fallback: float) -> float:
            try:
                return float(params.get(key, fallback))
            except (TypeError, ValueError):
                return fallback

        def rounded(value: float, step: float = 1.0) -> float | int:
            scaled = round(value / step) * step
            return int(scaled) if step == 1.0 else round(scaled, 6)

        def clamp(value: float, low: float, high: float) -> float:
            return max(low, min(high, value))

        def set_int(key: str, factor: float, low: int = 2, high: int = 500) -> None:
            if key in params:
                params[key] = int(rounded(clamp(number(key, 0) * factor, low, high)))

        def set_float(key: str, factor: float, low: float, high: float, step: float = 0.0001) -> None:
            if key in params:
                params[key] = rounded(clamp(number(key, 0.0) * factor, low, high), step)

        slow_factor = 1.0 + (0.2 * intensity)
        strict_factor = 1.0 + (0.25 * intensity)
        tolerance_factor = 1.0 - (0.25 * intensity)

        if strategy_name == "ema_crossover":
            set_int("fast", 1.0 + (0.15 * intensity), high=100)
            set_int("slow", slow_factor, high=250)
            set_int("trend", 1.0 + (0.1 * intensity), high=500)
            if "fast" in params and "slow" in params and int(params["fast"]) >= int(params["slow"]):
                params["slow"] = int(params["fast"]) + 1
        elif strategy_name == "rsi":
            if "oversold" in params:
                params["oversold"] = int(rounded(clamp(number("oversold", 30) - (5 * intensity), 10, 45)))
            if "overbought" in params:
                params["overbought"] = int(rounded(clamp(number("overbought", 70) + (5 * intensity), 55, 90)))
            set_int("period", 1.0 + (0.1 * intensity), high=50)
        elif strategy_name == "macd":
            set_int("fast", 1.0 + (0.1 * intensity), high=50)
            set_int("slow", 1.0 + (0.15 * intensity), high=100)
            set_int("signal", 1.0 + (0.1 * intensity), high=50)
            if "fast" in params and "slow" in params and int(params["fast"]) >= int(params["slow"]):
                params["slow"] = int(params["fast"]) + 1
        elif strategy_name == "volume_spike":
            set_int("period", slow_factor, high=120)
            set_float("multiplier", strict_factor, low=1.0, high=5.0, step=0.05)
        elif strategy_name == "supertrend":
            set_int("period", 1.0 + (0.15 * intensity), high=80)
            set_float("multiplier", strict_factor, low=1.0, high=8.0, step=0.1)
        elif strategy_name == "bollinger_bands":
            set_int("period", 1.0 + (0.1 * intensity), high=100)
            set_float("stddev", 1.0 + (0.15 * intensity), low=1.0, high=4.0, step=0.1)
        elif strategy_name == "vwap":
            set_int("period", slow_factor, high=240)
            set_float("tolerance_pct", tolerance_factor, low=0.0005, high=0.02, step=0.0001)
        elif strategy_name == "fibonacci":
            set_int("lookback", 1.0 + (0.15 * intensity), high=500)
            set_float("tolerance_pct", tolerance_factor, low=0.0005, high=0.02, step=0.0001)
        elif strategy_name == "support_resistance":
            set_int("lookback", 1.0 + (0.15 * intensity), high=500)
            set_int("pivot_window", 1.0 + (0.25 * intensity), high=20)
            set_float("tolerance_pct", tolerance_factor, low=0.0005, high=0.02, step=0.0001)

        return params

    def _result(
        self,
        applied: bool,
        eligible: bool,
        reason: str,
        overall: PerformanceStats,
        changes: list[CalibrationChange],
        strategy_adjustments: dict[str, dict[str, Any]],
        settings: dict[str, Any],
    ) -> CalibrationResult:
        return CalibrationResult(
            applied=applied,
            eligible=eligible,
            reason=reason,
            overall=overall,
            changes=changes,
            strategy_adjustments=strategy_adjustments,
            settings=settings,
        )

    def _record_run(
        self,
        db: Session,
        *,
        force: bool,
        overall: PerformanceStats,
        changes: list[CalibrationChange],
        event_type: str,
        reason: str,
        status: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        serialized_changes = [change.__dict__ for change in changes]
        state = db.get(AppSetting, "auto_calibration_state") or AppSetting(
            key="auto_calibration_state",
            value={},
        )
        merged_value = dict(state.value) if isinstance(state.value, dict) else {}
        merged_value.update(
            {
                "last_run_at": now,
                "last_trade_count": overall.trades,
                "last_summary": serialize_stats(overall),
                "last_changes": serialized_changes,
                "last_reason": reason,
                "last_status": status,
            }
        )
        state.value = merged_value
        db.add(state)
        db.add(
            DecisionLog(
                event_type=event_type,
                message="Auto calibration completed" if event_type != "auto_calibration_skipped" else "Auto calibration skipped",
                data={
                    "force": force,
                    "status": status,
                    "reason": reason,
                    "overall": serialize_stats(overall),
                    "changes": serialized_changes,
                },
            )
        )
        db.commit()


def _wilson_bounds(wins: int, trades: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval per la proporzione di vittorie.

    Why: con 20 trade e 7 wins (WR=0.35), l'intervallo di confidenza al 95% va da
    ~0.18 a ~0.57. Decidere di tagliare un weight basandosi sul punto WR=0.35 è
    sensibile al rumore campionario. La Wilson è preferibile alla Normal-approx
    perché è ben definita anche con piccoli campioni e per p vicino a 0 o 1.
    """
    if trades <= 0:
        return 0.0, 0.0
    p_hat = wins / trades
    z_sq = z * z
    denom = 1.0 + z_sq / trades
    center = (p_hat + z_sq / (2 * trades)) / denom
    margin = z * math.sqrt((p_hat * (1 - p_hat) + z_sq / (4 * trades)) / trades) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _wilson_lower_bound(wins: int, trades: int) -> float:
    return _wilson_bounds(wins, trades)[0]


def _wilson_upper_bound(wins: int, trades: int) -> float:
    return _wilson_bounds(wins, trades)[1]


def serialize_calibration(result: CalibrationResult) -> dict[str, Any]:
    return {
        "applied": result.applied,
        "eligible": result.eligible,
        "reason": result.reason,
        "overall": serialize_stats(result.overall),
        "changes": [change.__dict__ for change in result.changes],
        "strategy_adjustments": result.strategy_adjustments,
        "settings": result.settings,
    }
