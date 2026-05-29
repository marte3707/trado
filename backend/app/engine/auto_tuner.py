from __future__ import annotations

import math
import hashlib
import random
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AppSetting, DecisionLog, Position
from app.engine.diagnostics import equity_slope
from app.engine.performance import SignalPerformanceAnalyzer

DEFAULT_SEQUENCE: list[dict[str, Any]] = [
    {
        "key": "regime_filter",
        "label": "Regime filter (ADX)",
        "patch": {"regime_filter_enabled": True},
        "revert": {"regime_filter_enabled": False},
    },
    {
        "key": "mtf_confluence",
        "label": "Multi-timeframe confluence",
        "patch": {"mtf_confluence_enabled": True},
        "revert": {"mtf_confluence_enabled": False},
    },
    {
        "key": "volatility_filter",
        "label": "Volatility (ATR percentile) filter",
        "patch": {"volatility_filter_enabled": True},
        "revert": {"volatility_filter_enabled": False},
    },
    {
        "key": "participation_ratio_50",
        "label": "Participation ratio 0.50",
        "patch": {"participation_ratio": 0.50},
        "revert": {"participation_ratio": 0.35},
    },
    {
        # Auto-reverse posizioni su cambio regime BTC: vive in AppSetting
        # "btc_macro_protection" (non in "risk"). Richiede sequential mode
        # perché agisce sull'engine di protezione, non su un overlay per-segnale.
        "key": "auto_reverse",
        "label": "Auto-reverse on BTC regime change",
        "target_setting": "btc_macro_protection",
        "patch": {"enabled": True, "reverse_mode_enabled": True},
        "revert": {"reverse_mode_enabled": False},
        "sequential_only": True,
    },
]

SOURCE_AB_SEQUENCE: list[dict[str, Any]] = [
    {
        "key": "whale_flow_source",
        "label": "Whale flow source",
        "patch": {"disabled_signal_sources": ["fear_greed"]},
        "revert": {"disabled_signal_sources": ["whale_flow", "fear_greed"]},
    },
    {
        "key": "fear_greed_source",
        "label": "Fear & Greed source",
        "patch": {"disabled_signal_sources": ["whale_flow"]},
        "revert": {"disabled_signal_sources": ["whale_flow", "fear_greed"]},
    },
]

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    # "sequential" = comportamento legacy before/after sulla stessa popolazione.
    # "ab_test" = randomizzazione per-segnale tra control/test con Welch t-test
    # (vedi §3.10 di docs/USAGE.md).
    "mode": "sequential",
    "min_trades_per_step": 50,
    "improvement_threshold": 0.0,
    # Quando > 0, la soglia di promozione viene calcolata come max(improvement_threshold,
    # adaptive_threshold_multiplier × std(pnl)) — riduce i falsi successi.
    "adaptive_threshold_multiplier": 0.5,
    # Parametri specifici per mode="ab_test".
    "ab_test_min_trades_per_arm": 30,
    "ab_test_significance_level": 0.05,
    # P(assegnazione arm "test"); 0.5 = 50/50.
    "ab_test_allocation": 0.5,
    "ab_test_assignment_salt": "trado-v1",
    "sequence": DEFAULT_SEQUENCE,
}


def active_ab_overlay(
    db: Session,
    *,
    market_id: int | None = None,
    timeframe: str | None = None,
    source_candle_open_time: Any | None = None,
) -> dict[str, Any] | None:
    """Ritorna l'overlay AB da applicare al prossimo segnale, o None.

    Letta dal signal_engine ad ogni `evaluate()`: sceglie casualmente un arm e
    restituisce l'overlay corrispondente più il tag (`step_key`, `arm`) da
    persistere nel `strategy_breakdown` per attribuire il trade in fase di
    valutazione."""
    config_setting = db.get(AppSetting, "auto_tuner_config")
    if config_setting is None or not isinstance(config_setting.value, dict):
        return None
    config = config_setting.value
    if not bool(config.get("enabled", False)):
        return None
    if str(config.get("mode", "sequential")) != "ab_test":
        return None
    state_setting = db.get(AppSetting, "auto_tuner_state")
    if state_setting is None or not isinstance(state_setting.value, dict):
        return None
    state = state_setting.value
    ab_block = state.get("ab_test")
    if not isinstance(ab_block, dict):
        return None
    step_key = state.get("current_step_key")
    if not step_key:
        return None
    allocation = float(ab_block.get("allocation", 0.5))
    allocation = max(0.0, min(1.0, allocation))
    assignment_method = "random_fallback"
    if market_id is not None and timeframe and source_candle_open_time is not None:
        salt = str(config.get("ab_test_assignment_salt", "trado-v1"))
        assignment_key = (
            f"{salt}:{step_key}:{market_id}:{timeframe}:"
            f"{source_candle_open_time.isoformat() if hasattr(source_candle_open_time, 'isoformat') else source_candle_open_time}"
        )
        digest = hashlib.sha256(assignment_key.encode("utf-8")).hexdigest()
        ratio = int(digest[:16], 16) / float(16**16)
        arm = "test" if ratio < allocation else "control"
        assignment_method = "deterministic_hash"
    else:
        arm = "test" if random.random() < allocation else "control"
    overlay_raw = ab_block.get(f"{arm}_overlay") or {}
    if not isinstance(overlay_raw, dict):
        overlay_raw = {}
    return {
        "step_key": str(step_key),
        "arm": arm,
        "overlay": dict(overlay_raw),
        "assignment_method": assignment_method,
    }


