"use client";

import React, { useEffect, useState } from "react";
import { Icon } from "@/components/atoms";
import { Strategy, AppSettings, apiPatch } from "@/lib/api";

interface Props {
  strategies: Strategy[];
  settings: AppSettings;
  threshold: number;
  onThresholdChange: (t: number) => void | Promise<void>;
  onRefresh: () => void | Promise<void>;
}

const TF_ORDER = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"];
type Toast = { id: number; tone: "success" | "error"; message: string };

function bucketName(s: number) {
  if (s >= 90) return "molto forte";
  if (s >= 75) return "forte";
  if (s >= 60) return "buono";
  if (s >= 40) return "medio";
  return "debole";
}

function toNum(v: unknown, fallback: number): number {
  const n = Number(v);
  return isFinite(n) && v != null ? n : fallback;
}

export function StrategiesScreen({ strategies, settings, threshold, onThresholdChange, onRefresh }: Props) {
  const [localStrategies, setLocalStrategies] = useState(strategies);
  const [tfs, setTfs] = useState<Record<string, boolean>>(() =>
    strategies.reduce<Record<string, boolean>>((acc, strategy) => {
      acc[strategy.timeframe] = (acc[strategy.timeframe] ?? false) || strategy.enabled;
      return acc;
    }, {})
  );
  const riskSettings = settings.risk ?? {};
  const [minRR, setMinRR] = useState(() => toNum(riskSettings.min_risk_reward, 2.0));
  const [risk, setRisk] = useState(() => toNum(riskSettings.max_risk_per_trade_pct, 1.0));
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const maxWeight = toNum(settings.auto_calibration?.max_weight, 30);

  const enabledCount = localStrategies.filter(s => s.enabled).length;
  const totalWeight = localStrategies.reduce((a, s) => a + s.weight, 0);

  const byTf = localStrategies.reduce<Record<string, Strategy[]>>((acc, s) => {
    (acc[s.timeframe] = acc[s.timeframe] ?? []).push(s);
    return acc;
  }, {});
  const sortedTfs = [
    ...TF_ORDER.filter(tf => tf in byTf),
    ...Object.keys(byTf).filter(tf => !TF_ORDER.includes(tf)),
  ];

  useEffect(() => {
    setLocalStrategies(strategies);
    setTfs(strategies.reduce<Record<string, boolean>>((acc, strategy) => {
      acc[strategy.timeframe] = (acc[strategy.timeframe] ?? false) || strategy.enabled;
      return acc;
    }, {}));
  }, [strategies]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  function notify(tone: Toast["tone"], message: string) {
    setToast({ id: Date.now(), tone, message });
  }

  async function toggleStrategy(s: Strategy) {
    const next = !s.enabled;
    setBusy(`toggle-${s.id}`);
    setError(null);
    try {
      await apiPatch(`/api/strategies/${s.id}`, { enabled: next });
      setLocalStrategies(prev => prev.map(item => item.id === s.id ? { ...item, enabled: next } : item));
      notify("success", next ? "Strategia abilitata" : "Strategia disabilitata");
      await onRefresh();
    } catch (e) {
      const message = e instanceof Error ? e.message : "Errore";
      setError(message);
      notify("error", message);
    } finally {
      setBusy(null);
    }
  }

  async function saveWeight(s: Strategy, weight: number) {
    setBusy(`weight-${s.id}`);
    setError(null);
    try {
      await apiPatch(`/api/strategies/${s.id}`, { weight });
      setLocalStrategies(prev => prev.map(item => item.id === s.id ? { ...item, weight } : item));
      notify("success", `Peso ${s.strategy_name} salvato`);
      await onRefresh();
    } catch (e) {
      const message = e instanceof Error ? e.message : "Errore";
      setError(message);
      notify("error", message);
    } finally {
      setBusy(null);
    }
  }

  function updateWeight(id: number, w: number) {
    setLocalStrategies(prev => prev.map(s => s.id === id ? { ...s, weight: w } : s));
  }

  async function toggleTimeframe(timeframe: string) {
    const next = !tfs[timeframe];
    setBusy(`tf-${timeframe}`);
    setError(null);
    try {
      const group = localStrategies.filter(s => s.timeframe === timeframe);
      await Promise.all(group.map(s => apiPatch(`/api/strategies/${s.id}`, { enabled: next })));
      setTfs(prev => ({ ...prev, [timeframe]: next }));
      setLocalStrategies(prev => prev.map(s => s.timeframe === timeframe ? { ...s, enabled: next } : s));
      notify("success", `${timeframe} ${next ? "abilitato" : "disabilitato"}`);
      await onRefresh();
    } catch (e) {
      const message = e instanceof Error ? e.message : "Errore";
      setError(message);
      notify("error", message);
    } finally {
      setBusy(null);
    }
  }

  async function saveRiskSettings() {
    setBusy("risk");
    setError(null);
    try {
      await apiPatch("/api/settings", {
        risk: {
          value: {
            ...riskSettings,
            min_signal_score: threshold,
            min_risk_reward: minRR,
            max_risk_per_trade_pct: risk,
          },
        },
      });
      notify("success", "Risk management salvato");
      await onRefresh();
    } catch (e) {
      const message = e instanceof Error ? e.message : "Errore";
      setError(message);
      notify("error", message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Strategie</h1>
          <div className="page-sub">
            {enabledCount} attive · weight totale {totalWeight} · soglia score ≥ {threshold}
          </div>
        </div>
        <div className="page-actions">
          <button className="btn ghost" onClick={onRefresh}><Icon name="refresh" size={12} /></button>
          <button className="btn primary" disabled={busy !== null} onClick={saveRiskSettings}>
            <Icon name="check" size={12} /> {busy === "risk" ? "Salvataggio..." : "Salva risk"}
          </button>
        </div>
      </div>

      {toast && <ToastNotice toast={toast} onClose={() => setToast(null)} />}

      {error && (
        <div style={{ padding: "8px 14px", marginBottom: "var(--gap)", background: "var(--short-soft)", border: "1px solid var(--short-border)", borderRadius: "var(--radius)", color: "var(--short)", fontSize: "var(--fz-sm)" }}>
          {error}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: "var(--gap)" }}>
        {/* Strategy list */}
        <div className="card">
          <div className="card-head">
            <span className="title">Indicatori &amp; strategie</span>
            <span className="badge muted">{strategies.length} moduli</span>
            <div className="right muted" style={{ fontSize: "var(--fz-sm)" }}>weight contribuisce allo score finale (0–100)</div>
          </div>
          <div>
            {sortedTfs.map(tf => (
              <div key={tf}>
                <div style={{ padding: "8px 18px", background: "var(--bg-2)", borderBottom: "1px solid var(--border)", fontSize: "var(--fz-xs)", fontWeight: 600, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  {tf} · {byTf[tf].filter(s => s.enabled).length}/{byTf[tf].length} attive
                </div>
                {byTf[tf].map(s => {
                  const local = localStrategies.find(l => l.id === s.id)!;
                  return (
                    <StratRow
                      key={s.id}
                      s={local}
                      disabled={busy !== null}
                      maxWeight={maxWeight}
                      onToggle={() => toggleStrategy(local)}
                      onWeight={w => updateWeight(s.id, w)}
                      onWeightSave={w => saveWeight(local, w)}
                      isSaving={busy === `weight-${s.id}`}
                    />
                  );
                })}
              </div>
            ))}
            {strategies.length === 0 && (
              <div style={{ padding: "32px", textAlign: "center", color: "var(--fg-3)" }}>Nessuna strategia configurata</div>
            )}
          </div>
        </div>

        {/* Side panel */}
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap)" }}>
          {/* Score threshold */}
          <div className="card">
            <div className="card-head"><span className="title">Soglia minima score</span></div>
            <div className="card-body">
              <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 12 }}>
                <span className="mono" style={{ fontSize: "var(--fz-3xl)", fontWeight: 500, letterSpacing: "-0.02em" }}>{threshold}</span>
                <span className="muted">/ 100</span>
                <span style={{ marginLeft: "auto" }} className="badge long">{bucketName(threshold)}</span>
              </div>
              <input
                type="range" min={0} max={100} value={threshold}
                onChange={e => onThresholdChange(+e.target.value)}
                style={{ width: "100%", accentColor: "var(--accent)" }}
              />
              <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8, fontSize: 10, color: "var(--fg-3)" }} className="mono">
                <span>0</span><span>40</span><span>60</span><span>75</span><span>90</span><span>100</span>
              </div>
              <div style={{ marginTop: 12, padding: 10, background: "var(--bg-2)", borderRadius: "var(--radius-sm)", fontSize: "var(--fz-sm)", color: "var(--fg-2)" }}>
                Segnali con score sotto {threshold} verranno ignorati o nascosti.
              </div>
            </div>
          </div>

          {/* TF toggles */}
          <div className="card">
            <div className="card-head"><span className="title">Timeframe abilitati</span></div>
            <div className="card-body" style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {sortedTfs.map(t => (
                <button
                  key={t}
                  className={"btn sm " + (tfs[t] ? "primary" : "")}
                  disabled={busy !== null}
                  style={{ minWidth: 52 }}
                  onClick={() => toggleTimeframe(t)}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>

          {/* Risk management */}
          <div className="card">
            <div className="card-head"><span className="title">Risk management</span></div>
            <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <SliderRow label="Risk per trade" value={risk} onChange={setRisk} min={0.1} max={3.0} step={0.1} unit="%" hint="del capitale" />
              <SliderRow label="Min R/R ratio" value={minRR} onChange={setMinRR} min={1.0} max={5.0} step={0.1} unit="" hint="segnali sotto soglia ignorati" />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Strategy row ──────────────────────────────────────────────────────────── */
function StratRow({ s, disabled, maxWeight, onToggle, onWeight, onWeightSave, isSaving }: {
  s: Strategy;
  disabled: boolean;
  maxWeight: number;
  onToggle: () => void;
  onWeight: (w: number) => void;
  onWeightSave: (w: number) => void;
  isSaving: boolean;
}) {
  const paramsStr = Object.entries(s.params ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "default params";
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "24px 180px 1fr 250px 80px",
      alignItems: "center", gap: 14, padding: "12px 18px",
      borderBottom: "1px solid var(--border)",
      opacity: s.enabled ? 1 : 0.5,
    }}>
      <div
        className={"toggle " + (s.enabled ? "on" : "")}
        onClick={() => !disabled && onToggle()}
        style={{ cursor: disabled ? "not-allowed" : "pointer" }}
      />
      <div>
        <div style={{ fontWeight: 500, fontSize: "var(--fz-md)" }}>{s.strategy_name}</div>
        <div className="muted mono" style={{ fontSize: 10, marginTop: 1 }}>{s.timeframe}</div>
      </div>
      <div className="muted" style={{ fontSize: "var(--fz-sm)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{paramsStr}</div>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <input
          type="range" min={0} max={maxWeight} value={s.weight}
          disabled={!s.enabled || disabled}
          onChange={e => onWeight(+e.target.value)}
          style={{ flex: 1, accentColor: "var(--accent)" }}
        />
        <button
          className="btn sm"
          disabled={!s.enabled || disabled}
          onClick={() => onWeightSave(s.weight)}
          title={`Salva peso ${s.strategy_name}`}
          aria-label={`Salva peso ${s.strategy_name}`}
        >
          <Icon name="check" size={11} /> {isSaving ? "..." : "Salva"}
        </button>
      </div>
      <div className="mono" style={{ textAlign: "right", color: s.enabled ? "var(--accent)" : "var(--fg-3)" }}>
        +{s.weight}
      </div>
    </div>
  );
}

function ToastNotice({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  const isError = toast.tone === "error";
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "fixed",
        right: 20,
        bottom: 20,
        zIndex: 60,
        display: "flex",
        alignItems: "center",
        gap: 10,
        minWidth: 260,
        maxWidth: 360,
        padding: "12px 14px",
        borderRadius: "var(--radius)",
        border: `1px solid ${isError ? "var(--short-border)" : "var(--long-border)"}`,
        background: "var(--bg-1)",
        boxShadow: "var(--shadow-deep)",
        color: isError ? "var(--short)" : "var(--long)",
        fontSize: "var(--fz-sm)",
      }}
    >
      <Icon name={isError ? "x" : "check"} size={14} />
      <span style={{ flex: 1, color: "var(--fg)" }}>{toast.message}</span>
      <button className="btn ghost sm" onClick={onClose} aria-label="Chiudi notifica">
        <Icon name="x" size={12} />
      </button>
    </div>
  );
}

/* ── Slider row ─────────────────────────────────────────────────────────────── */
function SliderRow({ label, value, onChange, min, max, step, unit, hint }: {
  label: string; value: number; onChange: (v: number) => void;
  min: number; max: number; step: number; unit: string; hint: string;
}) {
  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", marginBottom: 6, gap: 6 }}>
        <span style={{ flex: 1, fontSize: "var(--fz-sm)" }}>{label}</span>
        <span className="mono" style={{ fontSize: "var(--fz-md)", color: "var(--fg)" }}>{value}{unit}</span>
      </div>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={e => onChange(+e.target.value)}
        style={{ width: "100%", accentColor: "var(--accent)" }}
      />
      <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>{hint}</div>
    </div>
  );
}
