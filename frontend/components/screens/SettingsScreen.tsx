"use client";

import React, { useEffect, useState } from "react";
import { Icon, Seg } from "@/components/atoms";
import {
  AppSettings,
  CoinGeckoUsage,
  CoinMarketCapUsage,
  Health,
  SystemErrorLogs,
  SystemService,
  SystemServiceAction,
  SystemServiceResult,
  apiGet,
  apiPatch,
  apiPost,
} from "@/lib/api";

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
const BTC_PROT_TF_OPTIONS = ["1m", "3m", "5m", "15m", "30m"];

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

/* ─── Tooltip ──────────────────────────────────────────────────────────── */
function InfoTooltip({ content }: { content: string }) {
  const [show, setShow] = useState(false);
  return (
    <span
      style={{ position: "relative", display: "inline-flex", alignItems: "center" }}
      onMouseEnter={() => setShow(true)}
      onMouseLeave={() => setShow(false)}
    >
      <span style={{ color: "var(--fg-4)", lineHeight: 0, cursor: "default" }}>
        <Icon name="info" size={11} />
      </span>
      {show && (
        <span style={{
          position: "absolute",
          bottom: "calc(100% + 6px)",
          left: "50%",
          transform: "translateX(-50%)",
          background: "var(--bg-3)",
          border: "1px solid var(--border-strong)",
          borderRadius: "var(--radius)",
          padding: "8px 10px",
          fontSize: 11,
          color: "var(--fg-2)",
          width: 220,
          zIndex: 200,
          lineHeight: 1.6,
          pointerEvents: "none",
          whiteSpace: "normal",
          boxShadow: "0 4px 20px rgba(0,0,0,.5)",
        }}>
          {content}
        </span>
      )}
    </span>
  );
}

/* ─── Section label ────────────────────────────────────────────────────── */
function SectionDivider({ label }: { label: string }) {
  return (
    <div style={{
      display: "flex",
      alignItems: "center",
      gap: 10,
      margin: "4px 0 2px",
    }}>
      <span style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--fg-4)", whiteSpace: "nowrap" }}>
        {label}
      </span>
      <div style={{ flex: 1, height: 1, background: "var(--border)" }} />
    </div>
  );
}

/* ─── Field wrapper ────────────────────────────────────────────────────── */
function Field({ label, children, tooltip }: { label: string; children: React.ReactNode; tooltip?: string }) {
  return (
    <label style={{ display: "grid", gap: 6 }}>
      <span style={{ display: "flex", alignItems: "center", gap: 5 }}>
        <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>{label}</span>
        {tooltip && <InfoTooltip content={tooltip} />}
      </span>
      {children}
    </label>
  );
}