class AutoTuner:
    def __init__(self, analyzer: SignalPerformanceAnalyzer | None = None) -> None:
        self.analyzer = analyzer or SignalPerformanceAnalyzer()

    def run(self, db: Session) -> dict[str, Any]:
        config = self._load_config(db)
        state = self._load_state(db)

        if not bool(config.get("enabled", False)):
            return {"status": "disabled"}

        sequence: list[dict[str, Any]] = list(config.get("sequence", []))
        if not sequence:
            return {"status": "no_sequence"}

        mode = str(config.get("mode", "sequential"))
        if mode == "ab_test":
            return self._run_ab_test(db, state, config, sequence)
        return self._run_sequential(db, state, config, sequence)

    def _run_sequential(
        self,
        db: Session,
        state: dict[str, Any],
        config: dict[str, Any],
        sequence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        history: list[dict[str, Any]] = list(state.get("step_history", []))
        completed_keys = {entry.get("key") for entry in history if entry.get("status") in {"success", "reverted", "skipped", "promoted", "rejected"}}

        if state.get("current_step_index") is None:
            next_index = self._next_step_index(sequence, completed_keys)
            if next_index is None:
                return self._finish(db, state, "completed", "all sequence steps completed")
            return self._start_step(db, state, config, sequence, next_index)

        idx = int(state["current_step_index"])
        if idx >= len(sequence):
            return self._finish(db, state, "completed", "step index out of sequence range")

        positions = self.analyzer.closed_paper_positions(db, limit=2000)
        trades_now = len(positions)
        trades_at_activation = int(state.get("trades_at_activation", trades_now))
        delta_trades = max(0, trades_now - trades_at_activation)
        min_trades = int(config.get("min_trades_per_step", 50))

        if delta_trades < min_trades:
            return {
                "status": "in_progress",
                "step": sequence[idx].get("key"),
                "trades_done": delta_trades,
                "trades_required": min_trades,
            }

        post_positions = self._positions_after(positions, state.get("activated_at"))
        if len(post_positions) < min_trades:
            return {
                "status": "in_progress",
                "step": sequence[idx].get("key"),
                "trades_done": len(post_positions),
                "trades_required": min_trades,
                "reason": "waiting for additional post-activation closures",
            }

        return self._evaluate_and_advance(
            db, state, config, sequence, idx, post_positions, history
        )

    def _run_ab_test(
        self,
        db: Session,
        state: dict[str, Any],
        config: dict[str, Any],
        sequence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        history: list[dict[str, Any]] = list(state.get("step_history", []))
        completed_keys = {
            entry.get("key")
            for entry in history
            if entry.get("status") in {"success", "reverted", "skipped", "promoted", "rejected"}
        }

        if state.get("current_step_index") is None:
            next_index = self._next_step_index(
                sequence, completed_keys, skip_sequential_only=True
            )
            if next_index is None:
                return self._finish(db, state, "completed", "all sequence steps completed")
            return self._start_ab_step(db, state, config, sequence, next_index)

        idx = int(state["current_step_index"])
        if idx >= len(sequence):
            return self._finish(db, state, "completed", "step index out of sequence range")

        step = sequence[idx]
        step_key = str(step.get("key"))
        positions = self.analyzer.closed_paper_positions(db, limit=5000)
        arms = self._partition_by_arm(positions, state.get("activated_at"), step_key)
        min_per_arm = int(config.get("ab_test_min_trades_per_arm", 30))
        n_control = len(arms["control"])
        n_test = len(arms["test"])

        if n_control < min_per_arm or n_test < min_per_arm:
            return {
                "status": "in_progress",
                "step": step_key,
                "mode": "ab_test",
                "trades_control": n_control,
                "trades_test": n_test,
                "trades_required_per_arm": min_per_arm,
            }

        return self._evaluate_ab_step(db, state, config, sequence, idx, arms, history)

    def _next_step_index(
        self,
        sequence: list[dict[str, Any]],
        completed_keys: set,
        skip_sequential_only: bool = False,
    ) -> int | None:
        for index, step in enumerate(sequence):
            if step.get("key") in completed_keys:
                continue
            if skip_sequential_only and bool(step.get("sequential_only", False)):
                continue
            return index
        return None

    def _start_step(
        self,
        db: Session,
        state: dict[str, Any],
        config: dict[str, Any],
        sequence: list[dict[str, Any]],
        index: int,
    ) -> dict[str, Any]:
        step = sequence[index]
        positions = self.analyzer.closed_paper_positions(db, limit=2000)
        metrics_before = self._metrics_for(positions[: int(config.get("min_trades_per_step", 50))])
        risk_snapshot = self._snapshot_risk(db)
        target_setting = str(step.get("target_setting", "risk"))
        self._apply_patch(db, dict(step.get("patch", {})), target_setting=target_setting)
        activated_at = datetime.now(timezone.utc).isoformat()
        new_state = dict(state)
        new_state.update(
            {
                "current_step_index": index,
                "current_step_key": step.get("key"),
                "activated_at": activated_at,
                "trades_at_activation": len(positions),
                "metrics_before": metrics_before,
                "risk_snapshot_before": risk_snapshot,
            }
        )
        self._persist_state(db, new_state)
        self._log_event(
            db,
            event_type="auto_tuner_step_started",
            message=f"Auto-tuner started step '{step.get('key')}'",
            data={
                "step": step.get("key"),
                "label": step.get("label"),
                "patch": step.get("patch"),
                "metrics_before": metrics_before,
                "trades_at_activation": len(positions),
            },
        )
        db.commit()
        return {
            "status": "started",
            "step": step.get("key"),
            "metrics_before": metrics_before,
            "min_trades_per_step": int(config.get("min_trades_per_step", 50)),
        }

    def _evaluate_and_advance(
        self,
        db: Session,
        state: dict[str, Any],
        config: dict[str, Any],
        sequence: list[dict[str, Any]],
        idx: int,
        post_positions: list[Position],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        step = sequence[idx]
        metrics_before = dict(state.get("metrics_before") or {})
        metrics_after = self._metrics_for(post_positions)
        threshold = float(config.get("improvement_threshold", 0.0))
        adaptive_multiplier = float(config.get("adaptive_threshold_multiplier", 0.0))
        # Why: prima si confrontava `equity_slope` (USD/trade-index) con una soglia
        # derivata da `std(pnl)` (USD/trade) — unità incomparabili. Ora il confronto è
        # tra `mean_pnl` before/after (USD/trade) con soglia in standard-error units
        # del mean (`multiplier * std_pnl / sqrt(n)`), che è una stima di significatività.
        pnl_values = [
            float(p.realized_pnl) for p in post_positions if p.realized_pnl is not None
        ]
        if adaptive_multiplier > 0 and len(pnl_values) >= 2:
            mean = sum(pnl_values) / len(pnl_values)
            variance = sum((x - mean) ** 2 for x in pnl_values) / (len(pnl_values) - 1)
            std_pnl = math.sqrt(variance)
            standard_error = std_pnl / math.sqrt(len(pnl_values))
            adaptive_threshold = adaptive_multiplier * standard_error
            threshold = max(threshold, adaptive_threshold)
        mean_pnl_before = float(metrics_before.get("mean_pnl", 0.0))
        mean_pnl_after = float(metrics_after.get("mean_pnl", 0.0))
        improved = mean_pnl_after >= mean_pnl_before + threshold

        outcome = "success" if improved else "reverted"
        if not improved:
            target_setting = str(step.get("target_setting", "risk"))
            self._apply_patch(
                db, dict(step.get("revert", {})), target_setting=target_setting
            )

        evaluated_at = datetime.now(timezone.utc).isoformat()
        history.append(
            {
                "key": step.get("key"),
                "label": step.get("label"),
                "status": outcome,
                "metrics_before": metrics_before,
                "metrics_after": metrics_after,
                "trades_evaluated": len(post_positions),
                "evaluated_at": evaluated_at,
            }
        )
        new_state = dict(state)
        new_state.update(
            {
                "current_step_index": None,
                "current_step_key": None,
                "activated_at": None,
                "trades_at_activation": None,
                "metrics_before": None,
                "risk_snapshot_before": None,
                "step_history": history,
            }
        )
        self._persist_state(db, new_state)
        self._log_event(
            db,
            event_type=f"auto_tuner_step_{outcome}",
            message=f"Auto-tuner step '{step.get('key')}' {outcome}",
            data={
                "step": step.get("key"),
                "label": step.get("label"),
                "metrics_before": metrics_before,
                "metrics_after": metrics_after,
                "improvement_threshold": threshold,
                "trades_evaluated": len(post_positions),
                "adaptive_threshold_multiplier": adaptive_multiplier,
            },
        )
        db.commit()
        if outcome == "success":
            status_payload = "advanced"
        else:
            status_payload = "reverted"
        return {
            "status": status_payload,
            "step": step.get("key"),
            "metrics_before": metrics_before,
            "metrics_after": metrics_after,
        }

    def _start_ab_step(
        self,
        db: Session,
        state: dict[str, Any],
        config: dict[str, Any],
        sequence: list[dict[str, Any]],
        index: int,
    ) -> dict[str, Any]:
        step = sequence[index]
        positions = self.analyzer.closed_paper_positions(db, limit=2000)
        risk_snapshot = self._snapshot_risk(db)
        # In modalità AB *non* modifichiamo `risk` globalmente: control e test
        # convivono e sono iniettati per-segnale tramite active_ab_overlay().
        control_overlay = dict(step.get("revert", {}))
        test_overlay = dict(step.get("patch", {}))
        allocation = float(config.get("ab_test_allocation", 0.5))
        allocation = max(0.0, min(1.0, allocation))
        activated_at = datetime.now(timezone.utc).isoformat()
        new_state = dict(state)
        new_state.update(
            {
                "current_step_index": index,
                "current_step_key": step.get("key"),
                "mode": "ab_test",
                "activated_at": activated_at,
                "trades_at_activation": len(positions),
                "metrics_before": None,
                "risk_snapshot_before": risk_snapshot,
                "ab_test": {
                    "control_overlay": control_overlay,
                    "test_overlay": test_overlay,
                    "allocation": allocation,
                    "assignment_method": "deterministic_hash",
                },
            }
        )
        self._persist_state(db, new_state)
        self._log_event(
            db,
            event_type="auto_tuner_ab_started",
            message=f"Auto-tuner AB test started for step '{step.get('key')}'",
            data={
                "step": step.get("key"),
                "label": step.get("label"),
                "control_overlay": control_overlay,
                "test_overlay": test_overlay,
                "allocation": allocation,
                "assignment_method": "deterministic_hash",
                "min_trades_per_arm": int(config.get("ab_test_min_trades_per_arm", 30)),
            },
        )
        db.commit()
        return {
            "status": "started",
            "mode": "ab_test",
            "step": step.get("key"),
            "control_overlay": control_overlay,
            "test_overlay": test_overlay,
            "allocation": allocation,
            "min_trades_per_arm": int(config.get("ab_test_min_trades_per_arm", 30)),
        }

    def _evaluate_ab_step(
        self,
        db: Session,
        state: dict[str, Any],
        config: dict[str, Any],
        sequence: list[dict[str, Any]],
        idx: int,
        arms: dict[str, list[Position]],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        step = sequence[idx]
        control_pnls = [
            float(p.realized_pnl) for p in arms["control"] if p.realized_pnl is not None
        ]
        test_pnls = [
            float(p.realized_pnl) for p in arms["test"] if p.realized_pnl is not None
        ]
        stats = self._welch_t_test(control_pnls, test_pnls)
        alpha = float(config.get("ab_test_significance_level", 0.05))
        # H1 one-tailed: test > control. Promuoviamo solo se la differenza è
        # positiva *e* statisticamente significativa.
        improvement = stats["mean_test"] - stats["mean_control"]
        promoted = improvement > 0 and stats["p_one_tailed"] < alpha
        sample_balance = (
            min(len(control_pnls), len(test_pnls)) / max(len(control_pnls), len(test_pnls))
            if control_pnls and test_pnls
            else 0.0
        )
        assignment_method = str(
            (state.get("ab_test") or {}).get("assignment_method", "deterministic_hash")
            if isinstance(state.get("ab_test"), dict)
            else "deterministic_hash"
        )

        if promoted:
            target_setting = str(step.get("target_setting", "risk"))
            self._apply_patch(
                db, dict(step.get("patch", {})), target_setting=target_setting
            )
            outcome = "promoted"
        else:
            outcome = "rejected"

        evaluated_at = datetime.now(timezone.utc).isoformat()
        history.append(
            {
                "key": step.get("key"),
                "label": step.get("label"),
                "status": outcome,
                "mode": "ab_test",
                "ab_test": {
                    "n_control": len(control_pnls),
                    "n_test": len(test_pnls),
                    "mean_control": stats["mean_control"],
                    "mean_test": stats["mean_test"],
                    "std_control": stats["std_control"],
                    "std_test": stats["std_test"],
                    "t_statistic": stats["t"],
                    "degrees_of_freedom": stats["df"],
                    "p_one_tailed": stats["p_one_tailed"],
                    "alpha": alpha,
                    "improvement": improvement,
                    "mean_delta": improvement,
                    "sample_balance": sample_balance,
                    "assignment_method": assignment_method,
                },
                "evaluated_at": evaluated_at,
            }
        )
        new_state = dict(state)
        new_state.update(
            {
                "current_step_index": None,
                "current_step_key": None,
                "activated_at": None,
                "trades_at_activation": None,
                "metrics_before": None,
                "risk_snapshot_before": None,
                "ab_test": None,
                "step_history": history,
            }
        )
        self._persist_state(db, new_state)
        self._log_event(
            db,
            event_type=f"auto_tuner_ab_{outcome}",
            message=f"Auto-tuner AB step '{step.get('key')}' {outcome}",
            data={
                "step": step.get("key"),
                "n_control": len(control_pnls),
                "n_test": len(test_pnls),
                "mean_control": stats["mean_control"],
                "mean_test": stats["mean_test"],
                "t_statistic": stats["t"],
                "p_one_tailed": stats["p_one_tailed"],
                "alpha": alpha,
                "mean_delta": improvement,
                "sample_balance": sample_balance,
                "assignment_method": assignment_method,
            },
        )
        db.commit()
        return {
            "status": outcome,
            "mode": "ab_test",
            "step": step.get("key"),
            "mean_control": stats["mean_control"],
            "mean_test": stats["mean_test"],
            "mean_delta": improvement,
            "p_one_tailed": stats["p_one_tailed"],
            "alpha": alpha,
            "sample_balance": sample_balance,
            "assignment_method": assignment_method,
        }

    def pause(self, db: Session) -> dict[str, Any]:
        config = self._load_config(db)
        config["enabled"] = False
        self._persist_config(db, config)
        self._log_event(
            db,
            event_type="auto_tuner_paused",
            message="Auto-tuner paused",
            data={},
        )
        db.commit()
        return {"ok": True, "enabled": False}

    def resume(self, db: Session) -> dict[str, Any]:
        config = self._load_config(db)
        config["enabled"] = True
        self._persist_config(db, config)
        self._log_event(
            db,
            event_type="auto_tuner_resumed",
            message="Auto-tuner resumed",
            data={},
        )
        db.commit()
        return {"ok": True, "enabled": True}

    def rollback_current(self, db: Session) -> dict[str, Any]:
        config = self._load_config(db)
        state = self._load_state(db)
        if state.get("current_step_index") is None:
            return {"ok": False, "reason": "no active step"}
        sequence = list(config.get("sequence", []))
        idx = int(state["current_step_index"])
        if idx >= len(sequence):
            return {"ok": False, "reason": "step index out of range"}
        step = sequence[idx]
        is_ab = str(state.get("mode", "")) == "ab_test"
        # In AB mode il patch non era mai stato applicato a `risk`: control e test
        # erano iniettati per-segnale via overlay. Quindi non c'è nulla da revertire
        # — basta chiudere lo step.
        if not is_ab:
            target_setting = str(step.get("target_setting", "risk"))
            self._apply_patch(
                db, dict(step.get("revert", {})), target_setting=target_setting
            )
        history: list[dict[str, Any]] = list(state.get("step_history", []))
        history.append(
            {
                "key": step.get("key"),
                "label": step.get("label"),
                "status": "reverted",
                "mode": "ab_test" if is_ab else "sequential",
                "metrics_before": state.get("metrics_before"),
                "metrics_after": None,
                "trades_evaluated": 0,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "manual": True,
            }
        )
        new_state = dict(state)
        new_state.update(
            {
                "current_step_index": None,
                "current_step_key": None,
                "activated_at": None,
                "trades_at_activation": None,
                "metrics_before": None,
                "risk_snapshot_before": None,
                "ab_test": None,
                "step_history": history,
            }
        )
        self._persist_state(db, new_state)
        self._log_event(
            db,
            event_type="auto_tuner_manual_rollback",
            message=f"Auto-tuner manual rollback of step '{step.get('key')}'",
            data={"step": step.get("key"), "mode": "ab_test" if is_ab else "sequential"},
        )
        db.commit()
        return {"ok": True, "reverted_step": step.get("key")}

    def reset(self, db: Session) -> dict[str, Any]:
        state = self._load_state(db)
        if state.get("current_step_index") is not None:
            self.rollback_current(db)
        empty: dict[str, Any] = {"step_history": []}
        self._persist_state(db, empty)
        self._log_event(
            db,
            event_type="auto_tuner_reset",
            message="Auto-tuner state reset",
            data={},
        )
        db.commit()
        return {"ok": True}

    def status(self, db: Session) -> dict[str, Any]:
        config = self._load_config(db)
        state = self._load_state(db)
        sequence = list(config.get("sequence", []))
        completed_keys = {
            entry.get("key")
            for entry in state.get("step_history", [])
            if entry.get("status") in {"success", "reverted", "skipped", "promoted", "rejected"}
        }
        current_step_index = state.get("current_step_index")
        mode = str(config.get("mode", "sequential"))
        next_index = self._next_step_index(
            sequence, completed_keys, skip_sequential_only=(mode == "ab_test")
        )
        if current_step_index is not None:
            next_index = int(current_step_index) + 1
        positions = self.analyzer.closed_paper_positions(db, limit=2000)
        trades_now = len(positions)
        progress: dict[str, Any] | None = None
        ab_progress: dict[str, Any] | None = None
        metrics_now: dict[str, Any] | None = None
        if state.get("current_step_index") is not None:
            if str(state.get("mode", "")) == "ab_test":
                step_key = str(state.get("current_step_key") or "")
                arms = self._partition_by_arm(positions, state.get("activated_at"), step_key)
                control_pnls = [
                    float(p.realized_pnl)
                    for p in arms["control"]
                    if p.realized_pnl is not None
                ]
                test_pnls = [
                    float(p.realized_pnl)
                    for p in arms["test"]
                    if p.realized_pnl is not None
                ]
                n_control = len(control_pnls)
                n_test = len(test_pnls)
                sample_balance = (
                    min(n_control, n_test) / max(n_control, n_test)
                    if n_control and n_test
                    else 0.0
                )
                mean_control = sum(control_pnls) / n_control if n_control else 0.0
                mean_test = sum(test_pnls) / n_test if n_test else 0.0
                raw_ab_state = state.get("ab_test")
                ab_state: dict[str, Any] = raw_ab_state if isinstance(raw_ab_state, dict) else {}
                ab_progress = {
                    "trades_control": n_control,
                    "trades_test": n_test,
                    "trades_required_per_arm": int(
                        config.get("ab_test_min_trades_per_arm", 30)
                    ),
                    "sample_balance": sample_balance,
                    "mean_control": mean_control,
                    "mean_test": mean_test,
                    "mean_delta": mean_test - mean_control,
                    "alpha": float(config.get("ab_test_significance_level", 0.05)),
                    "assignment_method": ab_state.get(
                        "assignment_method", "deterministic_hash"
                    ),
                    "control_overlay": ab_state.get("control_overlay", {}),
                    "test_overlay": ab_state.get("test_overlay", {}),
                    "active_overlay": ab_state.get("test_overlay", {}),
                }
            else:
                raw_trades_at_activation = state.get("trades_at_activation", trades_now)
                try:
                    trades_at_activation = int(raw_trades_at_activation)
                except (TypeError, ValueError):
                    trades_at_activation = trades_now
                delta = max(0, trades_now - trades_at_activation)
                progress = {
                    "trades_done": delta,
                    "trades_required": int(config.get("min_trades_per_step", 50)),
                }
                post_positions = self._positions_after(positions, state.get("activated_at"))
                metrics_now = self._metrics_for(post_positions) if post_positions else None
        return {
            "enabled": bool(config.get("enabled", False)),
            "mode": mode,
            "current_step_key": state.get("current_step_key"),
            "current_step_index": state.get("current_step_index"),
            "current_step_mode": state.get("mode"),
            "activated_at": state.get("activated_at"),
            "metrics_before": state.get("metrics_before"),
            "metrics_now": metrics_now,
            "ab_test_state": state.get("ab_test"),
            "next_step_key": sequence[next_index]["key"] if next_index is not None and next_index < len(sequence) else None,
            "trades_now": trades_now,
            "progress": progress,
            "ab_progress": ab_progress,
            "step_history": state.get("step_history", []),
            "config": {
                "min_trades_per_step": int(config.get("min_trades_per_step", 50)),
                "improvement_threshold": float(config.get("improvement_threshold", 0.0)),
                "mode": mode,
                "ab_test_min_trades_per_arm": int(
                    config.get("ab_test_min_trades_per_arm", 30)
                ),
                "ab_test_significance_level": float(
                    config.get("ab_test_significance_level", 0.05)
                ),
                "ab_test_allocation": float(config.get("ab_test_allocation", 0.5)),
                "ab_test_assignment_salt": str(
                    config.get("ab_test_assignment_salt", "trado-v1")
                ),
            },
            "available_source_ab_sequence": SOURCE_AB_SEQUENCE,
        }

    def source_tests(self, db: Session) -> dict[str, Any]:
        config = self._load_config(db)
        state = self._load_state(db)
        disabled_sources = self._disabled_signal_sources(db)
        active_key = state.get("current_step_key")
        active_mode = state.get("mode") or config.get("mode")
        tests = []
        for step in SOURCE_AB_SEQUENCE:
            key = str(step.get("key"))
            source = key.replace("_source", "")
            dynamic_step = self._source_test_step(source, disabled_sources)
            tests.append(
                {
                    "source": source,
                    "key": key,
                    "label": step.get("label"),
                    "control_overlay": dynamic_step.get("revert", {}),
                    "test_overlay": dynamic_step.get("patch", {}),
                    "currently_disabled": source in disabled_sources,
                    "active": active_key == key,
                    "available": active_key in (None, key),
                }
            )
        return {
            "enabled": bool(config.get("enabled", False)),
            "mode": str(config.get("mode", "sequential")),
            "active_step_key": active_key,
            "active_mode": active_mode,
            "blocked": active_key is not None,
            "tests": tests,
        }

    def start_source_test(self, db: Session, source: str) -> dict[str, Any]:
        normalized = source.strip().lower().replace("-", "_")
        step = next(
            (
                item
                for item in SOURCE_AB_SEQUENCE
                if str(item.get("key")) == f"{normalized}_source"
                or str(item.get("key")).replace("_source", "") == normalized
            ),
            None,
        )
        if step is None:
            return {
                "ok": False,
                "status": "unknown_source",
                "source": source,
                "available_sources": [
                    str(item.get("key", "")).replace("_source", "")
                    for item in SOURCE_AB_SEQUENCE
                ],
            }

        state = self._load_state(db)
        active_key = state.get("current_step_key")
        if active_key is not None:
            return {
                "ok": False,
                "status": "conflict",
                "active_step_key": active_key,
                "requested_step_key": step.get("key"),
                "recommended_actions": ["wait", "rollback", "reset"],
            }

        config = self._load_config(db)
        disabled_sources = self._disabled_signal_sources(db)
        dynamic_step = self._source_test_step(normalized, disabled_sources)
        config.update(
            {
                "enabled": True,
                "mode": "ab_test",
                "sequence": [deepcopy(dynamic_step)],
            }
        )
        self._persist_config(db, config)
        result = self._start_ab_step(db, state, config, [dynamic_step], 0)
        result.update({"ok": True, "source": normalized})
        return result

    def _disabled_signal_sources(self, db: Session) -> list[str]:
        risk = self._snapshot_risk(db)
        disabled_sources = risk.get("disabled_signal_sources", [])
        if not isinstance(disabled_sources, list):
            return []
        return [str(item) for item in disabled_sources]

    def _source_test_step(
        self, source: str, disabled_sources: list[str]
    ) -> dict[str, Any]:
        template = next(
            (
                item
                for item in SOURCE_AB_SEQUENCE
                if str(item.get("key")).replace("_source", "") == source
            ),
            None,
        )
        control_sources = list(dict.fromkeys(disabled_sources))
        test_sources = [item for item in control_sources if item != source]
        return {
            "key": f"{source}_source",
            "label": template.get("label") if template else f"{source} source",
            "patch": {"disabled_signal_sources": test_sources},
            "revert": {"disabled_signal_sources": control_sources},
        }

    def _metrics_for(self, positions: list[Position]) -> dict[str, Any]:
        if not positions:
            return {"trades": 0, "win_rate": 0.0, "equity_slope": 0.0, "mean_pnl": 0.0}
        stats = self.analyzer.summarize_positions(positions)
        mean_pnl = stats.realized_pnl / stats.trades if stats.trades else 0.0
        return {
            "trades": stats.trades,
            "win_rate": stats.win_rate,
            "realized_pnl": stats.realized_pnl,
            "mean_pnl": mean_pnl,
            "profit_factor": stats.profit_factor,
            "equity_slope": equity_slope(positions),
        }

    def _partition_by_arm(
        self,
        positions: list[Position],
        activated_at_iso: str | None,
        step_key: str,
    ) -> dict[str, list[Position]]:
        out: dict[str, list[Position]] = {"control": [], "test": []}
        if not activated_at_iso:
            return out
        try:
            cutoff = datetime.fromisoformat(activated_at_iso)
        except ValueError:
            return out
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        for position in positions:
            closed_at = position.closed_at
            if closed_at is None:
                continue
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
            if closed_at <= cutoff:
                continue
            meta = position.metadata_json or {}
            tag = meta.get("_auto_tuner")
            if not isinstance(tag, dict):
                continue
            if tag.get("step_key") != step_key:
                continue
            arm = tag.get("arm")
            if arm in out:
                out[arm].append(position)
        return out

    @staticmethod
    def _welch_t_test(control: list[float], test: list[float]) -> dict[str, float]:
        # Welch's t-test (varianza non assunta uguale). p-value calcolato con
        # approssimazione normale del CDF di Student: per n ≥ ~20 per arm
        # l'errore è trascurabile rispetto al rumore dei PnL.
        n_c = len(control)
        n_t = len(test)
        if n_c < 2 or n_t < 2:
            return {
                "mean_control": (sum(control) / n_c) if n_c else 0.0,
                "mean_test": (sum(test) / n_t) if n_t else 0.0,
                "std_control": 0.0,
                "std_test": 0.0,
                "t": 0.0,
                "df": 0.0,
                "p_one_tailed": 1.0,
            }
        mean_c = sum(control) / n_c
        mean_t = sum(test) / n_t
        var_c = sum((x - mean_c) ** 2 for x in control) / (n_c - 1)
        var_t = sum((x - mean_t) ** 2 for x in test) / (n_t - 1)
        se_sq = var_c / n_c + var_t / n_t
        if se_sq <= 0:
            return {
                "mean_control": mean_c,
                "mean_test": mean_t,
                "std_control": math.sqrt(var_c),
                "std_test": math.sqrt(var_t),
                "t": 0.0,
                "df": float(n_c + n_t - 2),
                "p_one_tailed": 1.0,
            }
        se = math.sqrt(se_sq)
        t_stat = (mean_t - mean_c) / se
        num = se_sq ** 2
        denom = ((var_c / n_c) ** 2) / (n_c - 1) + ((var_t / n_t) ** 2) / (n_t - 1)
        df = num / denom if denom > 0 else float(n_c + n_t - 2)
        # P(T > t) ≈ 1 - Φ(t), normal approx.
        p_one_tailed = 0.5 * math.erfc(t_stat / math.sqrt(2))
        return {
            "mean_control": mean_c,
            "mean_test": mean_t,
            "std_control": math.sqrt(var_c),
            "std_test": math.sqrt(var_t),
            "t": t_stat,
            "df": df,
            "p_one_tailed": p_one_tailed,
        }

    def _positions_after(self, positions: list[Position], activated_at_iso: str | None) -> list[Position]:
        if not activated_at_iso:
            # Stato corrotto: senza activated_at non possiamo identificare le posizioni
            # post-attivazione, quindi non valutiamo (ritornando lista vuota teniamo lo
            # step in "in_progress" finché lo state non viene riparato/resettato).
            return []
        try:
            cutoff = datetime.fromisoformat(activated_at_iso)
        except ValueError:
            return []
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        result: list[Position] = []
        for position in positions:
            closed_at = position.closed_at
            if closed_at is None:
                continue
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
            if closed_at > cutoff:
                result.append(position)
        return result

    def _apply_patch(
        self,
        db: Session,
        patch: dict[str, Any],
        target_setting: str = "risk",
    ) -> None:
        setting = db.get(AppSetting, target_setting) or AppSetting(
            key=target_setting, value={}
        )
        current = dict(setting.value) if isinstance(setting.value, dict) else {}
        current.update(patch)
        setting.value = current
        db.add(setting)

    def _snapshot_risk(self, db: Session) -> dict[str, Any]:
        risk_setting = db.get(AppSetting, "risk")
        if risk_setting is None or not isinstance(risk_setting.value, dict):
            return {}
        return deepcopy(risk_setting.value)

    def _load_config(self, db: Session) -> dict[str, Any]:
        setting = db.get(AppSetting, "auto_tuner_config")
        value = dict(setting.value) if setting and isinstance(setting.value, dict) else {}
        merged = deepcopy(DEFAULT_CONFIG)
        for key, override in value.items():
            merged[key] = override
        return merged

    def _persist_config(self, db: Session, config: dict[str, Any]) -> None:
        setting = db.get(AppSetting, "auto_tuner_config") or AppSetting(
            key="auto_tuner_config", value={}
        )
        setting.value = config
        db.add(setting)

    def _load_state(self, db: Session) -> dict[str, Any]:
        setting = db.get(AppSetting, "auto_tuner_state")
        if setting is None or not isinstance(setting.value, dict):
            return {"step_history": []}
        return deepcopy(setting.value)

    def _persist_state(self, db: Session, state: dict[str, Any]) -> None:
        setting = db.get(AppSetting, "auto_tuner_state") or AppSetting(
            key="auto_tuner_state", value={}
        )
        setting.value = state
        db.add(setting)

    def _log_event(
        self,
        db: Session,
        *,
        event_type: str,
        message: str,
        data: dict[str, Any],
    ) -> None:
        db.add(DecisionLog(event_type=event_type, message=message, data=data))

    def _finish(self, db: Session, state: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
        new_state = dict(state)
        new_state["current_step_index"] = None
        new_state["current_step_key"] = None
        self._persist_state(db, new_state)
        self._log_event(
            db,
            event_type="auto_tuner_finished",
            message=f"Auto-tuner finished: {reason}",
            data={"status": status, "reason": reason},
        )
        db.commit()
        return {"status": status, "reason": reason}
