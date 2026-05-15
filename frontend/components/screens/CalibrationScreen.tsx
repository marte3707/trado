"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Icon, fmt } from "@/components/atoms";
import { AppSettings, Calibration, CalibrationChange, CalibrationHistoryEntry, apiGet, apiPost } from "@/lib/api";

interface Props {
  calibration: Calibration;
  history: CalibrationHistoryEntry[];
  settings: AppSettings;
  onRefresh: () => void | Promise<void>;
}

function statusBadge(entry: CalibrationHistoryEntry) {
  if (entry.event_type === "auto_calibration_applied") return <span className="badge long">applied</span>;
  if (entry.event_type === "auto_calibration_failed") return <span className="badge short">failed</span>;
  if (entry.event_type === "auto_calibration_skipped") return <span className="badge warn">skipped</span>;
  return <span className="badge info">checked</span>;
}

function pct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function num(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(2);
}

function changeLabel(change: CalibrationChange) {
  if (change.target === "risk_setting") {
    if (change.key === "min_signal_score") return "Score minimo";
    if (change.key === "min_risk_reward") return "Risk/reward minimo";
    if (change.key === "max_risk_per_trade_pct") return "Rischio per trade";
  }
  const [timeframe, strategy] = change.key.split(":");
  if (change.target === "strategy_weight") return `${strategy ?? change.key} weight ${timeframe ?? ""}`.trim();
  if (change.target === "strategy_params") return `${strategy ?? change.key} params ${timeframe ?? ""}`.trim();
  return change.key;
}

function valueText(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, val]) => `${key}: ${valueText(val)}`)
      .join(", ");
  }
  return String(value);
}

function changeTone(change: CalibrationChange) {
  if (change.target === "risk_setting") return "warn";
  if (change.target === "strategy_weight") return Number(change.after) > Number(change.before) ? "long" : "short";
  return "info";
}

function changeImpact(change: CalibrationChange) {
  if (change.target === "risk_setting") {
    if (change.key === "min_signal_score") return "filtro segnali reso più selettivo";
    if (change.key === "min_risk_reward") return "ingressi con reward minimo più alto";
    if (change.key === "max_risk_per_trade_pct") return "rischio per posizione ridotto";
  }
  if (change.target === "strategy_weight") {
    return Number(change.after) > Number(change.before)
      ? "strategia premiata nello scoring"
      : "strategia penalizzata nello scoring";
  }
  if (change.target === "strategy_params") return "parametri resi più selettivi o ripristinati";
  return change.reason;
}

function lastRunText(calibration: Calibration) {
  const raw = calibration.state?.last_run_at;
  return typeof raw === "string" ? fmt.relTime(raw) : "mai";
}