/* ─── NumberField ──────────────────────────────────────────────────────── */
function NumberField({ label, value, onChange, min, max, step = 1, suffix, tooltip }: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
  tooltip?: string;
}) {
  return (
    <Field label={label} tooltip={tooltip}>
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

/* ─── Toggle (pill switch) ─────────────────────────────────────────────── */
function Toggle({ label, checked, onChange, danger = false, tooltip }: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  danger?: boolean;
  tooltip?: string;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
      <span style={{ display: "flex", alignItems: "center", gap: 5 }}>
        <span style={{
          fontSize: "var(--fz-sm)",
          color: danger && checked ? "var(--short)" : "var(--fg-2)",
        }}>{label}</span>
        {tooltip && <InfoTooltip content={tooltip} />}
      </span>
      <button
        type="button"
        className={"toggle " + (checked ? "on" : "")}
        onClick={() => onChange(!checked)}
        aria-pressed={checked}
        style={danger && checked ? { background: "var(--short)" } : undefined}
      />
    </div>
  );
}

/* ─── Helpers for nested position_management settings ──────────────────── */
function pmTp(settings: AppSettings, key: string, def: number) {
  return toNum((settings.position_management?.takeProfit as Record<string, unknown> | undefined)?.[key], def);
}
function pmTs(settings: AppSettings, key: string, def: number) {
  return toNum((settings.position_management?.trailingStop as Record<string, unknown> | undefined)?.[key], def);
}
function pmTsBool(settings: AppSettings, key: string, def: boolean) {
  return toBool((settings.position_management?.trailingStop as Record<string, unknown> | undefined)?.[key], def);
}

/* ─── Main component ───────────────────────────────────────────────────── */
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
  const [liveMinAccount, setLiveMinAccount] = useState(toNum(settings.live_execution?.min_account_value_usd, 100));
  const [liveNotionalCap, setLiveNotionalCap] = useState(toNum(settings.live_execution?.order_notional_cap_usd, 250));
  const [liveMinNotional, setLiveMinNotional] = useState(toNum(settings.live_execution?.min_order_notional_usd, 10));
  const [liveBumpToMin, setLiveBumpToMin] = useState(toBool(settings.live_execution?.bump_to_min_notional, false));
  const [liveSlippage, setLiveSlippage] = useState(toNum(settings.live_execution?.max_slippage_pct, 0.5));
  const [liveExpires, setLiveExpires] = useState(toNum(settings.live_execution?.order_expires_after_seconds, 30));
  const [mainnetLiveEnabled, setMainnetLiveEnabled] = useState(toBool(settings.live_execution?.enable_mainnet_live_trading, false));
  const [timeframes, setTimeframes] = useState<string[]>(() => enabledTimeframes(settings));
  const [autoCalibration, setAutoCalibration] = useState(toBool(settings.auto_calibration?.enabled, true));
  // Position management — take profit
  const [tpEnabled, setTpEnabled] = useState(() => toBool((settings.position_management?.takeProfit as Record<string, unknown> | undefined)?.enabled, true));
  const [tp1R, setTp1R] = useState(() => pmTp(settings, "tp1R", 1.0));
  const [tp1ClosePct, setTp1ClosePct] = useState(() => pmTp(settings, "tp1ClosePct", 30));
  const [tp2R, setTp2R] = useState(() => pmTp(settings, "tp2R", 2.0));
  const [tp2ClosePct, setTp2ClosePct] = useState(() => pmTp(settings, "tp2ClosePct", 30));
  const [runnerPct, setRunnerPct] = useState(() => pmTp(settings, "runnerPct", 40));
  // Position management — trailing stop
  const [trailingEnabled, setTrailingEnabled] = useState(() => pmTsBool(settings, "enabled", true));
  const [activationR, setActivationR] = useState(() => pmTs(settings, "activationR", 1.0));
  const [atrMultInit, setAtrMultInit] = useState(() => pmTs(settings, "atrMultiplierInitial", 3.0));
  const [atrMultTp1, setAtrMultTp1] = useState(() => pmTs(settings, "atrMultiplierAfterTp1", 2.5));
  const [atrMultTp2, setAtrMultTp2] = useState(() => pmTs(settings, "atrMultiplierAfterTp2", 2.0));
  const [moveBeR, setMoveBeR] = useState(() => pmTs(settings, "moveToBreakevenAtR", 1.0));
  const [lockProfitR, setLockProfitR] = useState(() => pmTs(settings, "lockProfitAtR", 2.0));
  const [lockProfitStopR, setLockProfitStopR] = useState(() => pmTs(settings, "lockProfitStopR", 1.0));
  const [btcProtEnabled, setBtcProtEnabled] = useState(toBool(settings.btc_macro_protection?.enabled, false));
  const [btcProtTimeframe, setBtcProtTimeframe] = useState(String(settings.btc_macro_protection?.timeframe ?? "5m"));
  const [btcProtLookback, setBtcProtLookback] = useState(toNum(settings.btc_macro_protection?.lookback_candles, 6));
  const [btcProtPctThreshold, setBtcProtPctThreshold] = useState(toNum(settings.btc_macro_protection?.reversal_pct_threshold, 1.5));
  const [btcProtVolMultiplier, setBtcProtVolMultiplier] = useState(toNum(settings.btc_macro_protection?.volume_multiplier, 1.2));
  const [btcProtBufferPct, setBtcProtBufferPct] = useState(toNum(settings.btc_macro_protection?.breakeven_buffer_pct, 0.3));
  const [btcProtLossExit, setBtcProtLossExit] = useState(toBool(settings.btc_macro_protection?.loss_exit_enabled, true));
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const [cmcUsage, setCmcUsage] = useState<CoinMarketCapUsage | null>(null);
  const [cmcUsageError, setCmcUsageError] = useState<string | null>(null);
  const [cgUsage, setCgUsage] = useState<CoinGeckoUsage | null>(null);
  const [cgUsageError, setCgUsageError] = useState<string | null>(null);
  const [usageKey, setUsageKey] = useState(0);
  const [systemLogs, setSystemLogs] = useState<SystemErrorLogs | null>(null);
  const [systemLogsError, setSystemLogsError] = useState<string | null>(null);
  const [systemLogsKey, setSystemLogsKey] = useState(0);
  const [serviceBusy, setServiceBusy] = useState<string | null>(null);

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
    setLiveMinAccount(toNum(settings.live_execution?.min_account_value_usd, 100));
    setLiveNotionalCap(toNum(settings.live_execution?.order_notional_cap_usd, 250));
    setLiveMinNotional(toNum(settings.live_execution?.min_order_notional_usd, 10));
    setLiveBumpToMin(toBool(settings.live_execution?.bump_to_min_notional, false));
    setLiveSlippage(toNum(settings.live_execution?.max_slippage_pct, 0.5));
    setLiveExpires(toNum(settings.live_execution?.order_expires_after_seconds, 30));
    setMainnetLiveEnabled(toBool(settings.live_execution?.enable_mainnet_live_trading, false));
    setTimeframes(enabledTimeframes(settings));
    setAutoCalibration(toBool(settings.auto_calibration?.enabled, true));
    setTpEnabled(toBool((settings.position_management?.takeProfit as Record<string, unknown> | undefined)?.enabled, true));
    setTp1R(pmTp(settings, "tp1R", 1.0));
    setTp1ClosePct(pmTp(settings, "tp1ClosePct", 30));
    setTp2R(pmTp(settings, "tp2R", 2.0));
    setTp2ClosePct(pmTp(settings, "tp2ClosePct", 30));
    setRunnerPct(pmTp(settings, "runnerPct", 40));
    setTrailingEnabled(pmTsBool(settings, "enabled", true));
    setActivationR(pmTs(settings, "activationR", 1.0));
    setAtrMultInit(pmTs(settings, "atrMultiplierInitial", 3.0));
    setAtrMultTp1(pmTs(settings, "atrMultiplierAfterTp1", 2.5));
    setAtrMultTp2(pmTs(settings, "atrMultiplierAfterTp2", 2.0));
    setMoveBeR(pmTs(settings, "moveToBreakevenAtR", 1.0));
    setLockProfitR(pmTs(settings, "lockProfitAtR", 2.0));
    setLockProfitStopR(pmTs(settings, "lockProfitStopR", 1.0));
    setBtcProtEnabled(toBool(settings.btc_macro_protection?.enabled, false));
    setBtcProtTimeframe(String(settings.btc_macro_protection?.timeframe ?? "5m"));
    setBtcProtLookback(toNum(settings.btc_macro_protection?.lookback_candles, 6));
    setBtcProtPctThreshold(toNum(settings.btc_macro_protection?.reversal_pct_threshold, 1.5));
    setBtcProtVolMultiplier(toNum(settings.btc_macro_protection?.volume_multiplier, 1.2));
    setBtcProtBufferPct(toNum(settings.btc_macro_protection?.breakeven_buffer_pct, 0.3));
    setBtcProtLossExit(toBool(settings.btc_macro_protection?.loss_exit_enabled, true));
  }, [health, settings]);

  useEffect(() => {
    let mounted = true;
    Promise.allSettled([
      apiGet<CoinMarketCapUsage>("/api/market-intelligence/coinmarketcap-usage"),
      apiGet<CoinGeckoUsage>("/api/market-intelligence/coingecko-usage"),
    ]).then(([cmc, cg]) => {
      if (!mounted) return;
      if (cmc.status === "fulfilled") {
        setCmcUsage(cmc.value);
        setCmcUsageError(null);
      } else {
        setCmcUsage(null);
        setCmcUsageError(cmc.reason instanceof Error ? cmc.reason.message : "Usage CMC non disponibile");
      }
      if (cg.status === "fulfilled") {
        setCgUsage(cg.value);
        setCgUsageError(null);
      } else {
        setCgUsage(null);
        setCgUsageError(cg.reason instanceof Error ? cg.reason.message : "Usage CoinGecko non disponibile");
      }
    });
    return () => { mounted = false; };
  }, [usageKey]);

  useEffect(() => {
    let mounted = true;
    apiGet<SystemErrorLogs>("/api/system/error-logs?limit=20")
      .then((logs) => {
        if (!mounted) return;
        setSystemLogs(logs);
        setSystemLogsError(null);
      })
      .catch((error) => {
        if (!mounted) return;
        setSystemLogs(null);
        setSystemLogsError(error instanceof Error ? error.message : "Log errori non disponibili");
      });
    return () => { mounted = false; };
  }, [systemLogsKey]);

  function refreshUsage() {
    setCmcUsage(null);
    setCmcUsageError(null);
    setCgUsage(null);
    setCgUsageError(null);
    setUsageKey((k) => k + 1);
  }

  function refreshSystemLogs() {
    setSystemLogs(null);
    setSystemLogsError(null);
    setSystemLogsKey((k) => k + 1);
  }

  async function runServiceAction(service: SystemService, action: SystemServiceAction) {
    const key = `${service}:${action}`;
    setServiceBusy(key);
    setToast(null);
    try {
      const result = await apiPost<SystemServiceResult>(`/api/system/services/${service}/${action}`);
      const serviceLabel = result.service === "backend" ? "Backend" : "Worker";
      const actionLabel = action === "start" ? "avvio" : action === "stop" ? "stop" : "reboot";
      setToast({ tone: "success", message: `${serviceLabel}: comando ${actionLabel} inviato` });
      window.setTimeout(() => {
        void Promise.resolve(onRefresh()).catch(() => undefined);
        refreshSystemLogs();
      }, action === "stop" && service === "backend" ? 300 : 900);
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Comando servizio fallito" });
    } finally {
      setServiceBusy(null);
    }
  }

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
          value: mergePatch(settings.operation_mode, { mode, enable_live_trading: liveEnabled }),
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
        live_execution: {
          value: mergePatch(settings.live_execution, {
            min_account_value_usd: liveMinAccount,
            order_notional_cap_usd: liveNotionalCap,
            min_order_notional_usd: liveMinNotional,
            bump_to_min_notional: liveBumpToMin,
            max_slippage_pct: liveSlippage,
            order_expires_after_seconds: liveExpires,
            enable_mainnet_live_trading: mainnetLiveEnabled,
          }),
        },
        timeframes: {
          value: mergePatch(settings.timeframes, { enabled: timeframes }),
        },
        auto_calibration: {
          value: mergePatch(settings.auto_calibration, { enabled: autoCalibration }),
        },
        position_management: {
          value: {
            ...((settings.position_management as Record<string, unknown>) ?? {}),
            takeProfit: {
              ...(((settings.position_management?.takeProfit) as Record<string, unknown>) ?? {}),
              enabled: tpEnabled,
              tp1R, tp1ClosePct, tp2R, tp2ClosePct, runnerPct,
            },
            trailingStop: {
              ...(((settings.position_management?.trailingStop) as Record<string, unknown>) ?? {}),
              enabled: trailingEnabled,
              activationR,
              atrMultiplierInitial: atrMultInit,
              atrMultiplierAfterTp1: atrMultTp1,
              atrMultiplierAfterTp2: atrMultTp2,
              moveToBreakevenAtR: moveBeR,
              lockProfitAtR: lockProfitR,
              lockProfitStopR,
            },
          },
        },
        btc_macro_protection: {
          value: mergePatch(settings.btc_macro_protection, {
            enabled: btcProtEnabled,
            timeframe: btcProtTimeframe,
            lookback_candles: btcProtLookback,
            reversal_pct_threshold: btcProtPctThreshold,
            volume_multiplier: btcProtVolMultiplier,
            breakeven_buffer_pct: btcProtBufferPct,
            loss_exit_enabled: btcProtLossExit,
          }),
        },
      });
      setToast({ tone: "success", message: "Impostazioni salvate" });
      await onRefresh();
      refreshUsage();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Errore salvataggio" });
    } finally {
      setBusy(false);
    }
  }

  const liveArmed = mode === "AUTO_TRADE" && liveEnabled && !killEnabled;
  const isLiveMode = mode === "AUTO_TRADE";
  const isPaperMode = mode === "PAPER_TRADING";

  return (
    <div className="page">
      {/* ── Header ── */}
      <div className="page-head">
        <div>
          <h1 className="page-title">Settings</h1>
          <div className="page-sub">
            {health.network} · {liveArmed ? "live armato" : "live non armato"} · {timeframes.length} timeframe attivi
          </div>
        </div>
        <div className="page-actions">
          <button className="btn ghost" onClick={() => { void onRefresh(); refreshUsage(); refreshSystemLogs(); }}>
            <Icon name="refresh" size={12} />
          </button>
          <button className="btn primary" disabled={busy} onClick={save}>
            <Icon name="check" size={12} /> {busy ? "Salvataggio..." : "Salva"}
          </button>
        </div>
      </div>

      {toast && (
        <div style={{
          padding: "8px 14px",
          marginBottom: "var(--gap)",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          background: toast.tone === "success" ? "var(--long-soft)" : "var(--short-soft)",
          color: toast.tone === "success" ? "var(--long)" : "var(--short)",
          fontSize: "var(--fz-sm)",
        }}>
          {toast.message}
        </div>
      )}

      {/* ── Modalità operativa ── */}
      <SectionDivider label="Modalità operativa" />
      <div className="card" style={{ marginBottom: "var(--gap)", marginTop: "var(--gap)" }}>
        <div className="card-head">
          <span className="title">Controlli di sistema</span>
          <span className={"badge " + (liveArmed ? "short" : killEnabled ? "warn" : "muted")}>
            {killEnabled ? "kill switch ON" : liveArmed ? "live armato" : mode.toLowerCase().replace("_", " ")}
          </span>
        </div>
        <div className="card-body">
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 24 }}>

            {/* Modalità */}
            <div style={{ display: "grid", gap: 10 }}>
              <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Modalità operativa
              </span>
              <Seg value={mode} onChange={setMode} options={MODE_OPTIONS} />
              <div style={{ fontSize: "var(--fz-xs)", color: "var(--fg-3)", lineHeight: 1.5 }}>
                {mode === "SIGNAL_ONLY" && "Genera segnali ma non apre posizioni."}
                {mode === "PAPER_TRADING" && "Simula trade con equity virtuale."}
                {mode === "AUTO_TRADE" && "Esegue ordini reali su Hyperliquid."}
              </div>
            </div>

            {/* Abilitazioni live */}
            <div style={{ display: "grid", gap: 12, alignContent: "start" }}>
              <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Abilitazioni
              </span>
              <Toggle
                label="Enable live trading"
                checked={liveEnabled}
                onChange={setLiveEnabled}
                tooltip="Abilita il motore di esecuzione ordini. Necessario sia in paper che in live."
              />
              <Toggle
                label="Enable mainnet live"
                checked={mainnetLiveEnabled}
                onChange={setMainnetLiveEnabled}
                danger
                tooltip="Abilita l'invio di ordini reali su Hyperliquid mainnet. Richiede fondi reali e private key configurata."
              />
            </div>

            {/* Kill switch + readiness */}
            <div style={{ display: "grid", gap: 12, alignContent: "start" }}>
              <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Sicurezza
              </span>
              <Toggle
                label="Kill switch"
                checked={killEnabled}
                onChange={setKillEnabled}
                danger
                tooltip="Blocca immediatamente tutte le esecuzioni. Ha la precedenza su qualsiasi altra impostazione."
              />
              {health.live_readiness?.checks?.length ? (
                <div style={{ padding: "8px 10px", background: "var(--short-soft)", borderRadius: "var(--radius-sm)", color: "var(--short)", fontSize: "var(--fz-xs)", lineHeight: 1.5 }}>
                  {health.live_readiness.checks.join(" · ")}
                </div>
              ) : (
                <div style={{ fontSize: "var(--fz-xs)", color: "var(--fg-4)", lineHeight: 1.5 }}>
                  DATABASE_URL, network e private key si configurano come env e richiedono restart.
                </div>
              )}
            </div>

          </div>
        </div>
      </div>

      {/* ── Segnali & Filtri ── */}
      <SectionDivider label="Segnali & Filtri · tutti i modi" />
      <div style={{ display: "grid", gridTemplateColumns: "1.3fr 0.7fr", gap: "var(--gap)", marginTop: "var(--gap)", marginBottom: "var(--gap)" }}>

        {/* Filtri segnali */}
        <div className="card">
          <div className="card-head">
            <span className="title">Filtri segnali</span>
            <span className="badge muted">Tutti i modi</span>
          </div>
          <div className="card-body" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 }}>
            <NumberField
              label="Score minimo"
              value={minScore}
              onChange={setMinScore}
              min={0}
              max={100}
              step={1}
              tooltip="Punteggio minimo (0–100) che un segnale deve raggiungere per essere considerato. Valori più alti filtrano i segnali meno certi."
            />
            <NumberField
              label="Risk/reward minimo"
              value={minRR}
              onChange={setMinRR}
              min={0}
              step={0.1}
              tooltip="Rapporto minimo take profit / stop loss. Es: 2 significa che il TP deve valere almeno il doppio dello SL."
            />
            <NumberField
              label="Volume medio minimo"
              value={minVolume}
              onChange={setMinVolume}
              min={0}
              step={1000}
              suffix="USD"
              tooltip="Volume medio giornaliero richiesto sul mercato. Filtra asset illiquidi dove lo slippage sarebbe elevato."
            />
          </div>
        </div>

        {/* Timeframe + Calibrazione */}
        <div style={{ display: "grid", gap: "var(--gap)" }}>
          <div className="card">
            <div className="card-head">
              <span className="title">Timeframe worker</span>
              <span className="badge muted">{timeframes.length} attivi</span>
            </div>
            <div className="card-body" style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {TF_OPTIONS.map((tf) => (
                <button
                  key={tf}
                  className={"btn sm " + (timeframes.includes(tf) ? "primary" : "ghost")}
                  onClick={() => toggleTimeframe(tf)}
                  type="button"
                >
                  {tf}
                </button>
              ))}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="title">Automazioni</span>
            </div>
            <div className="card-body">
              <Toggle
                label="Auto-calibration"
                checked={autoCalibration}
                onChange={setAutoCalibration}
                tooltip="Adatta automaticamente i parametri di scoring in base alla performance storica dei trade chiusi."
              />
            </div>
          </div>
        </div>
      </div>

      {/* ── Gestione del rischio ── */}
      <SectionDivider label="Gestione del rischio" />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--gap)", marginTop: "var(--gap)", marginBottom: "var(--gap)" }}>

        {/* Rischio comune */}
        <div className="card">
          <div className="card-head">
            <span className="title">Position sizing</span>
            <span className="badge muted">Tutti i modi</span>
          </div>
          <div className="card-body" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <NumberField
              label="Risk per trade"
              value={riskPct}
              onChange={setRiskPct}
              step={0.1}
              suffix="%"
              tooltip="Percentuale del capitale a rischio per ogni trade. Es: 1% su $10.000 = rischio massimo $100 per posizione."
            />
            <NumberField
              label="Max leverage"
              value={maxLeverage}
              onChange={setMaxLeverage}
              step={0.1}
              suffix="x"
              tooltip="Leva massima applicata ai trade. Influenza la size dell'ordine calcolata dallo stop loss."
            />
          </div>
        </div>

        {/* Paper trading */}
        <div className="card" style={!isPaperMode ? { opacity: 0.65 } : undefined}>
          <div className="card-head">
            <span className="title">Paper trading</span>
            <span className={"badge " + (isPaperMode ? "info" : "muted")}>
              {isPaperMode ? "modalità attiva" : "PAPER_TRADING"}
            </span>
          </div>
          <div className="card-body">
            <NumberField
              label="Equity virtuale"
              value={paperEquity}
              onChange={setPaperEquity}
              step={100}
              suffix="USD"
              tooltip="Capitale iniziale simulato usato in paper trading per calcolare la size degli ordini virtuali."
            />
          </div>
        </div>
      </div>

      {/* ── Guardrail live ── */}
      <SectionDivider label="Guardrail live · solo AUTO_TRADE" />
      <div className="card" style={{ marginTop: "var(--gap)", marginBottom: "var(--gap)", ...(!isLiveMode ? { opacity: 0.65 } : {}) }}>
        <div className="card-head">
          <span className="title">Limiti di esposizione</span>
          <span className={"badge " + (isLiveMode ? "short" : "muted")}>
            {isLiveMode ? "AUTO_TRADE" : "non attivo"}
          </span>
        </div>
        <div className="card-body" style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 14 }}>
          <NumberField
            label="Max posizioni aperte"
            value={maxPositions}
            onChange={setMaxPositions}
            min={0}
            step={1}
            tooltip="Numero massimo di posizioni live aperte contemporaneamente. Nuovi segnali vengono ignorati se il limite è raggiunto."
          />
          <NumberField
            label="Max esposizione totale"
            value={maxExposure}
            onChange={setMaxExposure}
            min={0}
            step={1}
            suffix="%"
            tooltip="Esposizione totale massima su tutte le posizioni aperte, come percentuale del valore account."
          />
          <NumberField
            label="Max daily loss"
            value={dailyLoss}
            onChange={setDailyLoss}
            min={0}
            step={0.1}
            suffix="%"
            tooltip="Perdita giornaliera massima tollerata. Se raggiunta, il sistema smette di aprire nuove posizioni fino al giorno successivo."
          />
        </div>
      </div>

      {/* ── Esecuzione ordini live ── */}
      <div className="card" style={{ marginBottom: "var(--gap)", ...(!isLiveMode ? { opacity: 0.65 } : {}) }}>
        <div className="card-head">
          <span className="title">Esecuzione ordini</span>
          <span className={"badge " + (health.live_trading_allowed ? "long" : isLiveMode ? "short" : "muted")}>
            {health.live_trading_allowed ? "ready" : isLiveMode ? "blocked" : "non attivo"}
          </span>
        </div>
        <div className="card-body" style={{ display: "grid", gap: 16 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 14 }}>
            <NumberField
              label="Min account"
              value={liveMinAccount}
              onChange={setLiveMinAccount}
              min={0}
              step={10}
              suffix="USD"
              tooltip="Valore minimo richiesto sull'account Hyperliquid per aprire nuove posizioni. Protezione contro account sotto-capitalizzati."
            />
            <NumberField
              label="Order cap"
              value={liveNotionalCap}
              onChange={setLiveNotionalCap}
              min={0}
              step={10}
              suffix="USD"
              tooltip="Valore massimo in USD di un singolo ordine, indipendentemente dalla size calcolata dal risk_pct."
            />
            <NumberField
              label="Min notional"
              value={liveMinNotional}
              onChange={setLiveMinNotional}
              min={0}
              step={1}
              suffix="USD"
              tooltip="Valore minimo in USD per aprire un ordine su Hyperliquid. Ordini sotto questa soglia vengono scartati (o aumentati se bump_to_min è attivo)."
            />
            <NumberField
              label="Max slippage"
              value={liveSlippage}
              onChange={setLiveSlippage}
              min={0}
              step={0.1}
              suffix="%"
              tooltip="Slippage massimo tollerato sull'esecuzione. L'ordine viene annullato se il prezzo di fill supera questa percentuale rispetto al prezzo del segnale."
            />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, alignItems: "center" }}>
            <NumberField
              label="Order expiry"
              value={liveExpires}
              onChange={setLiveExpires}
              min={0}
              step={1}
              suffix="s"
              tooltip="Secondi dopo i quali un ordine aperto e non ancora eseguito viene cancellato automaticamente."
            />
            <Toggle
              label="Bump to min notional"
              checked={liveBumpToMin}
              onChange={setLiveBumpToMin}
              danger
              tooltip="Se la size calcolata è sotto il minimo Hyperliquid, aumenta l'ordine al minimo consentito. ATTENZIONE: viola il risk_pct configurato."
            />
          </div>
        </div>
      </div>

      {/* ── Gestione posizioni ── */}
      <SectionDivider label="Gestione posizioni · paper e live" />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--gap)", marginTop: "var(--gap)", marginBottom: "var(--gap)" }}>

        {/* Take profit plan */}
        <div className="card">
          <div className="card-head">
            <span className="title">Take profit plan</span>
            <span className={"badge " + (tpEnabled ? "long" : "muted")}>{tpEnabled ? "attivo" : "disabilitato"}</span>
          </div>
          <div className="card-body" style={{ display: "grid", gap: 16 }}>
            <Toggle
              label="Abilitato"
              checked={tpEnabled}
              onChange={setTpEnabled}
              tooltip="Se disabilitato, non vengono piazzati ordini TP e la posizione viene gestita interamente dallo stop."
            />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 }}>
              <NumberField label="TP1 R-multiplo" value={tp1R} onChange={setTp1R} min={0.1} step={0.1} suffix="R"
                tooltip="Distanza del primo take profit in multipli del rischio iniziale. Es: 1R su entry 100 con SL a 95 → TP1 a 105." />
              <NumberField label="TP1 close %" value={tp1ClosePct} onChange={setTp1ClosePct} min={1} max={99} step={1} suffix="%"
                tooltip="Percentuale della posizione da chiudere quando TP1 viene raggiunto." />
              <div />
              <NumberField label="TP2 R-multiplo" value={tp2R} onChange={setTp2R} min={0.1} step={0.1} suffix="R"
                tooltip="Distanza del secondo take profit. Deve essere maggiore di TP1." />
              <NumberField label="TP2 close %" value={tp2ClosePct} onChange={setTp2ClosePct} min={1} max={99} step={1} suffix="%"
                tooltip="Percentuale della posizione da chiudere quando TP2 viene raggiunto." />
              <NumberField label="Runner %" value={runnerPct} onChange={setRunnerPct} min={0} max={99} step={1} suffix="%"
                tooltip="Quota residua lasciata aperta dopo TP1 e TP2. Non ha un ordine TP fisso: viene chiusa solo dallo stop in trailing." />
            </div>
            <div style={{ padding: "6px 10px", background: "var(--bg-2)", borderRadius: "var(--radius-sm)", fontSize: "var(--fz-xs)", color: "var(--fg-3)" }}>
              TP1 + TP2 + Runner = {tp1ClosePct + tp2ClosePct + runnerPct}%
              {tp1ClosePct + tp2ClosePct + runnerPct !== 100 && (
                <span style={{ color: "var(--short)", marginLeft: 8 }}>· deve essere 100%</span>
              )}
            </div>
          </div>
        </div>

        {/* Trailing stop */}
        <div className="card">
          <div className="card-head">
            <span className="title">Trailing stop</span>
            <span className={"badge " + (trailingEnabled ? "long" : "muted")}>{trailingEnabled ? "attivo" : "disabilitato"}</span>
          </div>
          <div className="card-body" style={{ display: "grid", gap: 16 }}>
            <Toggle
              label="Abilitato"
              checked={trailingEnabled}
              onChange={setTrailingEnabled}
              tooltip="Gestisce il runner spostando lo stop in favore della posizione man mano che il prezzo avanza."
            />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
              <NumberField label="Attivazione" value={activationR} onChange={setActivationR} min={0} step={0.1} suffix="R"
                tooltip="Il trailing stop inizia a seguire il prezzo solo quando il guadagno raggiunge questo multiplo del rischio iniziale." />
              <NumberField label="Breakeven a" value={moveBeR} onChange={setMoveBeR} min={0} step={0.1} suffix="R"
                tooltip="Quando il guadagno raggiunge questo R, lo stop viene spostato al prezzo di entry (breakeven)." />
              <NumberField label="Lock profit a" value={lockProfitR} onChange={setLockProfitR} min={0} step={0.1} suffix="R"
                tooltip="Quando il guadagno raggiunge questo R, lo stop viene bloccato a lockProfitStopR dal rischio." />
              <NumberField label="Lock profit stop" value={lockProfitStopR} onChange={setLockProfitStopR} min={0} step={0.1} suffix="R"
                tooltip="Posizione dello stop bloccato, in R dal entry. Es: 1R → stop a entry + 1× rischio." />
            </div>
            <div>
              <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", display: "block", marginBottom: 8 }}>
                ATR multiplier (ampiezza trailing)
              </span>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 }}>
                <NumberField label="Iniziale" value={atrMultInit} onChange={setAtrMultInit} min={0.1} step={0.1} suffix="×"
                  tooltip="Moltiplicatore ATR per calcolare lo stop iniziale. Più alto = stop più lontano = meno falsi trigger." />
                <NumberField label="Dopo TP1" value={atrMultTp1} onChange={setAtrMultTp1} min={0.1} step={0.1} suffix="×"
                  tooltip="Moltiplicatore più stretto dopo che TP1 è stato raggiunto." />
                <NumberField label="Runner" value={atrMultTp2} onChange={setAtrMultTp2} min={0.1} step={0.1} suffix="×"
                  tooltip="Moltiplicatore più stretto quando il runner è attivo (dopo TP2)." />
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* ── BTC Macro Protection ── */}
      <SectionDivider label="BTC Macro Protection · solo AUTO_TRADE" />
      <BtcMacroProtectionCard
        isLiveMode={isLiveMode}
        settings={settings}
        enabled={btcProtEnabled}
        onEnabled={setBtcProtEnabled}
        timeframe={btcProtTimeframe}
        onTimeframe={setBtcProtTimeframe}
        lookback={btcProtLookback}
        onLookback={setBtcProtLookback}
        pctThreshold={btcProtPctThreshold}
        onPctThreshold={setBtcProtPctThreshold}
        volMultiplier={btcProtVolMultiplier}
        onVolMultiplier={setBtcProtVolMultiplier}
        bufferPct={btcProtBufferPct}
        onBufferPct={setBtcProtBufferPct}
        lossExit={btcProtLossExit}
        onLossExit={setBtcProtLossExit}
      />

      {/* ── Monitoraggio ── */}
      <SectionDivider label="Monitoraggio" />
      <div style={{ marginTop: "var(--gap)" }}>
        <WorkerStatusPanel worker={health.worker} serviceBusy={serviceBusy} onServiceAction={runServiceAction} />
        <SystemErrorLogsPanel logs={systemLogs} error={systemLogsError} onRefresh={refreshSystemLogs} />
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--gap)" }}>
          <CoinMarketCapUsagePanel usage={cmcUsage} error={cmcUsageError} />
          <CoinGeckoUsagePanel usage={cgUsage} error={cgUsageError} />
        </div>
      </div>
    </div>
  );
}

