"use client";

import React, { useEffect, useState } from "react";
import { Icon, Seg } from "@/components/atoms";
import { AppSettings, Health, apiPatch } from "@/lib/api";

interface Props {
  settings: AppSettings;
  health: Health;
  onRefresh: () => void | Promise<void>;
}

type Toast = { tone: "success" | "error"; message: string };

const MODE_OPTIONS: [string, string][] = [
  ["SIGNAL_ONLY", "Signal"],
  ["PAPER_TRADING", "Paper"],
  ["AUTO_TRADE", "Live"],
];

const TF_OPTIONS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"];

function toNum(value: unknown, fallback: number): number {
  const next = Number(value);
  return Number.isFinite(next) ? next : fallback;
}

function toBool(value: unknown, fallback = false): boolean {
  if (value == null) return fallback;
  return Boolean(value);
}

function enabledTimeframes(settings: AppSettings): string[] {
  const value = settings.timeframes?.enabled;
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function mergePatch(value: Record<string, unknown> | undefined, patch: Record<string, unknown>) {
  return { ...(value ?? {}), ...patch };
}

export function SettingsScreen({ settings, health, onRefresh }: Props) {
  const [mode, setMode] = useState(String(settings.operation_mode?.mode ?? health.mode ?? "SIGNAL_ONLY"));
  const [liveEnabled, setLiveEnabled] = useState(toBool(settings.operation_mode?.enable_live_trading, health.enable_live_trading));
  const [killEnabled, setKillEnabled] = useState(toBool(settings.kill_switch?.enabled));
  const [minScore, setMinScore] = useState(toNum(settings.risk?.min_signal_score, 60));
  const [minRR, setMinRR] = useState(toNum(settings.risk?.min_risk_reward, 2));
  const [minVolume, setMinVolume] = useState(toNum(settings.risk?.min_average_quote_volume_usd, 50000));
  const [riskPct, setRiskPct] = useState(toNum(settings.risk?.max_risk_per_trade_pct, 1));
  const [dailyLoss, setDailyLoss] = useState(toNum(settings.risk?.max_daily_loss_pct, 3));
  const [maxPositions, setMaxPositions] = useState(toNum(settings.risk?.max_open_positions, 5));
  const [maxExposure, setMaxExposure] = useState(toNum(settings.risk?.max_total_exposure_pct, 25));
  const [maxLeverage, setMaxLeverage] = useState(toNum(settings.risk?.max_leverage, 3));
  const [paperEquity, setPaperEquity] = useState(toNum(settings.paper?.equity, 10000));
  const [timeframes, setTimeframes] = useState<string[]>(() => enabledTimeframes(settings));
  const [autoCalibration, setAutoCalibration] = useState(toBool(settings.auto_calibration?.enabled, true));
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);

  useEffect(() => {
    setMode(String(settings.operation_mode?.mode ?? health.mode ?? "SIGNAL_ONLY"));
    setLiveEnabled(toBool(settings.operation_mode?.enable_live_trading, health.enable_live_trading));
    setKillEnabled(toBool(settings.kill_switch?.enabled));
    setMinScore(toNum(settings.risk?.min_signal_score, 60));
    setMinRR(toNum(settings.risk?.min_risk_reward, 2));
    setMinVolume(toNum(settings.risk?.min_average_quote_volume_usd, 50000));
    setRiskPct(toNum(settings.risk?.max_risk_per_trade_pct, 1));
    setDailyLoss(toNum(settings.risk?.max_daily_loss_pct, 3));
    setMaxPositions(toNum(settings.risk?.max_open_positions, 5));
    setMaxExposure(toNum(settings.risk?.max_total_exposure_pct, 25));
    setMaxLeverage(toNum(settings.risk?.max_leverage, 3));
    setPaperEquity(toNum(settings.paper?.equity, 10000));
    setTimeframes(enabledTimeframes(settings));
    setAutoCalibration(toBool(settings.auto_calibration?.enabled, true));
  }, [health, settings]);

  function toggleTimeframe(timeframe: string) {
    setTimeframes((prev) =>
      prev.includes(timeframe) ? prev.filter((item) => item !== timeframe) : [...prev, timeframe]
    );
  }

  async function save() {
    setBusy(true);
    setToast(null);
    try {
      await apiPatch("/api/settings", {
        operation_mode: {
          value: mergePatch(settings.operation_mode, {
            mode,
            enable_live_trading: liveEnabled,
          }),
        },
        kill_switch: {
          value: mergePatch(settings.kill_switch, { enabled: killEnabled }),
        },
        risk: {
          value: mergePatch(settings.risk, {
            min_signal_score: minScore,
            min_risk_reward: minRR,
            min_average_quote_volume_usd: minVolume,
            max_risk_per_trade_pct: riskPct,
            max_daily_loss_pct: dailyLoss,
            max_open_positions: maxPositions,
            max_total_exposure_pct: maxExposure,
            max_leverage: maxLeverage,
          }),
        },
        paper: {
          value: mergePatch(settings.paper, { equity: paperEquity }),
        },
        timeframes: {
          value: mergePatch(settings.timeframes, { enabled: timeframes }),
        },
        auto_calibration: {
          value: mergePatch(settings.auto_calibration, { enabled: autoCalibration }),
        },
      });
      setToast({ tone: "success", message: "Impostazioni salvate" });
      await onRefresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Errore salvataggio" });
    } finally {
      setBusy(false);
    }
  }

  const liveArmed = mode === "AUTO_TRADE" && liveEnabled && !killEnabled;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Settings</h1>
          <div className="page-sub">
            {health.network} · {liveArmed ? "live armato" : "live non armato"} · {timeframes.length} timeframe
          </div>
        </div>
        <div className="page-actions">
          <button className="btn ghost" onClick={onRefresh}><Icon name="refresh" size={12} /></button>
          <button className="btn primary" disabled={busy} onClick={save}>
            <Icon name="check" size={12} /> {busy ? "Salvataggio..." : "Salva"}
          </button>
        </div>
      </div>

      {toast && (
        <div style={{ padding: "8px 14px", marginBottom: "var(--gap)", borderRadius: "var(--radius)", border: "1px solid var(--border)", background: toast.tone === "success" ? "var(--long-soft)" : "var(--short-soft)", color: toast.tone === "success" ? "var(--long)" : "var(--short)", fontSize: "var(--fz-sm)" }}>
          {toast.message}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Runtime</span>
            <span className={"badge " + (liveArmed ? "short" : "muted")}>{mode}</span>
          </div>
          <div className="card-body" style={{ display: "grid", gap: 14 }}>
            <Field label="Modalità operativa">
              <Seg value={mode} onChange={setMode} options={MODE_OPTIONS} />
            </Field>
            <Toggle label="Enable live trading" checked={liveEnabled} onChange={setLiveEnabled} />
            <Toggle label="Kill switch" checked={killEnabled} onChange={setKillEnabled} danger />
            <div style={{ padding: 10, background: "var(--bg-2)", borderRadius: "var(--radius-sm)", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
              <span className="mono">DATABASE_URL</span>, network, account address e private key restano configurazione ambiente e richiedono restart.
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="title">Paper trading</span>
          </div>
          <div className="card-body" style={{ display: "grid", gap: 14 }}>
            <NumberField label="Equity paper" value={paperEquity} onChange={setPaperEquity} step={100} suffix="EUR" />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <NumberField label="Risk per trade" value={riskPct} onChange={setRiskPct} step={0.1} suffix="%" />
              <NumberField label="Max leverage" value={maxLeverage} onChange={setMaxLeverage} step={0.1} suffix="x" />
            </div>
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1.2fr 0.8fr", gap: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Risk &amp; segnali</span>
          </div>
          <div className="card-body" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <NumberField label="Score minimo" value={minScore} onChange={setMinScore} min={0} max={100} step={1} />
            <NumberField label="Risk/reward minimo" value={minRR} onChange={setMinRR} min={0} step={0.1} />
            <NumberField label="Volume medio minimo" value={minVolume} onChange={setMinVolume} min={0} step={1000} suffix="EUR" />
            <NumberField label="Max daily loss" value={dailyLoss} onChange={setDailyLoss} min={0} step={0.1} suffix="%" />
            <NumberField label="Max posizioni live" value={maxPositions} onChange={setMaxPositions} min={0} step={1} />
            <NumberField label="Max esposizione live" value={maxExposure} onChange={setMaxExposure} min={0} step={1} suffix="%" />
          </div>
        </div>

        <div style={{ display: "grid", gap: "var(--gap)" }}>
          <div className="card">
            <div className="card-head">
              <span className="title">Timeframe worker</span>
              <span className="badge muted">{timeframes.length}</span>
            </div>
            <div className="card-body" style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {TF_OPTIONS.map((timeframe) => (
                <button
                  key={timeframe}
                  className={"btn sm " + (timeframes.includes(timeframe) ? "primary" : "ghost")}
                  onClick={() => toggleTimeframe(timeframe)}
                  type="button"
                >
                  {timeframe}
                </button>
              ))}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="title">Automazioni</span>
            </div>
            <div className="card-body" style={{ display: "grid", gap: 14 }}>
              <Toggle label="Auto-calibration" checked={autoCalibration} onChange={setAutoCalibration} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "grid", gap: 6 }}>
      <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>{label}</span>
      {children}
    </label>
  );
}

function NumberField({ label, value, onChange, min, max, step = 1, suffix }: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
}) {
  return (
    <Field label={label}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          className="filter"
          type="number"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(event) => onChange(Number(event.target.value))}
          style={{ width: "100%", height: 34, padding: "0 10px", fontSize: "var(--fz-sm)" }}
        />
        {suffix && <span className="muted mono" style={{ fontSize: "var(--fz-xs)", minWidth: 32 }}>{suffix}</span>}
      </div>
    </Field>
  );
}

function Toggle({ label, checked, onChange, danger = false }: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  danger?: boolean;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
      <span style={{ fontSize: "var(--fz-sm)" }}>{label}</span>
      <button
        type="button"
        className={"btn sm " + (checked ? (danger ? "short" : "primary") : "ghost")}
        onClick={() => onChange(!checked)}
        aria-pressed={checked}
      >
        {checked ? "ON" : "OFF"}
      </button>
    </div>
  );
}