export function CalibrationScreen({ calibration, history, settings, onRefresh }: Props) {
  const [localCalibration, setLocalCalibration] = useState(calibration);
  const [localHistory, setLocalHistory] = useState(history);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const enabled = settings.auto_calibration?.enabled !== false;
  const lastStatus = String(localCalibration.state?.last_status ?? "nessun run");

  useEffect(() => setLocalCalibration(calibration), [calibration]);
  useEffect(() => setLocalHistory(history), [history]);

  const appliedCount = useMemo(
    () => localHistory.filter(entry => entry.event_type === "auto_calibration_applied").length,
    [localHistory]
  );
  const failedCount = useMemo(
    () => localHistory.filter(entry => entry.event_type === "auto_calibration_failed" || entry.event_type === "auto_calibration_skipped").length,
    [localHistory]
  );

  async function runNow() {
    setBusy(true);
    setError(null);
    try {
      await apiPost<Calibration>("/api/auto-calibration/run");
      const nextCalibration = await apiGet<Calibration>("/api/analytics/calibration");
      const nextHistory = await apiGet<CalibrationHistoryEntry[]>("/api/analytics/calibration/history");
      setLocalCalibration(nextCalibration);
      setLocalHistory(nextHistory);
      await onRefresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Errore calibrazione");
    } finally {
      setBusy(false);
    }
  }

  const appliedChanges = Array.isArray(localCalibration.state?.last_changes)
    ? localCalibration.state.last_changes as CalibrationChange[]
    : [];
  const proposedChanges = localCalibration.changes;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Calibration</h1>
          <div className="page-sub">
            {enabled ? "attiva" : "disattivata"} · ultimo stato {lastStatus} · ultimo run {lastRunText(localCalibration)}
          </div>
        </div>
        <div className="page-actions">
          <button className="btn ghost" onClick={onRefresh}><Icon name="refresh" size={12} /></button>
          <button className="btn primary" disabled={busy} onClick={runNow}>
            <Icon name="refresh" size={12} /> {busy ? "Calibrazione..." : "Calibra ora"}
          </button>
        </div>
      </div>

      {error && (
        <div style={{ padding: "8px 14px", marginBottom: "var(--gap)", background: "var(--short-soft)", border: "1px solid var(--short-border)", borderRadius: "var(--radius)", color: "var(--short)", fontSize: "var(--fz-sm)" }}>
          {error}
        </div>
      )}

      <div className="stat-grid" style={{ marginBottom: "var(--gap)" }}>
        <div className="stat">
          <div className="stat-label">Trade letti</div>
          <div className="stat-value">{localCalibration.overall.trades}</div>
          <div className="stat-delta">min {String(localCalibration.settings.min_closed_trades ?? "—")}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Win rate</div>
          <div className="stat-value">{pct(localCalibration.overall.win_rate)}</div>
          <div className="stat-delta">{localCalibration.overall.wins} win · {localCalibration.overall.losses} loss</div>
        </div>
        <div className="stat">
          <div className="stat-label">PnL realizzato</div>
          <div className={"stat-value " + (localCalibration.overall.realized_pnl >= 0 ? "up" : "down")}>
            {fmt.pnl(localCalibration.overall.realized_pnl)}
          </div>
          <div className="stat-delta">PF {num(localCalibration.overall.profit_factor)}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Run loggati</div>
          <div className="stat-value">{localHistory.length}</div>
          <div className="stat-delta">{appliedCount} applicati · {failedCount} saltati/falliti</div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 380px", gap: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Modifiche applicate nell&apos;ultimo run</span>
            <span className="badge muted">{appliedChanges.length} cambi</span>
          </div>
          {appliedChanges.length === 0 ? (
            <div className="card-body muted" style={{ fontSize: "var(--fz-sm)" }}>
              Nessuna modifica applicata da una calibrazione salvata.
            </div>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Dove</th>
                  <th>Prima</th>
                  <th>Dopo</th>
                  <th>Effetto</th>
                </tr>
              </thead>
              <tbody>
                {appliedChanges.map((change, index) => (
                  <tr key={`${change.target}-${change.key}-${index}`}>
                    <td className="fg">
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span className={"badge " + changeTone(change)}>{change.target.replace("_", " ")}</span>
                        <span>{changeLabel(change)}</span>
                      </div>
                    </td>
                    <td>{valueText(change.before)}</td>
                    <td>{valueText(change.after)}</td>
                    <td>{changeImpact(change)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <div className="card-head"><span className="title">Motivo ultimo run</span></div>
          <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <div className="stat-label">Reason</div>
              <div style={{ color: "var(--fg)", fontSize: "var(--fz-md)" }}>
                {String(localCalibration.state?.last_reason ?? localCalibration.reason)}
              </div>
            </div>
            <div>
              <div className="stat-label">Policy attuale</div>
              <div className="mono" style={{ color: "var(--fg-2)", fontSize: "var(--fz-sm)", lineHeight: 1.8 }}>
                score ≥ {String(settings.risk?.min_signal_score ?? "—")}<br />
                RR ≥ {String(settings.risk?.min_risk_reward ?? "—")}<br />
                risk/trade {String(settings.risk?.max_risk_per_trade_pct ?? "—")}%
              </div>
            </div>
          </div>
        </div>

        <div className="card" style={{ gridColumn: "1 / -1" }}>
          <div className="card-head">
            <span className="title">Preview non applicata</span>
            <span className="badge info">{proposedChanges.length} proposte</span>
            <div className="right">Queste righe descrivono cosa cambierebbe premendo Calibra ora.</div>
          </div>
          {proposedChanges.length === 0 ? (
            <div className="card-body muted" style={{ fontSize: "var(--fz-sm)" }}>
              Nessuna proposta pendente dalla preview corrente.
            </div>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Dove</th>
                  <th>Ora</th>
                  <th>Proposto</th>
                  <th>Effetto</th>
                </tr>
              </thead>
              <tbody>
                {proposedChanges.map((change, index) => (
                  <tr key={`preview-${change.target}-${change.key}-${index}`}>
                    <td className="fg">
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span className={"badge " + changeTone(change)}>{change.target.replace("_", " ")}</span>
                        <span>{changeLabel(change)}</span>
                      </div>
                    </td>
                    <td>{valueText(change.before)}</td>
                    <td>{valueText(change.after)}</td>
                    <td>{changeImpact(change)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card" style={{ gridColumn: "1 / -1" }}>
          <div className="card-head">
            <span className="title">Log calibrazione</span>
            <span className="badge muted">{localHistory.length} eventi</span>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th>Quando</th>
                <th>Stato</th>
                <th>Trade</th>
                <th>Win rate</th>
                <th>PnL</th>
                <th>Modifiche</th>
                <th>Motivo / errore</th>
              </tr>
            </thead>
            <tbody>
              {localHistory.map(entry => (
                <tr key={entry.id}>
                  <td className="mono">{fmt.relTime(entry.timestamp)}</td>
                  <td>{statusBadge(entry)}</td>
                  <td className="num">{entry.overall?.trades ?? "—"}</td>
                  <td className="num">{pct(entry.overall?.win_rate)}</td>
                  <td className={entry.overall && entry.overall.realized_pnl >= 0 ? "up num" : "down num"}>
                    {entry.overall ? fmt.pnl(entry.overall.realized_pnl) : "—"}
                  </td>
                  <td className="num">{entry.changes?.length ?? 0}</td>
                  <td>{entry.error ?? entry.reason ?? entry.message}</td>
                </tr>
              ))}
              {localHistory.length === 0 && (
                <tr>
                  <td colSpan={7} style={{ textAlign: "center", color: "var(--fg-3)", height: 72 }}>
                    Nessuna calibrazione ancora registrata.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
