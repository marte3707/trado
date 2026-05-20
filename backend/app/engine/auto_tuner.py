from __future__ import annotations

import math
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
]

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "min_trades_per_step": 50,
    "improvement_threshold": 0.0,
    # Quando > 0, la soglia di promozione viene calcolata come max(improvement_threshold,
    # adaptive_threshold_multiplier × std(pnl)) — riduce i falsi successi.
    "adaptive_threshold_multiplier": 0.5,
    "sequence": DEFAULT_SEQUENCE,
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

        history: list[dict[str, Any]] = list(state.get("step_history", []))
        completed_keys = {entry.get("key") for entry in history if entry.get("status") in {"success", "reverted", "skipped"}}

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

    def _next_step_index(self, sequence: list[dict[str, Any]], completed_keys: set) -> int | None:
        for index, step in enumerate(sequence):
            if step.get("key") not in completed_keys:
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
        self._apply_patch(db, dict(step.get("patch", {})))
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
        if adaptive_multiplier > 0:
            pnl_values = [
                float(p.realized_pnl) for p in post_positions if p.realized_pnl is not None
            ]
            if len(pnl_values) >= 2:
                mean = sum(pnl_values) / len(pnl_values)
                variance = sum((x - mean) ** 2 for x in pnl_values) / (len(pnl_values) - 1)
                std_pnl = math.sqrt(variance)
                adaptive_threshold = adaptive_multiplier * std_pnl
                threshold = max(threshold, adaptive_threshold)
        slope_before = float(metrics_before.get("equity_slope", 0.0))
        slope_after = float(metrics_after.get("equity_slope", 0.0))
        improved = slope_after >= slope_before + threshold

        outcome = "success" if improved else "reverted"
        if not improved:
            self._apply_patch(db, dict(step.get("revert", {})))

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
        self._apply_patch(db, dict(step.get("revert", {})))
        history: list[dict[str, Any]] = list(state.get("step_history", []))
        history.append(
            {
                "key": step.get("key"),
                "label": step.get("label"),
                "status": "reverted",
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
                "step_history": history,
            }
        )
        self._persist_state(db, new_state)
        self._log_event(
            db,
            event_type="auto_tuner_manual_rollback",
            message=f"Auto-tuner manual rollback of step '{step.get('key')}'",
            data={"step": step.get("key")},
        )
        db.commit()
        return {"ok": True, "reverted_step": step.get("key")}

    def reset(self, db: Session) -> dict[str, Any]:
        state = self._load_state(db)
        if state.get("current_step_index") is not None:
            self.rollback_current(db)
        empty = {"step_history": []}
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
            if entry.get("status") in {"success", "reverted", "skipped"}
        }
        next_index = self._next_step_index(sequence, completed_keys)
        positions = self.analyzer.closed_paper_positions(db, limit=2000)
        trades_now = len(positions)
        progress: dict[str, Any] | None = None
        if state.get("current_step_index") is not None:
            trades_at_activation = int(state.get("trades_at_activation", trades_now))
            delta = max(0, trades_now - trades_at_activation)
            progress = {
                "trades_done": delta,
                "trades_required": int(config.get("min_trades_per_step", 50)),
            }
        return {
            "enabled": bool(config.get("enabled", False)),
            "current_step_key": state.get("current_step_key"),
            "current_step_index": state.get("current_step_index"),
            "activated_at": state.get("activated_at"),
            "metrics_before": state.get("metrics_before"),
            "next_step_key": sequence[next_index]["key"] if next_index is not None and next_index < len(sequence) else None,
            "trades_now": trades_now,
            "progress": progress,
            "step_history": state.get("step_history", []),
            "config": {
                "min_trades_per_step": int(config.get("min_trades_per_step", 50)),
                "improvement_threshold": float(config.get("improvement_threshold", 0.0)),
            },
        }

    def _metrics_for(self, positions: list[Position]) -> dict[str, Any]:
        if not positions:
            return {"trades": 0, "win_rate": 0.0, "equity_slope": 0.0}
        stats = self.analyzer.summarize_positions(positions)
        return {
            "trades": stats.trades,
            "win_rate": stats.win_rate,
            "realized_pnl": stats.realized_pnl,
            "profit_factor": stats.profit_factor,
            "equity_slope": equity_slope(positions),
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

    def _apply_patch(self, db: Session, patch: dict[str, Any]) -> None:
        risk_setting = db.get(AppSetting, "risk") or AppSetting(key="risk", value={})
        current = dict(risk_setting.value) if isinstance(risk_setting.value, dict) else {}
        current.update(patch)
        risk_setting.value = current
        db.add(risk_setting)

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
