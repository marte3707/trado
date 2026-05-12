"use client";

import React from "react";
import {
  Icon, ScoreChip, DirBadge, TfChip, MarketBadge, StratList, Spark, fmt, StatusBadge
} from "@/components/atoms";
import { Health, Signal, Position, Strategy, DecisionLog, AppSettings, MarketIntelligence } from "@/lib/api";

/* ── helpers ──────────────────────────────────────────────────────────────── */
function asNumber(v: unknown): number | null {
  if (v == null) return null;
  const n = typeof v === "number" ? v : Number(v);
  return isFinite(n) ? n : null;
}


interface Props {
  health: Health;
  signals: Signal[];
  positions: Position[];
  strategies: Strategy[];
  logs: DecisionLog[];
  settings: AppSettings;
  marketIntelligence: MarketIntelligence;
  threshold: number;
  onRefresh: () => void;
  onOpenSignal: (signal: Signal) => void;
  onOpenPosition: (position: Position) => void;
}

/* ─────────────────────────────────────────────────────────────────────────── */
export function DashboardScreen({ health, signals, positions, strategies, logs, settings, marketIntelligence, threshold, onRefresh, onOpenSignal, onOpenPosition }: Props) {
  const openPositions = positions.filter(p => p.status === "open");
  const liveOpenPositions = openPositions.filter(p => p.mode === "live");
  const paperOpenPositions = openPositions.filter(p => p.mode === "paper");
  const closedPositions = positions.filter(p => p.status !== "open");
  const activeSignals = signals.filter(s => s.status === "new" || s.status === "active");
  const strongSignals = activeSignals.filter(s => s.score >= threshold).slice(0, 4);
  const latestSignals = [
    ...activeSignals.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
    ...[...signals]
      .filter(s => s.status !== "new" && s.status !== "active")
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
  ].slice(0, 10);

  const pnls = closedPositions.map(p => asNumber(p.realized_pnl)).filter((v): v is number => v !== null);
  const totalPnl = pnls.reduce((a, b) => a + b, 0);

  const riskSettings = settings.risk ?? {};
  const maxPositions = asNumber(riskSettings.max_open_positions) ?? 0;
  const maxExposurePct = asNumber(riskSettings.max_total_exposure_pct) ?? 0;
  const liveExposurePct = (liveOpenPositions.length / Math.max(maxPositions, 1)) * maxExposurePct;
  const liveExposureUsagePct = maxExposurePct > 0 ? (liveExposurePct / maxExposurePct) * 100 : 0;
  const killEnabled = Boolean(settings.kill_switch?.enabled);
  const altcoinSeason = marketIntelligence.altcoin_season;
  const altIndex = altcoinSeason?.altcoin_index ?? null;
  const altSeasonLabel = altIndex === null
    ? "n/d"
    : altIndex >= 75
      ? "Alt season"
      : altIndex <= 25
        ? "BTC season"
        : "Neutral";

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <div className="page-sub">
            {health.mode} · {health.network} · {killEnabled ? "⛔ kill switch ON" : "live trading allowed"}
          </div>
        </div>
        <div className="page-actions">
          <button className="btn ghost" onClick={onRefresh}>
            <Icon name="refresh" size={12} /> Aggiorna
          </button>
        </div>
      </div>

      {/* Kill switch banner */}
      {killEnabled && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", marginBottom: "var(--gap)", background: "var(--short-soft)", border: "1px solid var(--short-border)", borderRadius: "var(--radius)", color: "var(--short)", fontSize: "var(--fz-sm)" }}>
          <Icon name="power" size={14} />
          <strong>Kill switch attivo</strong> — tutti i nuovi segnali sono bloccati.
        </div>
      )}

      {/* Stat row */}
      <div className="stat-grid" style={{ marginBottom: "var(--gap)" }}>
        <div className="stat">
          <div className="stat-label">Segnali forti</div>
          <div className="stat-value" style={{ color: "var(--long)" }}>{strongSignals.length}</div>
          <div className="stat-delta">score ≥ {threshold} · {activeSignals.length} totali</div>
        </div>
        <div className="stat">
          <div className="stat-label">Posizioni aperte</div>
          <div className="stat-value">{openPositions.length}</div>
          <div className="stat-delta">{paperOpenPositions.length} paper · {liveOpenPositions.length} live · {closedPositions.length} chiuse</div>
        </div>
        <div className="stat">
          <div className="stat-label">P&amp;L realizzato</div>
          <div className={"stat-value " + (totalPnl >= 0 ? "up" : "down")}>{fmt.pnl(totalPnl)}</div>
          <div className="stat-delta">{pnls.filter(v => v > 0).length}W · {pnls.filter(v => v < 0).length}L</div>
        </div>
        <div className="stat">
          <div className="stat-label">Esposizione live</div>
          <div className="stat-value">{liveOpenPositions.length > 0 ? (liveExposurePct).toFixed(0) : "0"}<span style={{ color: "var(--fg-3)" }}>%</span></div>
          <div style={{ marginTop: 8, width: 120 }}>
            <div className="bar"><i style={{ width: `${Math.min(100, liveExposureUsagePct)}%`, background: liveExposureUsagePct > 75 ? "var(--short)" : liveExposureUsagePct > 50 ? "var(--warn)" : "var(--accent)" }} /></div>
            <div className="stat-delta" style={{ marginTop: 4 }}>solo live · limite {maxExposurePct}%</div>
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Altcoin season</div>
          <div className="stat-value">{altIndex ?? "—"}<span style={{ color: "var(--fg-3)" }}>/100</span></div>
          <div style={{ marginTop: 8, width: 120 }}>
            <div className="bar"><i style={{ width: `${Math.max(0, Math.min(100, altIndex ?? 0))}%`, background: altIndex !== null && altIndex >= 75 ? "var(--long)" : altIndex !== null && altIndex <= 25 ? "var(--accent)" : "var(--warn)" }} /></div>
            <div className="stat-delta" style={{ marginTop: 4 }}>{altSeasonLabel}</div>
          </div>
        </div>
      </div>

      {/* Strong signals + risk */}
      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        {strongSignals.length > 0 && (
          <div className="card">
            <div className="card-head">
              <span className="title">Segnali forti</span>
              <span className="badge long">{strongSignals.length} attivi · score ≥ {threshold}</span>
              <div className="right">
                <button className="btn ghost sm">Vedi tutti <Icon name="chevron" size={11} /></button>
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 1, background: "var(--border)" }}>
              {strongSignals.map(s => <MiniSignalCard key={s.id} sig={s} />)}
            </div>
          </div>
        )}
        {strongSignals.length === 0 && (
          <div className="card" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: 120 }}>
            <div style={{ textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
              <Icon name="signal" size={20} />
              <div style={{ marginTop: 8 }}>Nessun segnale forte</div>
              <div style={{ fontSize: 10, marginTop: 4 }}>score ≥ {threshold}</div>
            </div>
          </div>
        )}

        <div className="card">
          <div className="card-head">
            <span className="title">Safety live</span>
            <div className="right"><span className="badge muted">{liveOpenPositions.length}/{maxPositions}</span></div>
          </div>
          <div style={{ padding: 0 }}>
            <RiskRow label="Posizioni live" current={liveOpenPositions.length} max={maxPositions} />
            <RiskRow label="Esposizione live" current={liveExposurePct} max={maxExposurePct} unit="%" />
            <RiskRow label="Kill switch" current={killEnabled ? 1 : 0} max={1} tone={killEnabled ? "danger" : "ok"} label2={killEnabled ? "ON" : "OFF"} />
            <RiskRow label="Strategie attive" current={strategies.filter(s => s.enabled).length} max={strategies.length} />
          </div>
        </div>
      </div>

      {/* Signals + positions */}
      <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Ultimi segnali</span>
            <span className="badge muted">{signals.length} totali</span>
            <div className="right">
              <span className="dot green" />
              <span className="mono" style={{ fontSize: "var(--fz-xs)", color: "var(--fg-3)" }}>LIVE</span>
            </div>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th>Symbol</th><th>TF</th><th>Dir</th><th>Score</th><th style={{ textAlign: "right" }}>Entry</th><th>Strats</th><th>Età</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {latestSignals.length === 0 && (
                <tr><td colSpan={8} style={{ textAlign: "center", padding: "24px 0", color: "var(--fg-3)" }}>Nessun segnale</td></tr>
              )}
              {latestSignals.map(s => (
                <tr key={s.id} onClick={() => onOpenSignal(s)} style={{ cursor: s.market ? "pointer" : "default" }} title={s.market ? `Apri dettaglio ${s.market.symbol}` : undefined}>
                  <td className="fg" style={{ display: "flex", alignItems: "center", gap: 8, height: "var(--row-h)" }}>
                    <strong>{s.market?.symbol}</strong>
                    <MarketBadge market={s.market?.market_type ?? "perp"} />
                  </td>
                  <td><TfChip tf={s.timeframe} /></td>
                  <td><DirBadge dir={s.direction} /></td>
                  <td><ScoreChip score={s.score} dir={s.direction} /></td>
                  <td className="num fg">{fmt.price(s.entry_price)}</td>
                  <td><StratList strats={Object.keys(s.strategy_breakdown ?? {})} max={3} /></td>
                  <td className="muted mono">{fmt.relTime(s.created_at)}</td>
                  <td><StatusBadge status={s.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="title">Posizioni aperte</span>
            <div className="right"><span className="badge muted">{openPositions.length} attive</span></div>
          </div>
          <table className="tbl">
            <thead>
              <tr><th>Symbol</th><th>Side</th><th style={{ textAlign: "right" }}>P&amp;L</th><th style={{ textAlign: "right" }}>P&amp;L%</th><th>Mode</th></tr>
            </thead>
            <tbody>
              {openPositions.length === 0 && (
                <tr><td colSpan={5} style={{ textAlign: "center", padding: "24px 0", color: "var(--fg-3)" }}>Nessuna posizione</td></tr>
              )}
              {openPositions.slice(0, 6).map(p => {
                const pnl = asNumber(p.unrealized_pnl ?? p.realized_pnl);
                const entryPrice = asNumber(p.entry_price);
                const size = asNumber(p.size);
                const notional = entryPrice !== null && size !== null ? entryPrice * size : null;
                const pnlPct = pnl !== null && notional !== null && notional > 0 ? (pnl / notional) * 100 : null;
                return (
                  <tr
                    key={p.id}
                    onClick={() => onOpenPosition(p)}
                    style={{ cursor: p.market ? "pointer" : "default" }}
                    title={p.market ? `Apri dettaglio ${p.market.symbol}` : undefined}
                  >
                    <td className="fg"><strong>{p.market?.symbol ?? "—"}</strong></td>
                    <td>
                      <span className={"dir mono " + (p.side.toUpperCase() === "LONG" ? "long" : "short")}>
                        {p.side.toUpperCase() === "LONG" ? "↑" : "↓"} {p.side.toUpperCase()}
                      </span>
                    </td>
                    <td className="num" style={{ color: pnl === null ? undefined : pnl >= 0 ? "var(--long)" : "var(--short)" }}>
                      {pnl !== null ? fmt.pnl(pnl) : "—"}
                    </td>
                    <td className="num" style={{ color: pnlPct === null ? undefined : pnlPct >= 0 ? "var(--long)" : "var(--short)" }}>
                      {pnlPct !== null ? `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%` : "—"}
                    </td>
                    <td className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>{p.mode}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Connection + Activity + Performance */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "var(--gap)" }}>
        <ConnectionCard health={health} />
        <ActivityCard logs={logs} />
        <PerformanceCard positions={closedPositions} />
      </div>
    </div>
  );
}


/* ── Mini signal card (2×2 grid in strong signals section) ─────────────────── */
function MiniSignalCard({ sig }: { sig: Signal }) {
  return (
    <div style={{ background: "var(--bg-1)", padding: 14 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <strong style={{ fontSize: "var(--fz-lg)" }}>{sig.market?.symbol}</strong>
        <MarketBadge market={sig.market?.market_type ?? "perp"} />
        <TfChip tf={sig.timeframe} />
        <div style={{ marginLeft: "auto" }}><ScoreChip score={sig.score} dir={sig.direction} /></div>
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 8 }}>
        <DirBadge dir={sig.direction} />
        <span className="mono fg" style={{ fontSize: "var(--fz-md)" }}>@ {fmt.price(sig.entry_price)}</span>
        <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>R/R {sig.risk_reward ?? "—"}</span>
      </div>
      <div className="muted" style={{ fontSize: "var(--fz-sm)", marginBottom: 8, textWrap: "pretty" as React.CSSProperties["textWrap"] }}>
        {sig.reason?.slice(0, 80)}{sig.reason?.length > 80 ? "…" : ""}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <StratList strats={Object.keys(sig.strategy_breakdown ?? {})} max={4} />
      </div>
    </div>
  );
}

/* ── Risk row ──────────────────────────────────────────────────────────────── */
function RiskRow({ label, current, max, unit = "", tone, label2 }: { label: string; current: number; max: number; unit?: string; tone?: string; label2?: string }) {
  const pct = Math.min(100, max > 0 ? (current / max) * 100 : 0);
  const barColor = tone === "danger" ? "var(--short)" : tone === "ok" ? "var(--long)" : pct > 80 ? "var(--short)" : pct > 60 ? "var(--warn)" : "var(--accent)";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px var(--pad-x)", borderBottom: "1px solid var(--border)" }}>
      <span style={{ flex: 1, fontSize: "var(--fz-sm)" }}>{label}</span>
      <span className="mono" style={{ fontSize: "var(--fz-sm)", color: "var(--fg)" }}>
        {label2 ?? `${current}${unit}`} <span className="muted">/ {max}{unit}</span>
      </span>
      <div className="bar" style={{ width: 60 }}>
        <i style={{ width: `${pct}%`, background: barColor }} />
      </div>
    </div>
  );
}

/* ── Connection card ──────────────────────────────────────────────────────── */
function ConnectionCard({ health }: { health: Health }) {
  const isOk = health.status === "ok";
  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Connessione Hyperliquid</span>
        <div className="right"><span className={"dot " + (isOk ? "green" : "red")} /></div>
      </div>
      <div className="card-body" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, fontSize: "var(--fz-sm)" }}>
        <div><div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>Stato</div><div className={isOk ? "up mono" : "down mono"}>{health.status}</div></div>
        <div><div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>Modalità</div><div className="mono">{health.mode}</div></div>
        <div><div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>Network</div><div className="mono">{health.network}</div></div>
        <div><div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>Trading</div><div className={health.live_trading_allowed ? "up mono" : "muted mono"}>{health.live_trading_allowed ? "live allowed" : "disabled"}</div></div>
      </div>
    </div>
  );
}