/* ─── BTC Macro Protection card ────────────────────────────────────────── */
function BtcMacroProtectionCard({
  isLiveMode, settings,
  enabled, onEnabled,
  timeframe, onTimeframe,
  lookback, onLookback,
  pctThreshold, onPctThreshold,
  volMultiplier, onVolMultiplier,
  bufferPct, onBufferPct,
  lossExit, onLossExit,
}: {
  isLiveMode: boolean;
  settings: AppSettings;
  enabled: boolean; onEnabled: (v: boolean) => void;
  timeframe: string; onTimeframe: (v: string) => void;
  lookback: number; onLookback: (v: number) => void;
  pctThreshold: number; onPctThreshold: (v: number) => void;
  volMultiplier: number; onVolMultiplier: (v: number) => void;
  bufferPct: number; onBufferPct: (v: number) => void;
  lossExit: boolean; onLossExit: (v: boolean) => void;
}) {
  const lastTrend = String(settings.btc_macro_protection?._last_btc_trend ?? "NEUTRAL");
  const lastDelta = settings.btc_macro_protection?._last_delta_pct;
  const lastChecked = typeof settings.btc_macro_protection?._last_checked_at === "string"
    ? settings.btc_macro_protection._last_checked_at as string
    : null;
  const trendColor = lastTrend === "BULLISH" ? "var(--long)" : lastTrend === "BEARISH" ? "var(--short)" : "var(--fg-4)";
  const badgeClass = enabled && isLiveMode ? "long" : enabled ? "warn" : "muted";
  const badgeLabel = enabled && isLiveMode ? "attiva" : enabled ? "abilitata · non in live" : "disabilitata";

  return (
    <div className="card" style={{ marginTop: "var(--gap)", marginBottom: "var(--gap)", ...(!isLiveMode ? { opacity: 0.65 } : {}) }}>
      <div className="card-head">
        <span className="title">Protezione macro BTC</span>
        <span className={"badge " + badgeClass}>{badgeLabel}</span>
      </div>
      <div className="card-body" style={{ display: "grid", gap: 18 }}>

        {/* Riga 1 – toggle abilitazione + stato rilevato */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, alignItems: "center" }}>
          <Toggle
            label="Abilitata"
            checked={enabled}
            onChange={onEnabled}
            tooltip="Attiva il monitoraggio del trend BTC. Quando BTC subisce un cambio repentino di trend, le posizioni altcoin esposte vengono protette automaticamente."
          />
          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 10px", background: "var(--bg-2)", borderRadius: "var(--radius-sm)" }}>
            <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", whiteSpace: "nowrap" }}>Ultimo trend BTC</span>
            <span style={{ fontSize: "var(--fz-sm)", fontWeight: 600, color: trendColor }}>{lastTrend}</span>
            {typeof lastDelta === "number" && (
              <span className="mono muted" style={{ fontSize: "var(--fz-xs)" }}>
                {lastDelta > 0 ? "+" : ""}{Number(lastDelta).toFixed(2)}%
              </span>
            )}
            {lastChecked && (
              <span className="muted" style={{ fontSize: "var(--fz-xs)", marginLeft: "auto" }}>
                {new Date(String(lastChecked)).toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" })}
              </span>
            )}
          </div>
        </div>

        {/* Riga 2 – parametri di detection */}
        <div>
          <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", display: "block", marginBottom: 10 }}>
            Rilevamento reversal
          </span>
          <div style={{ display: "grid", gridTemplateColumns: "auto 1fr 1fr 1fr", gap: 14, alignItems: "end" }}>
            <div style={{ display: "grid", gap: 6 }}>
              <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Timeframe BTC <InfoTooltip content="Timeframe delle candele BTC usate per calcolare il delta di prezzo. Timeframe più corti = reazione più rapida ma più falsi positivi." />
              </span>
              <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                {BTC_PROT_TF_OPTIONS.map((tf) => (
                  <button
                    key={tf}
                    type="button"
                    className={"btn sm " + (timeframe === tf ? "primary" : "ghost")}
                    onClick={() => onTimeframe(tf)}
                  >
                    {tf}
                  </button>
                ))}
              </div>
            </div>
            <NumberField
              label="Finestra candele"
              value={lookback}
              onChange={onLookback}
              min={2}
              max={50}
              step={1}
              tooltip="Numero di candele su cui calcolare il delta di prezzo. Es: 6 candele × 5m = 30 minuti di finestra temporale."
            />
            <NumberField
              label="Soglia delta prezzo"
              value={pctThreshold}
              onChange={onPctThreshold}
              min={0.1}
              step={0.1}
              suffix="%"
              tooltip="Variazione percentuale minima del prezzo BTC nella finestra per considerare il segnale valido. Valori più alti = meno segnali ma più affidabili."
            />
            <NumberField
              label="Moltiplicatore volume"
              value={volMultiplier}
              onChange={onVolMultiplier}
              min={0.1}
              step={0.1}
              suffix="×"
              tooltip="Il volume dell'ultima candela deve essere almeno questo multiplo del volume medio della finestra. Filtra i falsi positivi da basso volume."
            />
          </div>
        </div>

        {/* Riga 3 – reazione alle posizioni */}
        <div>
          <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", display: "block", marginBottom: 10 }}>
            Reazione alle posizioni
          </span>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <NumberField
              label="Buffer breakeven SL"
              value={bufferPct}
              onChange={onBufferPct}
              min={0}
              step={0.05}
              suffix="%"
              tooltip="Offset percentuale rispetto all'entry per il nuovo stop loss di protezione. Es: 0.3% su SHORT a 100$ → nuovo SL a 100.30$. Applicato solo se migliora lo stop esistente."
            />
            <Toggle
              label="Chiudi se in perdita"
              checked={lossExit}
              onChange={onLossExit}
              danger
              tooltip="Se la posizione è in perdita al momento del reversal BTC, la chiude immediatamente invece di spostare lo stop. Prioritizza la protezione del capitale."
            />
          </div>
        </div>

        {/* Nota esplicativa */}
        <div style={{ padding: "8px 12px", background: "var(--bg-2)", borderRadius: "var(--radius-sm)", fontSize: "var(--fz-xs)", color: "var(--fg-3)", lineHeight: 1.6 }}>
          Il reversal si attiva solo al cambio <span style={{ color: "var(--long)" }}>BULLISH</span> ↔ <span style={{ color: "var(--short)" }}>BEARISH</span> (non da/verso NEUTRAL).
          Le posizioni SHORT sono protette sui reversal bullish, le LONG sui reversal bearish.
          Il timeframe BTC deve essere tra i timeframe worker attivi.
        </div>
      </div>
    </div>
  );
}

