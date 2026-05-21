"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Icon, fmt } from "@/components/atoms";
import {
  AppSettings,
  AutoTunerStatus,
  Calibration,
  CalibrationChange,
  CalibrationHistoryEntry,
  SignalRejections,
  TunerStepHistory,
  WorkerCycle,
  apiPost,
  apiGet,
} from "@/lib/api";

interface Props {
  calibration: Calibration;
  history: CalibrationHistoryEntry[];
  settings: AppSettings;
  tunerStatus: AutoTunerStatus | null;
  workerCycles: WorkerCycle[];
  signalRejections: SignalRejections;
  onRefresh: () => void | Promise<void>;
}

/* ── helpers ──────────────────────────────────────────────────────────────── */

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

function num(value: number | null | undefined, decimals = 2) {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(decimals);
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

function tunerStepBadge(status: TunerStepHistory["status"]) {
  if (status === "success") return <span className="badge long">mantenuto</span>;
  if (status === "reverted") return <span className="badge short">ripristinato</span>;
  return <span className="badge muted">saltato</span>;
}

/* ── sub-components ───────────────────────────────────────────────────────── */

function TunerStatusBanner({ tuner, onAction }: {
  tuner: AutoTunerStatus;
  onAction: (action: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);

  async function act(action: string) {
    setBusy(action);
    try { await onAction(action); } finally { setBusy(null); }
  }

  const currentStep = tuner.current_step_key;
  const progress = tuner.progress;
  const progressPct = progress && progress.trades_required > 0
    ? Math.min(100, Math.round((progress.trades_done / progress.trades_required) * 100))
    : 0;

  return (
    <div style={{
      padding: "12px 16px",
      borderRadius: "var(--radius)",
      background: tuner.enabled ? "var(--long-soft)" : "var(--card-bg)",
      border: `1px solid ${tuner.enabled ? "var(--long-border)" : "var(--border)"}`,
      display: "flex",
      flexDirection: "column",
      gap: 8,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
        <span className={`badge ${tuner.enabled ? "long" : "muted"}`}>
          {tuner.enabled ? "attivo" : "in pausa"}
        </span>

        {currentStep ? (
          <span style={{ color: "var(--fg)", fontSize: "var(--fz-sm)" }}>
            Testando: <strong>{currentStep}</strong>
            {progress && (
              <span className="muted"> · {progress.trades_done}/{progress.trades_required} trade</span>
            )}
          </span>
        ) : tuner.next_step_key ? (
          <span style={{ color: "var(--fg-2)", fontSize: "var(--fz-sm)" }}>
            Prossimo step: <strong>{tuner.next_step_key}</strong>
          </span>
        ) : (
          <span className="muted" style={{ fontSize: "var(--fz-sm)" }}>
            {tuner.step_history.length > 0 ? "Sequenza completata" : "In attesa di dati sufficienti"}
          </span>
        )}

        <span className="muted" style={{ fontSize: "var(--fz-sm)" }}>
          {tuner.trades_now} trade chiusi · min {tuner.config.min_trades_per_step}/step
        </span>

        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          {tuner.enabled ? (
            <button className="btn ghost" style={{ fontSize: "var(--fz-xs)" }}
              disabled={busy !== null} onClick={() => act("pause")}>
              {busy === "pause" ? "…" : "Pausa"}
            </button>
          ) : (
            <button className="btn ghost" style={{ fontSize: "var(--fz-xs)" }}
              disabled={busy !== null} onClick={() => act("resume")}>
              {busy === "resume" ? "…" : "Riprendi"}
            </button>
          )}
          {currentStep && (
            <button className="btn ghost" style={{ fontSize: "var(--fz-xs)", color: "var(--short)" }}
              disabled={busy !== null} onClick={() => act("rollback")}>
              {busy === "rollback" ? "…" : "Rollback"}
            </button>
          )}
          <button
            className="btn ghost"
            style={{ fontSize: "var(--fz-xs)", color: "var(--short)", borderColor: "var(--short-border)" }}
            disabled={busy !== null}
            onClick={() => {
              if (!window.confirm("Reset del tuner: tutti gli step verranno cancellati. Continuare?")) return;
              act("reset");
            }}
          >
            {busy === "reset" ? "…" : "Reset"}
          </button>
        </div>
      </div>

      {currentStep && progress && (
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ flex: 1, height: 4, background: "var(--border)", borderRadius: 3, overflow: "hidden" }}>
            <div style={{
              width: `${progressPct}%`,
              height: "100%",
              background: "var(--long)",
              borderRadius: 3,
              transition: "width 0.4s ease",
            }} />
          </div>
          <span style={{ fontSize: "var(--fz-xs)", color: "var(--fg-3)", minWidth: 32, textAlign: "right" }}>
            {progressPct}%
          </span>
        </div>
      )}
    </div>
  );
}

function TunerStepHistoryTable({ history }: { history: TunerStepHistory[] }) {
  if (history.length === 0) {
    return (
      <div className="card-body muted" style={{ fontSize: "var(--fz-sm)" }}>
        Nessun step completato. Il tuner raccoglie dati prima di valutare ogni filtro.
      </div>
    );
  }
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Step</th>
          <th>Esito</th>
          <th style={{ textAlign: "right" }}>Trade val.</th>
          <th style={{ textAlign: "right" }}>WR prima</th>
          <th style={{ textAlign: "right" }}>WR dopo</th>
          <th>Valutato</th>
        </tr>
      </thead>
      <tbody>
        {[...history].reverse().map((step, i) => (
          <tr key={`${step.key}-${i}`}>
            <td className="fg">
              <div style={{ fontWeight: 500 }}>{step.label ?? step.key}</div>
              <div className="muted" style={{ fontSize: "var(--fz-xs)" }}>{step.key}</div>
            </td>
            <td>
              {tunerStepBadge(step.status)}
              {step.manual && <span className="muted" style={{ fontSize: "var(--fz-xs)", marginLeft: 4 }}>manuale</span>}
            </td>
            <td className="num">{step.trades_evaluated}</td>
            <td className="num">{pct(step.metrics_before?.win_rate)}</td>
            <td className="num">{pct(step.metrics_after?.win_rate)}</td>
            <td className="mono" style={{ fontSize: "var(--fz-xs)" }}>{fmt.relTime(step.evaluated_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function slopeColor(v: number) {
  if (v > 0.5) return "var(--long)";
  if (v < -0.5) return "var(--short)";
  return "var(--fg-2)";
}

function slopeArrow(v: number) {
  if (v > 0.5) return "↑";
  if (v < -0.5) return "↓";
  return "→";
}

function slopeLabel(v: number) {
  if (v > 2) return "forte crescita";
  if (v > 0.5) return "crescita";
  if (v > -0.5) return "neutro";
  if (v > -2) return "declino";
  return "forte declino";
}

function EquitySlopeCard({ calibration }: { calibration: Calibration }) {
  const state = calibration.state ?? {};
  const fullSlope = typeof state.last_equity_slope === "number" ? state.last_equity_slope : null;
  const recentSlope = typeof state.last_recent_slope === "number" ? state.last_recent_slope : null;
  const deepActive = state.deep_defensive_active === true;
  const fullStreak = typeof state.deep_defensive_positive_streak === "number" ? state.deep_defensive_positive_streak : 0;
  const recentStreak = typeof state.deep_defensive_recent_positive_streak === "number" ? state.deep_defensive_recent_positive_streak : 0;
  const zeroCycles = typeof state.consecutive_zero_signal_cycles === "number" ? state.consecutive_zero_signal_cycles : 0;
  const starvation = state.starvation_override_active === true;

  const rows: { label: string; value: string; color: string; sub?: string }[] = [];

  if (fullSlope !== null) {
    rows.push({
      label: "Slope equity (500 trade)",
      value: `${slopeArrow(fullSlope)} ${fullSlope.toFixed(3)}`,
      color: slopeColor(fullSlope),
      sub: slopeLabel(fullSlope),
    });
  }
  if (recentSlope !== null) {
    rows.push({
      label: "Slope recente (50 trade)",
      value: `${slopeArrow(recentSlope)} ${recentSlope.toFixed(3)}`,
      color: slopeColor(recentSlope),
      sub: slopeLabel(recentSlope),
    });
  }

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Equity slope</span>
        {deepActive
          ? <span className="badge short">deep defensive</span>
          : starvation
            ? <span className="badge warn">starvation override</span>
            : <span className="badge muted">normale</span>
        }
      </div>
      <div className="card-body" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 20 }}>
        {/* slope metrics */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {rows.length === 0 && (
            <span className="muted" style={{ fontSize: "var(--fz-sm)" }}>
              Nessun dato disponibile — la calibrazione non ha ancora completato un run.
            </span>
          )}
          {rows.map(r => (
            <div key={r.label}>
              <div className="stat-label" style={{ marginBottom: 4 }}>{r.label}</div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fz-md)", fontWeight: 600, color: r.color }}>
                {r.value}
              </div>
              <div className="muted" style={{ fontSize: "var(--fz-xs)", marginTop: 2 }}>{r.sub}</div>
            </div>
          ))}
        </div>

        {/* defensive status */}
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="stat-label">Stato modalità difensiva</div>
          <table style={{ fontSize: "var(--fz-sm)", borderCollapse: "collapse", width: "100%" }}>
            <tbody>
              <tr>
                <td className="muted" style={{ paddingBottom: 6, paddingRight: 12 }}>Deep defensive</td>
                <td style={{ color: deepActive ? "var(--short)" : "var(--long)", fontWeight: 500 }}>
                  {deepActive ? "attiva" : "inattiva"}
                </td>
              </tr>
              <tr>
                <td className="muted" style={{ paddingBottom: 6, paddingRight: 12 }}>Streak slope globale ≥ 0</td>
                <td style={{ fontVariantNumeric: "tabular-nums" }}>
                  {fullStreak} / 2 cicli
                  {fullStreak > 0 && <span className="muted"> ({fullStreak >= 2 ? "uscita via slope globale" : "in corso"})</span>}
                </td>
              </tr>
              <tr>
                <td className="muted" style={{ paddingBottom: 6, paddingRight: 12 }}>Streak slope recente ≥ 0</td>
                <td style={{ fontVariantNumeric: "tabular-nums" }}>
                  {recentStreak} / 3 cicli
                  {recentStreak > 0 && <span className="muted"> ({recentStreak >= 3 ? "uscita via slope recente" : "in corso"})</span>}
                </td>
              </tr>
              <tr>
                <td className="muted" style={{ paddingRight: 12 }}>Cicli senza segnali</td>
                <td style={{ color: zeroCycles >= 10 ? "var(--warn, var(--fg-2))" : "var(--fg)", fontVariantNumeric: "tabular-nums" }}>
                  {zeroCycles}
                  {zeroCycles >= 15 && <span className="muted"> (starvation hard)</span>}
                  {zeroCycles >= 10 && zeroCycles < 15 && <span className="muted"> (starvation soft)</span>}
                </td>
              </tr>
            </tbody>
          </table>
          <div className="muted" style={{ fontSize: "var(--fz-xs)", lineHeight: 1.5 }}>
            Lo slope è la pendenza della regressione lineare sulla curva del PnL cumulativo.
            Positivo = curva in salita. La versione recente usa solo gli ultimi 50 trade,
            permettendo di uscire dal deep defensive prima che l&apos;intera finestra storica si aggiusti.
          </div>
        </div>
      </div>
    </div>
  );
}

function ActiveStrategyCard({ calibration, tuner, settings }: { calibration: Calibration; tuner: AutoTunerStatus | null; settings: AppSettings }) {
  const risk = (settings.risk ?? {}) as Record<string, unknown>;
  const deepDefensive = calibration.state?.deep_defensive_active === true;
  const posStreak = typeof calibration.state?.deep_defensive_positive_streak === "number"
    ? calibration.state.deep_defensive_positive_streak
    : null;

  const activeFilters = tuner
    ? tuner.step_history.filter(s => s.status === "success").map(s => s.label ?? s.key)
    : [];

  const nextSequence = tuner?.next_step_key ?? null;
  const currentTesting = tuner?.current_step_key ?? null;

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Strategia attuale</span>
        {deepDefensive && <span className="badge short">difensiva profonda</span>}
      </div>
      <div className="card-body" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 20 }}>
        <div>
          <div className="stat-label" style={{ marginBottom: 8 }}>Parametri di rischio attivi</div>
          <div className="mono" style={{ color: "var(--fg-2)", fontSize: "var(--fz-sm)", lineHeight: 2 }}>
            score ≥ <strong style={{ color: "var(--fg)" }}>{String(risk?.min_signal_score ?? "—")}</strong><br />
            RR ≥ <strong style={{ color: "var(--fg)" }}>{String(risk?.min_risk_reward ?? "—")}</strong><br />
            risk/trade <strong style={{ color: "var(--fg)" }}>{String(risk?.max_risk_per_trade_pct ?? "—")}%</strong><br />
            ATR min <strong style={{ color: "var(--fg)" }}>{String(risk?.min_atr_multiplier ?? "—")}×</strong><br />
            stop min <strong style={{ color: "var(--fg)" }}>{risk?.min_stop_distance_pct != null ? `${(Number(risk.min_stop_distance_pct) * 100).toFixed(1)}%` : "—"}</strong>
          </div>
        </div>
        <div>
          <div className="stat-label" style={{ marginBottom: 8 }}>Filtri aggiuntivi (auto-tuner)</div>
          {activeFilters.length === 0 && !currentTesting ? (
            <div className="muted" style={{ fontSize: "var(--fz-sm)" }}>Nessun filtro aggiuntivo attivo</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {activeFilters.map(f => (
                <div key={f} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span className="badge long" style={{ fontSize: "var(--fz-xs)" }}>✓ attivo</span>
                  <span style={{ fontSize: "var(--fz-sm)" }}>{f}</span>
                </div>
              ))}
              {currentTesting && (
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span className="badge info" style={{ fontSize: "var(--fz-xs)" }}>⏳ test</span>
                  <span style={{ fontSize: "var(--fz-sm)" }}>{currentTesting}</span>
                </div>
              )}
              {nextSequence && (
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span className="badge muted" style={{ fontSize: "var(--fz-xs)" }}>prossimo</span>
                  <span style={{ fontSize: "var(--fz-sm)", color: "var(--fg-3)" }}>{nextSequence}</span>
                </div>
              )}
            </div>
          )}
          {deepDefensive && (
            <div style={{ marginTop: 12, fontSize: "var(--fz-sm)", color: "var(--short)" }}>
              Modalità difensiva: parametri irrigiditi automaticamente dalla calibrazione per performance scadente.
              {posStreak != null && posStreak > 0 && (
                <span className="muted"> Streak positivi: {String(posStreak)}</span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function WorkerCyclesCard({ cycles }: { cycles: WorkerCycle[] }) {
  const nonEmpty = cycles.filter(c => c.event_type === "worker_cycle");
  const zeroGenStreak = useMemo(() => {
    let streak = 0;
    for (const c of nonEmpty) {
      if ((c.data.generated ?? 0) === 0) streak++;
      else break;
    }
    return streak;
  }, [nonEmpty]);

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Cicli worker recenti</span>
        {zeroGenStreak > 0 && (
          <span className="badge short">{zeroGenStreak} cicli consecutivi senza segnali</span>
        )}
        <span className="badge muted">{nonEmpty.length} cicli</span>
      </div>
      <table className="tbl">
        <thead>
          <tr>
            <th>Quando</th>
            <th>Timeframe attivi</th>
            <th style={{ textAlign: "right" }}>Mercati</th>
            <th style={{ textAlign: "right" }}>Synced</th>
            <th style={{ textAlign: "right" }}>Sync saltati</th>
            <th style={{ textAlign: "right" }}>Segnali</th>
            <th style={{ textAlign: "right" }}>Chiusi</th>
          </tr>
        </thead>
        <tbody>
          {nonEmpty.slice(0, 15).map(c => {
            const d = c.data;
            const generated = d.generated ?? 0;
            return (
              <tr key={c.id}>
                <td className="mono" style={{ fontSize: "var(--fz-xs)", whiteSpace: "nowrap" }}>{fmt.relTime(c.timestamp)}</td>
                <td style={{ fontSize: "var(--fz-xs)", whiteSpace: "nowrap" }}>
                  {(d.active_timeframes ?? []).join(" · ") || <span className="muted">—</span>}
                </td>
                <td className="num">{d.markets ?? "—"}</td>
                <td className="num">{d.synced ?? "—"}</td>
                <td className={`num ${(d.skipped ?? 0) > 0 ? "down" : ""}`}>{d.skipped ?? 0}</td>
                <td className={`num ${generated > 0 ? "up" : "muted"}`}>{generated}</td>
                <td className="num">{d.closed ?? 0}</td>
              </tr>
            );
          })}
          {nonEmpty.length === 0 && (
            <tr>
              <td colSpan={7} style={{ textAlign: "center", color: "var(--fg-3)", height: 60 }}>
                Nessun ciclo registrato.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function SignalRejectionCard({ rejections, calibration, settings }: {
  rejections: SignalRejections;
  calibration: Calibration;
  settings: AppSettings;
}) {
  const deepDefensive = calibration.state?.deep_defensive_active === true;
  const sorted = Object.entries(rejections.by_reason).sort((a, b) => b[1] - a[1]);

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Segnali skippati — motivazioni</span>
        <span className="badge muted">{rejections.total} ordini live rifiutati</span>
      </div>
      <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>

        {deepDefensive && (
          <div style={{
            padding: "10px 14px",
            borderRadius: "var(--radius)",
            background: "var(--short-soft)",
            border: "1px solid var(--short-border)",
            fontSize: "var(--fz-sm)",
            color: "var(--short)",
          }}>
            <strong>Modalità difensiva profonda attiva.</strong>{" "}
            Il motore genera 0 segnali perché <code>min_signal_score</code> ({String(settings.risk?.min_signal_score ?? calibration.settings?.min_signal_score ?? "—")}) è troppo alto per il mercato attuale. La calibrazione automatica ha irrigidito i parametri in risposta a performance scarsa (win rate {pct(calibration.overall?.win_rate)}, PF {num(calibration.overall?.profit_factor)}).
          </div>
        )}

        {sorted.length > 0 ? (
          <div>
            <div className="stat-label" style={{ marginBottom: 8 }}>Motivi rifiuto ordini live (ultimi 50)</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {sorted.map(([reason, count]) => (
                <div key={reason} style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
                  <span className="num" style={{ fontSize: "var(--fz-sm)", color: "var(--short)", minWidth: 28, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{count}</span>
                  <span style={{ fontSize: "var(--fz-sm)", color: "var(--fg-2)" }}>{reason}</span>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="muted" style={{ fontSize: "var(--fz-sm)" }}>
            Nessun ordine live rifiutato nell&apos;intervallo recente.
          </div>
        )}

        {rejections.recent.length > 0 && (
          <div>
            <div className="stat-label" style={{ marginBottom: 8 }}>Ultimi rifiuti</div>
            <table className="tbl">
              <thead>
                <tr><th>Quando</th><th>Motivazioni</th></tr>
              </thead>
              <tbody>
                {rejections.recent.slice(0, 8).map(r => (
                  <tr key={r.id}>
                    <td className="mono" style={{ fontSize: "var(--fz-xs)" }}>{fmt.relTime(r.timestamp)}</td>
                    <td style={{ fontSize: "var(--fz-xs)" }}>{r.reasons.join(" · ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── main screen ──────────────────────────────────────────────────────────── */

export function CalibrationScreen({
  calibration,
  history,
  settings,
  tunerStatus,
  workerCycles,
  signalRejections,
  onRefresh,
}: Props) {
  const [localCalibration, setLocalCalibration] = useState(calibration);
  const [localHistory, setLocalHistory] = useState(history);
  const [localTuner, setLocalTuner] = useState(tunerStatus);
  const [localCycles, setLocalCycles] = useState(workerCycles);
  const [localRejections, setLocalRejections] = useState(signalRejections);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const enabled = settings.auto_calibration?.enabled !== false;
  const lastStatus = String(localCalibration.state?.last_status ?? "nessun run");
  const deepDefensive = localCalibration.state?.deep_defensive_active === true;

  useEffect(() => setLocalCalibration(calibration), [calibration]);
  useEffect(() => setLocalHistory(history), [history]);
  useEffect(() => setLocalTuner(tunerStatus), [tunerStatus]);
  useEffect(() => setLocalCycles(workerCycles), [workerCycles]);
  useEffect(() => setLocalRejections(signalRejections), [signalRejections]);

  const appliedCount = useMemo(
    () => localHistory.filter(entry => entry.event_type === "auto_calibration_applied").length,
    [localHistory]
  );
  const failedCount = useMemo(
    () => localHistory.filter(entry => entry.event_type === "auto_calibration_failed" || entry.event_type === "auto_calibration_skipped").length,
    [localHistory]
  );

  async function runNow() {
    if (!window.confirm("Avviare la calibrazione ora? I parametri di rischio attivi potrebbero essere modificati.")) return;
    setBusy(true);
    setError(null);
    try {
      await apiPost<Calibration>("/api/auto-calibration/run");
      const [nextCalibration, nextHistory] = await Promise.all([
        apiGet<Calibration>("/api/analytics/calibration"),
        apiGet<CalibrationHistoryEntry[]>("/api/analytics/calibration/history"),
      ]);
      setLocalCalibration(nextCalibration);
      setLocalHistory(nextHistory);
      await onRefresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Errore calibrazione");
    } finally {
      setBusy(false);
    }
  }

  async function tunerAction(action: string) {
    const endpoints: Record<string, string> = {
      pause: "/api/auto-tuner/pause",
      resume: "/api/auto-tuner/resume",
      rollback: "/api/auto-tuner/rollback",
      reset: "/api/auto-tuner/reset",
    };
    const url = endpoints[action];
    if (!url) return;
    await apiPost<unknown>(url);
    const next = await apiGet<AutoTunerStatus>("/api/auto-tuner/status");
    setLocalTuner(next);
    await onRefresh();
  }

  const appliedChanges = Array.isArray(localCalibration.state?.last_changes)
    ? localCalibration.state.last_changes as CalibrationChange[]
    : [];
  const proposedChanges = localCalibration.changes;

  return (
    <div className="page" style={{ opacity: busy ? 0.7 : 1, pointerEvents: busy ? "none" : "auto", transition: "opacity 0.2s" }}>
      <div className="page-head">
        <div>
          <h1 className="page-title">Calibrazione</h1>
          <div className="page-sub">
            {enabled ? "attiva" : "disattivata"} · ultimo stato {lastStatus} · ultimo run {lastRunText(localCalibration)}
          </div>
        </div>
        <div className="page-actions">
          <button className="btn ghost" aria-label="Aggiorna" onClick={onRefresh}><Icon name="refresh" size={12} /></button>
          <button className="btn primary" disabled={busy} onClick={runNow}>
            <Icon name="refresh" size={12} /> {busy ? "Calibrazione..." : "Calibra ora"}
          </button>
        </div>
      </div>

      {deepDefensive && (
        <div style={{
          padding: "12px 16px",
          marginBottom: "var(--gap)",
          borderRadius: "var(--radius)",
          background: "var(--short-soft)",
          border: "1px solid var(--short-border)",
          color: "var(--short)",
          fontSize: "var(--fz-sm)",
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}>
          <strong>⚠ Modalità difensiva profonda attiva</strong>
          {" — "}Il motore non emette segnali. Win rate: {pct(localCalibration.overall?.win_rate)} · PF: {num(localCalibration.overall?.profit_factor)} · score minimo: {String(settings.risk?.min_signal_score ?? localCalibration.settings?.min_signal_score ?? "—")}.
        </div>
      )}

      {error && (
        <div style={{ padding: "8px 14px", marginBottom: "var(--gap)", background: "var(--short-soft)", border: "1px solid var(--short-border)", borderRadius: "var(--radius)", color: "var(--short)", fontSize: "var(--fz-sm)" }}>
          {error}
        </div>
      )}

      {/* ── stat grid ───────────────────────────────────────────────────────── */}
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

      {/* ── equity slope ─────────────────────────────────────────────────── */}
      <div style={{ marginBottom: "var(--gap)" }}>
        <EquitySlopeCard calibration={localCalibration} />
      </div>

      {/* ── auto-tuner section ────────────────────────────────────────────── */}
      <div className="card" style={{ marginBottom: "var(--gap)" }}>
        <div className="card-head">
          <span className="title">Auto-Tuner</span>
          <span className="badge muted">
            {localTuner ? (localTuner.step_history.length + " step") : "n/d"}
          </span>
        </div>
        <div style={{ padding: "0 0 var(--gap)" }}>
          {localTuner ? (
            <TunerStatusBanner tuner={localTuner} onAction={tunerAction} />
          ) : (
            <div className="card-body muted" style={{ fontSize: "var(--fz-sm)" }}>
              Stato tuner non disponibile.
            </div>
          )}
        </div>
        {localTuner && (
          <TunerStepHistoryTable history={localTuner.step_history} />
        )}
      </div>

      {/* ── strategia attuale + motivo ultimo run ────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <ActiveStrategyCard calibration={localCalibration} tuner={localTuner} settings={settings} />
        <div className="card">
          <div className="card-head">
            <span className="title">Motivo ultimo run</span>
          </div>
          <div className="card-body">
            <div className="stat-label">Motivo</div>
            <div style={{ color: "var(--fg)", fontSize: "var(--fz-md)", marginTop: 4 }}>
              {String(localCalibration.state?.last_reason ?? localCalibration.reason)}
            </div>
          </div>
        </div>
      </div>

      {/* ── segnali skippati ─────────────────────────────────────────────── */}
      <div style={{ marginBottom: "var(--gap)" }}>
        <SignalRejectionCard rejections={localRejections} calibration={localCalibration} settings={settings} />
      </div>

      {/* ── worker cycles ─────────────────────────────────────────────────── */}
      <div style={{ marginBottom: "var(--gap)" }}>
        <WorkerCyclesCard cycles={localCycles} />
      </div>

      {/* ── calibration changes ──────────────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: "var(--gap)" }}>
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
          <div className="card-head"><span className="title">Preview non applicata</span>
            <span className="badge info">{proposedChanges.length} proposte</span>
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
      </div>

      {/* ── log calibrazione ─────────────────────────────────────────────── */}
      <CalibrationLogTable history={localHistory} />
    </div>
  );
}

const LOG_PAGE_SIZE = 15;

function CalibrationLogTable({ history }: { history: CalibrationHistoryEntry[] }) {
  const [page, setPage] = useState(0);
  const totalPages = Math.max(1, Math.ceil(history.length / LOG_PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const slice = history.slice(safePage * LOG_PAGE_SIZE, (safePage + 1) * LOG_PAGE_SIZE);

  return (
    <div className="card" style={{ marginTop: "var(--gap)" }}>
      <div className="card-head">
        <span className="title">Log calibrazione</span>
        <span className="badge muted">{history.length} eventi</span>
        {totalPages > 1 && (
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
            <button
              className="btn ghost"
              style={{ padding: "2px 8px", fontSize: "var(--fz-xs)" }}
              disabled={safePage === 0}
              onClick={() => setPage(p => Math.max(0, p - 1))}
            >
              ‹
            </button>
            <span style={{ fontSize: "var(--fz-xs)", color: "var(--fg-2)" }}>
              {safePage + 1} / {totalPages}
            </span>
            <button
              className="btn ghost"
              style={{ padding: "2px 8px", fontSize: "var(--fz-xs)" }}
              disabled={safePage >= totalPages - 1}
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
            >
              ›
            </button>
          </div>
        )}
      </div>
      <table className="tbl">
        <thead>
          <tr>
            <th>Quando</th>
            <th>Stato</th>
            <th style={{ textAlign: "right" }}>Trade</th>
            <th style={{ textAlign: "right" }}>Win rate</th>
            <th style={{ textAlign: "right" }}>PnL</th>
            <th style={{ textAlign: "right" }}>Modifiche</th>
            <th>Motivo / errore</th>
          </tr>
        </thead>
        <tbody>
          {slice.map(entry => (
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
          {history.length === 0 && (
            <tr>
              <td colSpan={7} style={{ textAlign: "center", color: "var(--fg-3)", height: 72 }}>
                Nessuna calibrazione ancora registrata.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