/* ── Activity card ────────────────────────────────────────────────────────── */
function ActivityCard({ logs }: { logs: DecisionLog[] }) {
  const iconFor = (t: string) => {
    const lower = t.toLowerCase();
    if (lower.includes("signal")) return "signal";
    if (lower.includes("execut") || lower.includes("trade")) return "check";
    if (lower.includes("kill") || lower.includes("error")) return "power";
    if (lower.includes("warn")) return "info";
    return "bolt";
  };
  return (
    <div className="card">
      <div className="card-head"><span className="title">Attività recente</span></div>
      <div style={{ padding: 0 }}>
        {logs.length === 0 && (
          <div style={{ padding: "24px", textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>Nessun evento</div>
        )}
        {logs.slice(0, 5).map((log, i) => (
          <div key={log.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px var(--pad-x)", borderBottom: i < 4 ? "1px solid var(--border)" : "none", fontSize: "var(--fz-sm)" }}>
            <span className="muted mono" style={{ fontSize: 10, width: 32 }}>{fmt.relTime(log.timestamp)}</span>
            <span style={{ color: "var(--fg-3)" }}><Icon name={iconFor(log.event_type)} size={12} /></span>
            <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{log.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── Performance card ─────────────────────────────────────────────────────── */
function PerformanceCard({ positions }: { positions: Position[] }) {
  const sorted = [...positions]
    .filter(p => p.realized_pnl != null)
    .sort((a, b) => new Date(a.closed_at ?? a.opened_at).getTime() - new Date(b.closed_at ?? b.opened_at).getTime());

  const pnls = sorted.map(p => asNumber(p.realized_pnl)).filter((v): v is number => v !== null);
  const wins = pnls.filter(v => v > 0).length;
  const wr = pnls.length > 0 ? (wins / pnls.length) * 100 : 0;
  const avgPnl = pnls.length > 0 ? pnls.reduce((a, b) => a + b, 0) / pnls.length : 0;

  const cumPnl: number[] = [];
  let cum = 0;
  for (const v of pnls) { cum += v; cumPnl.push(cum); }

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Performance</span>
        <div className="right"><span className={"badge " + (avgPnl >= 0 ? "long" : "short")}>{fmt.pnl(avgPnl)} avg</span></div>
      </div>
      <div className="card-body">
        <div style={{ display: "flex", gap: 24, marginBottom: 14 }}>
          <div>
            <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Win rate</div>
            <div className="mono" style={{ fontSize: "var(--fz-xl)" }}>{wr.toFixed(0)}<span className="muted">%</span></div>
          </div>
          <div>
            <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Trades</div>
            <div className="mono" style={{ fontSize: "var(--fz-xl)" }}>{pnls.length}</div>
          </div>
          <div>
            <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Best</div>
            <div className="mono up" style={{ fontSize: "var(--fz-xl)" }}>{pnls.length > 0 ? fmt.pnl(Math.max(...pnls)) : "—"}</div>
          </div>
        </div>
        <Spark data={cumPnl.length >= 2 ? cumPnl : []} color="var(--long)" w={280} h={48} />
      </div>
    </div>
  );
}
