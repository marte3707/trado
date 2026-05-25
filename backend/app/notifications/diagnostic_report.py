from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.db.models import Signal
from app.engine.diagnostics import build_diagnostics_report
from app.engine.performance import SignalPerformanceAnalyzer, serialize_stats

if TYPE_CHECKING:
    from app.notifications.telegram import TelegramNotifier


def _pnl_str(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}"


def _pct_str(v: float | None) -> str:
    if v is None or not isinstance(v, (int, float)):
        return "—"
    return f"{v * 100:.1f}%"


def _pf_str(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}"


def _slope_label(s: float) -> str:
    if s >= 2:
        return "forte crescita"
    if s >= 0.5:
        return "crescita"
    if s >= -0.5:
        return "stabile"
    if s >= -2:
        return "declino"
    return "forte declino"


class DiagnosticReporter:
    def build_report(self, db: Session, lookback: int = 200) -> dict[str, Any]:
        diag = build_diagnostics_report(db, lookback=lookback, history_limit=1)

        analyzer = SignalPerformanceAnalyzer()
        positions = analyzer.closed_paper_positions(db, limit=lookback)

        signal_ids = {
            int(p.metadata_json["source_signal_id"])
            for p in positions
            if isinstance(p.metadata_json, dict)
            and p.metadata_json.get("source_signal_id") is not None
        }
        signals_by_id = (
            {s.id: s for s in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
            if signal_ids
            else {}
        )

        by_direction: dict[str, list] = {}
        for position in positions:
            meta = position.metadata_json if isinstance(position.metadata_json, dict) else {}
            sid_raw = meta.get("source_signal_id")
            if sid_raw is None:
                continue
            try:
                signal = signals_by_id.get(int(sid_raw))
            except (TypeError, ValueError):
                signal = None
            if signal is not None:
                by_direction.setdefault(signal.direction, []).append(position)

        by_direction_stats = {
            direction: serialize_stats(analyzer.summarize_positions(pos_list))
            for direction, pos_list in by_direction.items()
        }

        overall = diag.get("overall", {})
        calibration_state = diag.get("calibration_state", {})

        trades = overall.get("trades", 0)
        losses = overall.get("losses", 0)
        sl_rate = losses / trades if trades else 0.0

        anomalies: list[str] = []
        if sl_rate > 0.80:
            anomalies.append(f"Tasso SL: {sl_rate:.0%} (soglia: 80%)")

        long_st = by_direction_stats.get("LONG", {})
        short_st = by_direction_stats.get("SHORT", {})
        long_wr = long_st.get("win_rate", 0.0) if long_st else 0.0
        short_wr = short_st.get("win_rate", 0.0) if short_st else 0.0
        if long_wr < 0.20 and short_wr > 0.40:
            anomalies.append("Bias LONG in mercato ribassista")

        equity_slope_val = diag.get("equity_slope", 0.0)
        last_recent_slope = float(calibration_state.get("last_recent_slope", 0.0))
        if equity_slope_val < 0 and last_recent_slope < equity_slope_val * 1.5:
            anomalies.append("Slope recente peggiora rispetto al trend globale")

        if calibration_state.get("deep_defensive_active", False):
            anomalies.append("Deep defensive attiva")

        by_strategy = diag.get("by_strategy", {})
        eligible = {k: v for k, v in by_strategy.items() if v.get("trades", 0) >= 3}
        sorted_by_pf = sorted(
            eligible.items(),
            key=lambda x: x[1].get("profit_factor") or 0.0,
            reverse=True,
        )
        top = sorted_by_pf[:2]
        bottom = sorted_by_pf[-2:] if len(sorted_by_pf) > 2 else []

        return {
            **diag,
            "by_direction": by_direction_stats,
            "anomalies": anomalies,
            "top_strategies": [{"name": k, **v} for k, v in top],
            "bottom_strategies": [{"name": k, **v} for k, v in bottom],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def format_telegram(self, report: dict[str, Any]) -> str:
        now = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        trades = report.get("trade_count", 0)
        overall = report.get("overall", {})
        calibration_state = report.get("calibration_state", {})

        equity_slope_val = report.get("equity_slope", 0.0)
        recent_slope = float(calibration_state.get("last_recent_slope", 0.0))
        deep_defensive = calibration_state.get("deep_defensive_active", False)
        pos_streak = calibration_state.get("deep_defensive_positive_streak", 0)
        recent_streak = calibration_state.get("deep_defensive_recent_positive_streak", 0)
        zero_cycles = calibration_state.get("consecutive_zero_signal_cycles", 0)
        min_score = (
            report.get("baseline_vs_current", {})
            .get("risk", {})
            .get("min_signal_score", {})
            .get("current", "—")
        )

        by_direction = report.get("by_direction", {})
        long_st = by_direction.get("LONG", {})
        short_st = by_direction.get("SHORT", {})
        long_trades = long_st.get("trades", 0)
        short_trades = short_st.get("trades", 0)
        total_side = long_trades + short_trades
        long_pct = long_trades / total_side if total_side else 0.0

        if long_pct > 0.65:
            bias_label = "⚠️ BIAS LONG"
        elif long_pct < 0.35:
            bias_label = "⚠️ BIAS SHORT"
        else:
            bias_label = "bilanciato"

        defensive_label = "deep defensive ATTIVA" if deep_defensive else "normale"
        top_strategies = report.get("top_strategies", [])
        bottom_strategies = report.get("bottom_strategies", [])
        anomalies = report.get("anomalies", [])

        lines = [
            f"📊 <b>Report diagnostico — {now}</b>",
            f"Finestra: {trades} trade paper chiusi",
            "",
            "📈 <b>Performance globale</b>",
            (
                f"WR {_pct_str(overall.get('win_rate'))} · "
                f"PF {_pf_str(overall.get('profit_factor'))} · "
                f"PnL <code>{_pnl_str(overall.get('realized_pnl'))}</code>"
            ),
            "",
            f"↕️ <b>Bias direzionale</b>    {bias_label}",
            f"Long  {long_trades}t  WR {_pct_str(long_st.get('win_rate'))}  <code>{_pnl_str(long_st.get('realized_pnl'))}</code>",
            f"Short {short_trades}t  WR {_pct_str(short_st.get('win_rate'))}  <code>{_pnl_str(short_st.get('realized_pnl'))}</code>",
            "",
            "📉 <b>Equity slope</b>",
            f"Globale: <code>{equity_slope_val:.1f}</code> ({_slope_label(equity_slope_val)})",
            f"Recente: <code>{recent_slope:.1f}</code> ({_slope_label(recent_slope)})",
            "",
            "⚙️ <b>Stato calibrazione</b>",
            f"{defensive_label} · score min {min_score}",
            f"Streak positivi: {pos_streak}/2 (globale) · {recent_streak}/3 (recente)",
            f"Zero signal cycles: {zero_cycles}",
        ]

        if top_strategies:
            lines += ["", "🏆 <b>Migliori strategie (PF)</b>"]
            for st in top_strategies:
                lines.append(
                    f"  {st['name']}  PF {_pf_str(st.get('profit_factor'))}  WR {_pct_str(st.get('win_rate'))}"
                )

        if bottom_strategies:
            lines += ["", "⚠️ <b>Peggiori strategie</b>"]
            for st in bottom_strategies:
                lines.append(
                    f"  {st['name']}  PF {_pf_str(st.get('profit_factor'))}  WR {_pct_str(st.get('win_rate'))}"
                )

        if anomalies:
            lines += ["", "🚨 <b>Anomalie rilevate</b>"]
            for a in anomalies:
                lines.append(f"• {a}")

        lines += ["", "🤖 <i>Trado · Diagnostics</i>"]
        return "\n".join(lines)

    async def send(
        self,
        db: Session,
        telegram: "TelegramNotifier",
        lookback: int = 200,
    ) -> dict[str, Any]:
        report = self.build_report(db, lookback)
        text = self.format_telegram(report)
        await telegram._send_text(text)
        return report