/* ─── Worker status panel ──────────────────────────────────────────────── */
function WorkerStatusPanel({
  worker,
  serviceBusy,
  onServiceAction,
}: {
  worker: Health["worker"];
  serviceBusy: string | null;
  onServiceAction: (service: SystemService, action: SystemServiceAction) => void | Promise<void>;
}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  if (!worker) {
    return (
      <div className="card" style={{ marginBottom: "var(--gap)" }}>
        <div className="card-head">
          <span className="title">Worker</span>
          <span className="badge muted">no data</span>
        </div>
        <div className="card-body" style={{ color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
          Stato worker non disponibile.
          <ServiceControlGrid serviceBusy={serviceBusy} onServiceAction={onServiceAction} />
        </div>
      </div>
    );
  }

  const lastRunMs = worker.last_run_at ? new Date(worker.last_run_at).getTime() : null;
  const liveSeconds = lastRunMs != null
    ? Math.max(0, Math.round((now - lastRunMs) / 1000))
    : worker.seconds_since_last_run ?? null;
  const healthy = worker.healthy;
  const cycle = worker.last_cycle ?? {};
  const timeframesCount = countTimeframes(cycle.timeframes);
  const activeCount = countTimeframes(cycle.active_timeframes);
  const errorAt = worker.last_error_at ? new Date(worker.last_error_at).getTime() : null;
  const errorIsRecent = errorAt != null && lastRunMs != null && errorAt > lastRunMs;

  // Heartbeat color: green < 30s, yellow 30–90s, red > 90s
  const heartbeatColor = !healthy
    ? "var(--short)"
    : liveSeconds == null
      ? "var(--fg-4)"
      : liveSeconds < 30
        ? "var(--long)"
        : liveSeconds < 90
          ? "var(--warn)"
          : "var(--short)";

  const heartbeatLabel = liveSeconds != null
    ? liveSeconds < 60 ? `${liveSeconds}s fa` : `${Math.floor(liveSeconds / 60)}m ${liveSeconds % 60}s fa`
    : "n/d";

  const statusLabel = healthy
    ? worker.phase ? worker.phase.toLowerCase() : "alive"
    : liveSeconds == null ? "no heartbeat" : "stale";

  return (
    <div className="card" style={{ marginBottom: "var(--gap)" }}>
      <div className="card-head">
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="title">Worker</span>
          {/* pulsing dot */}
          <span style={{
            width: 7, height: 7, borderRadius: "50%",
            background: healthy ? "var(--long)" : "var(--short)",
            boxShadow: healthy ? "0 0 0 2px var(--long-soft)" : "0 0 0 2px var(--short-soft)",
            flexShrink: 0,
          }} />
        </span>
        <span className={"badge " + (healthy ? "long" : "short")}>{statusLabel}</span>
      </div>

      <div className="card-body" style={{ display: "grid", gap: 16 }}>

        {/* Status row */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12 }}>
          <div style={{ display: "grid", gap: 4, minWidth: 0 }}>
            <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Heartbeat</span>
            <span className="mono" style={{ fontSize: "var(--fz-sm)", color: heartbeatColor, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {heartbeatLabel}
            </span>
          </div>
          <UsageMetric
            label="Ultimo ciclo"
            value={worker.last_cycle_at ? formatDateTime(worker.last_cycle_at) : "in corso"}
          />
          <UsageMetric label="Timeframe" value={`${activeCount}/${timeframesCount}`} />
          <UsageMetric label="Mercati" value={formatCompact(readNumber(cycle.markets))} />
        </div>

        {/* Cycle stats — inline */}
        <WorkerCycleInline cycle={cycle} />

        <ServiceControlGrid serviceBusy={serviceBusy} onServiceAction={onServiceAction} />

        {errorIsRecent && worker.last_error_message && (
          <div style={{ padding: 10, background: "var(--short-soft)", borderRadius: "var(--radius-sm)", color: "var(--short)", fontSize: "var(--fz-sm)", lineHeight: 1.5 }}>
            <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", display: "block", marginBottom: 4 }}>
              Errore · {worker.last_error_at ? formatDateTime(worker.last_error_at) : ""}
            </span>
            {worker.last_error_message}
          </div>
        )}
      </div>
    </div>
  );
}

function ServiceControlGrid({
  serviceBusy,
  onServiceAction,
}: {
  serviceBusy: string | null;
  onServiceAction: (service: SystemService, action: SystemServiceAction) => void | Promise<void>;
}) {
  const services: { id: SystemService; label: string }[] = [
    { id: "worker", label: "Worker" },
    { id: "backend", label: "Backend" },
  ];
  const actions: { id: SystemServiceAction; label: string; icon: string; className: string }[] = [
    { id: "stop", label: "Stop", icon: "power", className: "btn sm danger" },
    { id: "start", label: "Avvio", icon: "check", className: "btn sm ghost" },
    { id: "reboot", label: "Reboot", icon: "refresh", className: "btn sm" },
  ];

  return (
    <div style={{ display: "grid", gap: 10, paddingTop: 4 }}>
      <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>
        LaunchAgents
      </span>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        {services.map((service) => (
          <div key={service.id} style={{ display: "grid", gap: 8, padding: "8px 10px", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", background: "var(--bg-1)" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <span style={{ fontSize: "var(--fz-sm)", fontWeight: 600 }}>{service.label}</span>
              <span className="mono muted" style={{ fontSize: "var(--fz-xs)" }}>com.trado.{service.id}</span>
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {actions.map((action) => {
                const busy = serviceBusy === `${service.id}:${action.id}`;
                return (
                  <button
                    key={action.id}
                    type="button"
                    className={action.className}
                    disabled={serviceBusy != null}
                    onClick={() => { void onServiceAction(service.id, action.id); }}
                  >
                    <Icon name={action.icon} size={11} />
                    {busy ? "Invio..." : action.label}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function SystemErrorLogsPanel({
  logs,
  error,
  onRefresh,
}: {
  logs: SystemErrorLogs | null;
  error: string | null;
  onRefresh: () => void;
}) {
  return (
    <div className="card" style={{ marginBottom: "var(--gap)" }}>
      <div className="card-head">
        <span className="title">Error log</span>
        <button type="button" className="btn sm ghost" onClick={onRefresh}>
          <Icon name="refresh" size={11} />
          Aggiorna
        </button>
      </div>
      <div className="card-body" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        {error ? (
          <div style={{ gridColumn: "1 / -1", color: "var(--short)", fontSize: "var(--fz-sm)" }}>{error}</div>
        ) : (
          <>
            <ErrorLogBox title="Backend" log={logs?.backend ?? null} />
            <ErrorLogBox title="Worker" log={logs?.worker ?? null} />
          </>
        )}
      </div>
    </div>
  );
}

function ErrorLogBox({
  title,
  log,
}: {
  title: string;
  log: SystemErrorLogs[keyof SystemErrorLogs] | null;
}) {
  const lines = log?.lines ?? [];
  const updated = log?.updated_at ? formatDateTime(log.updated_at) : null;
  return (
    <div style={{ display: "grid", gap: 8, minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>
          {title}
        </span>
        <span className="mono muted" style={{ fontSize: "var(--fz-xs)" }}>
          {updated ?? (log?.exists === false ? "file assente" : "loading")}
        </span>
      </div>
      <pre style={{
        margin: 0,
        minHeight: 180,
        maxHeight: 260,
        overflow: "auto",
        padding: 10,
        background: "var(--bg-2)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-sm)",
        color: lines.length ? "var(--fg-2)" : "var(--fg-4)",
        fontSize: "var(--fz-xs)",
        lineHeight: 1.55,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}>
        {lines.length ? lines.join("\n") : log ? "Nessun errore recente." : "Caricamento..."}
      </pre>
      {log?.path && (
        <span className="mono muted" style={{ fontSize: 10, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {log.path}
        </span>
      )}
    </div>
  );
}

/* ─── Worker cycle inline summary ──────────────────────────────────────── */
type WorkerCycleData = NonNullable<NonNullable<Health["worker"]>["last_cycle"]>;

function WorkerCycleInline({ cycle }: { cycle: WorkerCycleData }) {
  const items: { label: string; value: number | null }[] = [
    { label: "synced", value: readNumber(cycle.synced) },
    { label: "skipped", value: readNumber(cycle.skipped) },
    { label: "segnali", value: readNumber(cycle.generated) },
    { label: "chiuse", value: readNumber(cycle.closed) },
    { label: "pnl", value: readNumber(cycle.pnl_updated) },
    { label: "recon.", value: readNumber(cycle.live_reconciled) },
    { label: "calib.", value: readNumber(cycle.calibrated) },
  ];

  const hasAny = items.some((item) => item.value != null && item.value > 0);

  return (
    <div style={{ display: "flex", alignItems: "baseline", flexWrap: "wrap", gap: "0 6px", fontSize: "var(--fz-xs)", lineHeight: 1.8 }}>
      <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginRight: 4, whiteSpace: "nowrap" }}>
        Ciclo
      </span>
      {!hasAny ? (
        <span className="muted">nessun dato</span>
      ) : (
        items.map((item, i) => {
          if (item.value == null) return null;
          const isZero = item.value === 0;
          return (
            <React.Fragment key={item.label}>
              {i > 0 && <span className="muted" style={{ userSelect: "none" }}>·</span>}
              <span style={{ color: isZero ? "var(--fg-4)" : "var(--fg-1)" }}>
                <span className="mono" style={{ fontWeight: isZero ? 400 : 600 }}>{formatCompact(item.value)}</span>
                {" "}
                <span className="muted">{item.label}</span>
              </span>
            </React.Fragment>
          );
        })
      )}
    </div>
  );
}

function countTimeframes(value: unknown): number {
  if (Array.isArray(value)) return value.length;
  const num = Number(value);
  return Number.isFinite(num) ? num : 0;
}

/* ─── CMC usage panel ──────────────────────────────────────────────────── */
function CoinMarketCapUsagePanel({ usage, error }: { usage: CoinMarketCapUsage | null; error: string | null }) {
  const plan = usage?.plan ?? {};
  const usageData = usage?.usage ?? {};
  const minute = asRecord(usageData.current_minute);
  const day = asRecord(usageData.current_day);
  const month = asRecord(usageData.current_month);
  const minuteUsed = readNumber(minute.requests_made);
  const minuteLeft = readNumber(minute.requests_left);
  const dayUsed = readNumber(day.credits_used);
  const dayLeft = readNumber(day.credits_left);
  const monthUsed = readNumber(month.credits_used);
  const monthLeft = readNumber(month.credits_left);
  const minuteLimit = readNumber(plan.rate_limit_minute) ?? sumKnown(minuteUsed, minuteLeft);
  const dayLimit = sumKnown(dayUsed, dayLeft);
  const monthLimit = readNumber(plan.credit_limit_monthly) ?? sumKnown(monthUsed, monthLeft);
  const resetAt = typeof plan.credit_limit_monthly_reset_timestamp === "string" ? plan.credit_limit_monthly_reset_timestamp : null;
  const resetText = typeof plan.credit_limit_monthly_reset === "string" ? plan.credit_limit_monthly_reset : null;
  const status = !usage
    ? (error ? "error" : "loading")
    : !usage.enabled
      ? "disabled"
      : usage.available
        ? "available"
        : "paused";

  return (
    <div className="card" style={{ marginBottom: "var(--gap)" }}>
      <div className="card-head">
        <span className="title">CoinMarketCap usage</span>
        <span className={"badge " + (status === "available" ? "long" : status === "loading" ? "muted" : "short")}>
          {status}
        </span>
      </div>
      <div className="card-body" style={{ display: "grid", gap: 14 }}>
        {error ? (
          <div style={{ color: "var(--short)", fontSize: "var(--fz-sm)" }}>{error}</div>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12 }}>
              <UsageMetric label="Mese" value={formatPair(monthUsed, monthLimit)} />
              <UsageMetric label="Oggi" value={formatPair(dayUsed, dayLimit)} />
              <UsageMetric label="Minuto" value={formatPair(minuteUsed, minuteLimit)} />
              <UsageMetric label="Reset mese" value={formatReset(resetAt, resetText)} />
            </div>
            <div style={{ display: "grid", gap: 10 }}>
              <UsageBar label="Crediti mese" used={monthUsed} left={monthLeft} limit={monthLimit} />
              <UsageBar label="Crediti oggi" used={dayUsed} left={dayLeft} limit={dayLimit} />
              <UsageBar label="Rate limit minuto" used={minuteUsed} left={minuteLeft} limit={minuteLimit} />
            </div>
            {usage?.disabled_until && (
              <div className="muted" style={{ fontSize: "var(--fz-xs)" }}>
                Pausa fino a <span className="mono">{formatDateTime(usage.disabled_until)}</span>
                {usage.disabled_reason ? ` · ${usage.disabled_reason}` : ""}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/* ─── CoinGecko usage panel ────────────────────────────────────────────── */
function CoinGeckoUsagePanel({ usage, error }: { usage: CoinGeckoUsage | null; error: string | null }) {
  const data = usage?.data ?? {};
  const used = readNumber(data.current_total_monthly_calls);
  const left = readNumber(data.current_remaining_monthly_calls);
  const planLimit = readNumber(data.monthly_call_credit);
  const keyLimit = readNumber(data.api_key_monthly_call_credit);
  const limit = keyLimit ?? planLimit ?? sumKnown(used, left);
  const minuteLimit = readNumber(data.api_key_rate_limit_request_per_minute) ?? readNumber(data.rate_limit_request_per_minute);
  const plan = typeof data.plan === "string" ? data.plan : "n/d";
  const status = !usage ? (error ? "error" : "loading") : usage.enabled ? "available" : "disabled";

  return (
    <div className="card" style={{ marginBottom: "var(--gap)" }}>
      <div className="card-head">
        <span className="title">CoinGecko usage</span>
        <span className={"badge " + (status === "available" ? "long" : status === "loading" ? "muted" : "short")}>
          {status}
        </span>
      </div>
      <div className="card-body" style={{ display: "grid", gap: 14 }}>
        {error ? (
          <div style={{ color: "var(--short)", fontSize: "var(--fz-sm)" }}>{error}</div>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 12 }}>
              <UsageMetric label="Mese" value={formatPair(used, limit)} />
              <UsageMetric label="Residui" value={formatCompact(left)} />
              <UsageMetric label="Rate/min" value={formatCompact(minuteLimit)} />
            </div>
            <UsageBar label={`Crediti mese · ${plan}`} used={used} left={left} limit={limit} />
            <div className="muted" style={{ fontSize: "var(--fz-xs)" }}>
              Reset mensile non restituito da endpoint CoinGecko.
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ─── Micro-components ─────────────────────────────────────────────────── */
function UsageMetric({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "grid", gap: 4, minWidth: 0 }}>
      <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>{label}</span>
      <span className="mono" style={{ fontSize: "var(--fz-sm)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{value}</span>
    </div>
  );
}

function UsageBar({ label, used, left, limit }: { label: string; used: number | null; left: number | null; limit: number | null }) {
  const safeLimit = limit && limit > 0 ? limit : null;
  const pct = safeLimit && used != null ? Math.max(0, Math.min(100, (used / safeLimit) * 100)) : 0;
  const exhausted = left != null && left <= 0;
  return (
    <div style={{ display: "grid", gap: 5 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: "var(--fz-xs)" }}>
        <span>{label}</span>
        <span className="mono muted">{formatPair(used, safeLimit)}</span>
      </div>
      <div style={{ height: 8, background: "var(--bg-2)", borderRadius: 999, overflow: "hidden" }}>
        <div style={{
          width: `${pct}%`,
          height: "100%",
          background: exhausted ? "var(--short)" : pct >= 80 ? "var(--warn)" : "var(--long)",
        }} />
      </div>
    </div>
  );
}

/* ─── Pure utilities ───────────────────────────────────────────────────── */
function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readNumber(value: unknown): number | null {
  const next = Number(value);
  return Number.isFinite(next) ? next : null;
}

function sumKnown(left: number | null, right: number | null): number | null {
  return left != null && right != null ? left + right : null;
}

function formatPair(used: number | null, limit: number | null): string {
  if (used == null && limit == null) return "n/d";
  if (limit == null) return formatCompact(used);
  return `${formatCompact(used ?? 0)}/${formatCompact(limit)}`;
}

function formatCompact(value: number | null): string {
  if (value == null) return "n/d";
  return new Intl.NumberFormat("it-IT", { maximumFractionDigits: 0 }).format(value);
}

function formatReset(timestamp: string | null, fallback: string | null): string {
  if (timestamp) return formatDateTime(timestamp);
  return fallback ?? "n/d";
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("it-IT", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
