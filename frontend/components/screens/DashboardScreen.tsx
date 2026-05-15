"use client";

import React from "react";
import {
  Icon, MarketBadge, Spark, fmt, Seg
} from "@/components/atoms";
import { Health, Signal, Position, Strategy, DecisionLog, AppSettings, MarketIntelligence } from "@/lib/api";

/* ── helpers ──────────────────────────────────────────────────────────────── */
function asNumber(v: unknown): number | null {
  if (v == null) return null;
  const n = typeof v === "number" ? v : Number(v);
  return isFinite(n) ? n : null;
}

function timeValue(v: string | null | undefined): number {
  if (!v) return 0;
  const time = new Date(v).getTime();
  return Number.isFinite(time) ? time : 0;
}

function fmtSize(value: string | number | null | undefined): string {
  const n = asNumber(value);
  if (n == null) return "—";
  const abs = Math.abs(n);
  const maxDigits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
  return n.toLocaleString("en-US", { maximumFractionDigits: maxDigits });
}

function displaySize(position: Position): string | number | null | undefined {
  if (position.initial_size != null) return position.initial_size;
  const initialSize = position.metadata_json?.initial_size;
  return typeof initialSize === "string" || typeof initialSize === "number"
    ? initialSize
    : position.size;
}

function metadataLabel(value: unknown): string | null {
  return typeof value === "string" || typeof value === "number" ? String(value) : null;
}

function takeProfitSummary(position: Position): string {
  const raw = position.metadata_json?.take_profit_plan;
  if (!Array.isArray(raw)) return fmt.price(position.take_profit);
  const pending = raw
    .filter((item): item is Record<string, unknown> => item != null && typeof item === "object" && !Boolean(item.filled))
    .map((item) => {
      const level = String(item.level ?? "TP").toUpperCase();
      const price = fmt.price(metadataLabel(item.price));
      return `${level} ${price}`;
    });
  return pending.length > 0 ? pending.slice(0, 2).join(" · ") : "completato";
}

function currentPrice(position: Position): number | null {
  const markPrice = asNumber(position.metadata_json?.last_mark_price);
  if (markPrice !== null) return markPrice;
  const entry = asNumber(position.entry_price);
  const size = asNumber(position.size);
  const pnl = asNumber(position.unrealized_pnl);
  if (entry === null || size === null || size === 0 || pnl === null) return null;
  return position.side.toLowerCase() === "short"
    ? entry - pnl / size
    : entry + pnl / size;
}

function fmtLeverage(value: string | number | null | undefined): string {
  const n = asNumber(value);
  if (n == null) return "—";
  return `${n.toLocaleString("en-US", { maximumFractionDigits: 2 })}x`;
}

function displayLeverage(position: Position): string {
  const contract = asNumber(position.metadata_json?.contract_leverage);
  if (contract !== null) return fmtLeverage(contract);
  return fmtLeverage(position.leverage);
}

function openPositionSearchText(position: Position): string {
  const sourceSignalId = metadataLabel(position.metadata_json?.source_signal_id);
  const sourceTimeframe = metadataLabel(position.metadata_json?.source_timeframe);
  const markPrice = currentPrice(position);
  return [
    position.market?.symbol, position.market?.base, position.market?.quote,
    position.market?.market_type, position.side, position.mode,
    position.entry_price, markPrice, displaySize(position),
    position.leverage, position.stop_loss, position.take_profit,
    position.unrealized_pnl, position.realized_pnl,
    sourceSignalId ? `#${sourceSignalId}` : null, sourceSignalId, sourceTimeframe,
    takeProfitSummary(position),
  ]
    .filter((value): value is string | number => typeof value === "string" || typeof value === "number")
    .join(" ")
    .toLowerCase();
}

type PositionMode = "all" | "live" | "paper";
const OPEN_POSITIONS_PAGE_SIZE = 10;

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
  onOpenPosition: (position: Position) => void;
}

/* ─────────────────────────────────────────────────────────────────────────── */
export function DashboardScreen({
  health, signals, positions, logs, settings, marketIntelligence, threshold, onRefresh, onOpenPosition
}: Props) {
  const [positionMode, setPositionMode] = React.useState<PositionMode>("all");
  const [openPositionPage, setOpenPositionPage] = React.useState(1);
  const [openPositionSearch, setOpenPositionSearch] = React.useState("");

  const openPositions = positions
    .filter(p => p.status === "open")
    .sort((a, b) => timeValue(b.opened_at) - timeValue(a.opened_at));
  const liveOpenPositions = openPositions.filter(p => p.mode === "live");
  const paperOpenPositions = openPositions.filter(p => p.mode === "paper");
  const closedPositions = positions.filter(p => p.status !== "open");

  const activeSignals = signals.filter(s => s.status === "new" || s.status === "active");
  const strongSignals = activeSignals.filter(s => s.score >= threshold);

  const tabPositions = positionMode === "live"
    ? liveOpenPositions
    : positionMode === "paper"
      ? paperOpenPositions
      : openPositions;

  const openPositionQuery = openPositionSearch.trim().toLowerCase();
  const openPositionSearchTokens = openPositionQuery.split(/\s+/).filter(Boolean);
  const filteredOpenPositions = openPositionSearchTokens.length === 0
    ? tabPositions
    : tabPositions.filter(position => {
        const text = openPositionSearchText(position);
        return openPositionSearchTokens.every(token => text.includes(token));
      });

  const openPositionPages = Math.max(1, Math.ceil(filteredOpenPositions.length / OPEN_POSITIONS_PAGE_SIZE));
  const safeOpenPositionPage = Math.min(openPositionPage, openPositionPages);
  const pagedOpenPositions = filteredOpenPositions.slice(
    (safeOpenPositionPage - 1) * OPEN_POSITIONS_PAGE_SIZE,
    safeOpenPositionPage * OPEN_POSITIONS_PAGE_SIZE
  );

  React.useEffect(() => { setOpenPositionPage(1); }, [openPositionQuery, positionMode]);
  React.useEffect(() => {
    if (openPositionPage > openPositionPages) setOpenPositionPage(openPositionPages);
  }, [openPositionPage, openPositionPages]);

  const liveClosedPositions = closedPositions.filter(p => p.mode === "live");
  const paperClosedPositions = closedPositions.filter(p => p.mode === "paper");

  const livePnls = liveClosedPositions.map(p => asNumber(p.realized_pnl)).filter((v): v is number => v !== null);
  const paperPnls = paperClosedPositions.map(p => asNumber(p.realized_pnl)).filter((v): v is number => v !== null);
  const liveTotalPnl = livePnls.reduce((a, b) => a + b, 0);
  const paperTotalPnl = paperPnls.reduce((a, b) => a + b, 0);

  const riskSettings = settings.risk ?? {};
  const maxPositions = asNumber(riskSettings.max_open_positions) ?? 0;
  const killEnabled = Boolean(settings.kill_switch?.enabled);

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

      {killEnabled && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", marginBottom: "var(--gap)", background: "var(--short-soft)", border: "1px solid var(--short-border)", borderRadius: "var(--radius)", color: "var(--short)", fontSize: "var(--fz-sm)" }}>
          <Icon name="power" size={14} />
          <strong>Kill switch attivo</strong> — tutti i nuovi segnali sono bloccati.
        </div>
      )}

      {/* ── KPI row ── */}
      <div className="stat-grid" style={{ marginBottom: "var(--gap)" }}>
        <div className="stat">
          <div className="stat-label">Posizioni live</div>
          <div className="stat-value">
            {liveOpenPositions.length}
            {maxPositions > 0 && (
              <span style={{ fontSize: "var(--fz-md)", color: "var(--fg-3)", fontWeight: 400 }}>/{maxPositions}</span>
            )}
          </div>
          <div className="stat-delta">
            {liveOpenPositions.length === 0 ? "nessuna aperta" : `${liveClosedPositions.length} chiuse`}
          </div>
        </div>

        <div className="stat">
          <div className="stat-label">Posizioni paper</div>
          <div className="stat-value" style={{ color: "var(--fg-2)" }}>{paperOpenPositions.length}</div>
          <div className="stat-delta">{paperClosedPositions.length} chiuse</div>
        </div>

        <div className="stat">
          <div className="stat-label">P&amp;L live realizzato</div>
          <div className={"stat-value " + (liveTotalPnl >= 0 ? "up" : "down")}>{fmt.pnl(liveTotalPnl)}</div>
          <div className="stat-delta">
            {livePnls.filter(v => v > 0).length}W · {livePnls.filter(v => v < 0).length}L
            {paperPnls.length > 0 && (
              <span style={{ marginLeft: 6, color: "var(--fg-3)" }}>· paper {fmt.pnl(paperTotalPnl)}</span>
            )}
          </div>
        </div>

        <div className="stat">
          <div className="stat-label">Segnali forti</div>
          <div className="stat-value" style={{ color: strongSignals.length > 0 ? "var(--long)" : undefined }}>
            {strongSignals.length}
          </div>
          <div className="stat-delta">score ≥ {threshold} · {activeSignals.length} attivi</div>
        </div>
      </div>

      {/* ── Global market metrics ── */}
      <GlobalMetricsRow globalMetrics={marketIntelligence.global_metrics} />

      {/* ── Market intelligence ── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <FearGreedCard fearGreed={marketIntelligence.fear_greed} />
        <AltcoinSeasonCard altcoinSeason={marketIntelligence.altcoin_season} />
        <ConnectionCard health={health} />
        <ActivityCard logs={logs} />
      </div>

      {/* ── Open positions ── */}
      <div style={{ marginBottom: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Posizioni aperte</span>
            <Seg
              value={positionMode}
              onChange={v => setPositionMode(v as PositionMode)}
              options={[
                ["all", `Tutte (${openPositions.length})`],
                ["live", `Live (${liveOpenPositions.length})`],
                ["paper", `Paper (${paperOpenPositions.length})`],
              ]}
            />
            <div className="right">
              <div style={{ position: "relative", display: "inline-flex", alignItems: "center" }}>
                <span style={{ position: "absolute", left: 9, color: "var(--fg-3)", pointerEvents: "none" }}>
                  <Icon name="search" size={12} />
                </span>
                <input
                  type="text"
                  value={openPositionSearch}
                  onChange={e => setOpenPositionSearch(e.target.value)}
                  placeholder="Cerca..."
                  className="filter"
                  style={{ width: 180, paddingLeft: 28 }}
                />
              </div>
              {openPositionQuery && (
                <span className="badge muted">{filteredOpenPositions.length}/{tabPositions.length}</span>
              )}
            </div>
          </div>

          <div style={{ overflowX: "auto" }}>
            <table className="tbl" style={{ minWidth: 1080 }}>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th style={{ textAlign: "right" }}>Entry</th>
                  <th style={{ textAlign: "right" }}>Current</th>
                  <th style={{ textAlign: "right" }}>Size</th>
                  <th style={{ textAlign: "right" }}>Lev</th>
                  <th style={{ textAlign: "right" }}>P&amp;L</th>
                  <th style={{ textAlign: "right" }}>P&amp;L%</th>
                  <th style={{ textAlign: "right" }}>Stop</th>
                  <th>Target</th>
                  <th>Mode</th>
                  <th>Età</th>
                </tr>
              </thead>
              <tbody>
                {openPositions.length === 0 && (
                  <tr>
                    <td colSpan={12} style={{ textAlign: "center", padding: "24px 0", color: "var(--fg-3)" }}>
                      Nessuna posizione aperta
                    </td>
                  </tr>
                )}
                {openPositions.length > 0 && filteredOpenPositions.length === 0 && (
                  <tr>
                    <td colSpan={12} style={{ textAlign: "center", padding: "24px 0", color: "var(--fg-3)" }}>
                      Nessuna posizione trovata
                    </td>
                  </tr>
                )}
                {pagedOpenPositions.map(p => {
                  const pnl = asNumber(p.total_pnl ?? p.unrealized_pnl ?? p.realized_pnl);
                  const markPrice = currentPrice(p);
                  const entryPrice = asNumber(p.entry_price);
                  const size = asNumber(displaySize(p));
                  const notional = asNumber(p.notional) ?? (entryPrice !== null && size !== null ? entryPrice * size : null);
                  const pnlPct = asNumber(p.return_pct) ?? (pnl !== null && notional !== null && notional > 0 ? (pnl / notional) * 100 : null);
                  const side = p.side.toUpperCase();
                  const isLive = p.mode === "live";
                  return (
                    <tr
                      key={p.id}
                      onClick={() => onOpenPosition(p)}
                      style={{ cursor: p.market ? "pointer" : "default" }}
                      title={p.market ? `Apri dettaglio ${p.market.symbol}` : undefined}
                    >
                      <td className="fg">
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          {isLive && (
                            <span style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--long)", display: "inline-block", flexShrink: 0 }} title="live" />
                          )}
                          <strong>{p.market?.symbol ?? "—"}</strong>
                          <MarketBadge market={p.market?.market_type ?? "perp"} />
                        </div>
                      </td>
                      <td>
                        <span className={"dir mono " + (side === "LONG" ? "long" : "short")}>
                          {side === "LONG" ? "↑" : "↓"} {side}
                        </span>
                      </td>
                      <td className="num fg">{fmt.price(p.entry_price)}</td>
                      <td className="num fg">{fmt.price(markPrice)}</td>
                      <td className="num fg">{fmtSize(displaySize(p))}</td>
                      <td className="num fg">{displayLeverage(p)}</td>
                      <td className="num" style={{ color: pnl === null ? undefined : pnl >= 0 ? "var(--long)" : "var(--short)" }}>
                        {pnl !== null ? fmt.pnl(pnl) : "—"}
                      </td>
                      <td className="num" style={{ color: pnlPct === null ? undefined : pnlPct >= 0 ? "var(--long)" : "var(--short)" }}>
                        {pnlPct !== null ? `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%` : "—"}
                      </td>
                      <td className="num">{fmt.price(p.stop_loss)}</td>
                      <td className="muted mono" style={{ fontSize: "var(--fz-xs)", whiteSpace: "nowrap" }}>
                        {takeProfitSummary(p)}
                      </td>
                      <td><span className={"badge " + (isLive ? "long" : "muted")}>{p.mode}</span></td>
                      <td className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>{fmt.relTime(p.opened_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {filteredOpenPositions.length > OPEN_POSITIONS_PAGE_SIZE && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, padding: "10px var(--pad-x)", borderTop: "1px solid var(--border)" }}>
              <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>
                {(safeOpenPositionPage - 1) * OPEN_POSITIONS_PAGE_SIZE + 1}–{Math.min(filteredOpenPositions.length, safeOpenPositionPage * OPEN_POSITIONS_PAGE_SIZE)} di {filteredOpenPositions.length}
              </span>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <button className="btn ghost sm" onClick={() => setOpenPositionPage(p => Math.max(1, p - 1))} disabled={safeOpenPositionPage <= 1} title="Precedente">
                  <Icon name="chevron_left" size={11} />
                </button>
                <span className="muted mono" style={{ minWidth: 44, textAlign: "center", fontSize: "var(--fz-xs)" }}>
                  {safeOpenPositionPage}/{openPositionPages}
                </span>
                <button className="btn ghost sm" onClick={() => setOpenPositionPage(p => Math.min(openPositionPages, p + 1))} disabled={safeOpenPositionPage >= openPositionPages} title="Successiva">
                  <Icon name="chevron" size={11} />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ── Performance ── */}
      <PerformanceCard positions={closedPositions} />
    </div>
  );
}

/* ── Global market metrics row ───────────────────────────────────────────── */
function fmtLargeUsd(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v >= 1e12) return `$${(v / 1e12).toFixed(2)}T`;
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

function fmtBtcPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

function GlobalMetricsRow({ globalMetrics }: { globalMetrics: MarketIntelligence["global_metrics"] }) {
  if (!globalMetrics) return null;

  const { btc_price_usd, total_market_cap_usd, total_market_cap_ex_top10_usd, total_volume_24h_usd, volume_trend, btc_dominance_pct, market_cap_change_24h_pct } = globalMetrics;

  const mcChange = market_cap_change_24h_pct;
  const mcChangeColor = mcChange == null ? "var(--fg-3)" : mcChange >= 0 ? "var(--long)" : "var(--short)";
  const mcChangeText = mcChange != null ? `${mcChange >= 0 ? "+" : ""}${mcChange.toFixed(2)}%` : null;

  return (
    <div className="card" style={{ marginBottom: "var(--gap)", padding: "10px var(--pad-x)" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 0 }}>
        <GlobalMetricItem
          label="BTC Price"
          value={fmtBtcPrice(btc_price_usd)}
          sub={btc_dominance_pct != null ? `${btc_dominance_pct.toFixed(1)}% dom.` : undefined}
        />
        <GlobalMetricItem
          label="Total Market Cap"
          value={fmtLargeUsd(total_market_cap_usd)}
          sub={mcChangeText ?? undefined}
          subColor={mcChangeColor}
        />
        <GlobalMetricItem
          label="Market Cap ex-Top 10"
          value={fmtLargeUsd(total_market_cap_ex_top10_usd)}
        />
        <GlobalMetricItem
          label="Volume 24h"
          value={fmtLargeUsd(total_volume_24h_usd)}
          sub={volume_trend === "up" ? "↑ in aumento" : volume_trend === "down" ? "↓ in calo" : undefined}
          subColor={volume_trend === "up" ? "var(--long)" : volume_trend === "down" ? "var(--short)" : undefined}
        />
        <GlobalMetricItem
          label="Fonte"
          value={globalMetrics.source}
          mono={false}
          muted
        />
      </div>
    </div>
  );
}

function GlobalMetricItem({ label, value, sub, subColor, mono = true, muted = false }: {
  label: string;
  value: string;
  sub?: string;
  subColor?: string;
  mono?: boolean;
  muted?: boolean;
}) {
  return (
    <div style={{ padding: "6px 12px", borderRight: "1px solid var(--border)" }}>
      <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--fg-3)", marginBottom: 3 }}>{label}</div>
      <div style={{ fontSize: "var(--fz-md)", fontWeight: 600, fontFamily: mono ? "var(--font-mono)" : undefined, color: muted ? "var(--fg-3)" : "var(--fg)" }}>{value}</div>
      {sub && (
        <div style={{ fontSize: "var(--fz-xs)", color: subColor ?? "var(--fg-3)", marginTop: 2 }}>{sub}</div>
      )}
    </div>
  );
}

/* ── Fear & Greed card ────────────────────────────────────────────────────── */
function FearGreedCard({ fearGreed }: { fearGreed: MarketIntelligence["fear_greed"] }) {
  const value = fearGreed?.value ?? null;
  const label = fearGreed?.classification ?? null;

  /* Semi-circle geometry: leftmost=fear(0), top=neutral(50), rightmost=greed(100) */
  const cx = 80, cy = 70, r = 56, nl = 44, sw = 7;

  const zones = [
    { v1: 0,  v2: 25,  color: "#EF4444" },
    { v1: 25, v2: 45,  color: "#F97316" },
    { v1: 45, v2: 55,  color: "#EAB308" },
    { v1: 55, v2: 75,  color: "#84CC16" },
    { v1: 75, v2: 100, color: "#22C55E" },
  ];

  function arcPath(v1: number, v2: number) {
    const a1 = (1 - v1 / 100) * Math.PI;
    const a2 = (1 - v2 / 100) * Math.PI;
    const x1 = (cx + r * Math.cos(a1)).toFixed(2);
    const y1 = (cy - r * Math.sin(a1)).toFixed(2);
    const x2 = (cx + r * Math.cos(a2)).toFixed(2);
    const y2 = (cy - r * Math.sin(a2)).toFixed(2);
    /* sweep-flag=1 → CW in SVG = correct upper-semicircle left→top→right */
    return `M ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2}`;
  }

  const activeZone = value !== null
    ? (zones.find(z => value >= z.v1 && value < z.v2) ?? zones[zones.length - 1])
    : null;
  const needleColor = activeZone?.color ?? "var(--fg-3)";

  const θ = value !== null ? (1 - value / 100) * Math.PI : Math.PI / 2;
  const nx = (cx + nl * Math.cos(θ)).toFixed(2);
  const ny = (cy - nl * Math.sin(θ)).toFixed(2);

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Fear & Greed</span>
        {label && <span className="badge muted">{label.toUpperCase()}</span>}
        <div className="right">
          <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>{fearGreed?.source ?? ""}</span>
        </div>
      </div>
      <div className="card-body" style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
        {fearGreed === null ? (
          <div style={{ padding: "20px 0", textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
            <div style={{ fontSize: 22, marginBottom: 6 }}>—</div>
            <div>API non configurata</div>
          </div>
        ) : (
          <>
            <svg width={160} height={88} viewBox="0 0 160 88" style={{ overflow: "visible" }}>
              {zones.map((z, i) => (
                <path key={i} d={arcPath(z.v1, z.v2)} fill="none"
                  stroke={z.color} strokeWidth={sw} strokeLinecap="butt" opacity={0.22} />
              ))}
              {activeZone && (
                <path d={arcPath(activeZone.v1, activeZone.v2)} fill="none"
                  stroke={activeZone.color} strokeWidth={sw} strokeLinecap="butt" opacity={0.9} />
              )}
              <line x1={cx} y1={cy} x2={nx} y2={ny}
                stroke={needleColor} strokeWidth={2} strokeLinecap="round" />
              <circle cx={cx} cy={cy} r={3.5} fill={needleColor} />
              <text x={cx} y={cy - 10} textAnchor="middle"
                style={{ fontSize: "24px", fontWeight: 700, fill: "var(--fg)", fontFamily: "var(--font-mono)" }}>
                {value ?? "—"}
              </text>
              <text x={14} y={82} textAnchor="middle"
                style={{ fontSize: "7px", fill: "#EF4444", fontFamily: "var(--font-sans)", opacity: 0.8 }}>Fear</text>
              <text x={146} y={82} textAnchor="middle"
                style={{ fontSize: "7px", fill: "#22C55E", fontFamily: "var(--font-sans)", opacity: 0.8 }}>Greed</text>
            </svg>
            {fearGreed.timestamp && (
              <div className="muted" style={{ fontSize: "var(--fz-xs)", marginTop: 4 }}>
                aggiornato {fmt.relTime(fearGreed.timestamp)}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/* ── Altcoin Season card ──────────────────────────────────────────────────── */
function AltcoinSeasonCard({ altcoinSeason }: { altcoinSeason: MarketIntelligence["altcoin_season"] }) {
  const altIndex = altcoinSeason?.altcoin_index ?? null;
  const label = altIndex === null
    ? null
    : altIndex >= 75 ? "Alt season" : altIndex <= 25 ? "BTC season" : "Neutral";
  const barColor = altIndex === null
    ? "var(--accent)"
    : altIndex >= 75 ? "var(--long)" : altIndex <= 25 ? "var(--accent)" : "var(--warn)";

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Altcoin Season</span>
        {label && <span className="badge muted">{label}</span>}
      </div>
      <div className="card-body">
        {altcoinSeason === null ? (
          <div style={{ padding: "20px 0", textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
            <div style={{ fontSize: 22, marginBottom: 6 }}>—</div>
            <div>API non configurata</div>
          </div>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "baseline", gap: 4, marginBottom: 12 }}>
              <span style={{ fontSize: "var(--fz-xl)", fontWeight: 700, fontFamily: "var(--font-mono)", color: "var(--fg)" }}>
                {altIndex ?? "—"}
              </span>
              <span style={{ color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>/100</span>
            </div>
            <div className="bar" style={{ marginBottom: 8 }}>
              <i style={{ width: `${Math.max(0, Math.min(100, altIndex ?? 0))}%`, background: barColor }} />
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "var(--fz-xs)", color: "var(--fg-3)" }}>
              <span>BTC season</span>
              <span>Alt season</span>
            </div>
          </>
        )}
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
        <span className="title">Hyperliquid</span>
        <div className="right"><span className={"dot " + (isOk ? "green" : "red")} /></div>
      </div>
      <div className="card-body" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, fontSize: "var(--fz-sm)" }}>
        <div>
          <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>Stato</div>
          <div className={isOk ? "up mono" : "down mono"}>{health.status}</div>
        </div>
        <div>
          <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>Mode</div>
          <div className="mono">{health.mode}</div>
        </div>
        <div>
          <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>Network</div>
          <div className="mono">{health.network}</div>
        </div>
        <div>
          <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>Trading</div>
          <div className={health.live_trading_allowed ? "up mono" : "muted mono"}>
            {health.live_trading_allowed ? "live OK" : "disabled"}
          </div>
        </div>
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
        {logs.length === 0 ? (
          <div style={{ padding: "24px", textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
            Nessun evento
          </div>
        ) : (
          logs.slice(0, 5).map((log, i) => (
            <div key={log.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px var(--pad-x)", borderBottom: i < 4 ? "1px solid var(--border)" : "none", fontSize: "var(--fz-sm)" }}>
              <span className="muted mono" style={{ fontSize: 10, width: 32, flexShrink: 0 }}>{fmt.relTime(log.timestamp)}</span>
              <span style={{ color: "var(--fg-3)", flexShrink: 0 }}><Icon name={iconFor(log.event_type)} size={12} /></span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{log.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

/* ── Performance card ─────────────────────────────────────────────────────── */
function perfStats(positions: Position[]) {
  const sorted = [...positions]
    .filter(p => p.realized_pnl != null)
    .sort((a, b) => new Date(a.closed_at ?? a.opened_at).getTime() - new Date(b.closed_at ?? b.opened_at).getTime());
  const pnls = sorted.map(p => asNumber(p.realized_pnl)).filter((v): v is number => v !== null);
  const wins = pnls.filter(v => v > 0).length;
  const losses = pnls.filter(v => v < 0).length;
  const total = pnls.reduce((a, b) => a + b, 0);
  const wr = pnls.length > 0 ? (wins / pnls.length) * 100 : 0;
  const best = pnls.length > 0 ? Math.max(...pnls) : null;
  const worst = pnls.length > 0 ? Math.min(...pnls) : null;
  const cumPnl: number[] = [];
  let cum = 0;
  for (const v of pnls) { cum += v; cumPnl.push(cum); }
  const holdTimes = sorted
    .map(p => p.closed_at ? new Date(p.closed_at).getTime() - new Date(p.opened_at).getTime() : null)
    .filter((v): v is number => v !== null && isFinite(v));
  const avgHoldH = holdTimes.length > 0
    ? holdTimes.reduce((a, b) => a + b, 0) / holdTimes.length / 3_600_000
    : null;
  return { pnls, wins, losses, total, wr, best, worst, cumPnl, avgHoldH };
}

function PerformanceCard({ positions }: { positions: Position[] }) {
  const [view, setView] = React.useState<"live" | "paper">("live");

  const livePos = positions.filter(p => p.mode === "live");
  const paperPos = positions.filter(p => p.mode === "paper");
  const live = perfStats(livePos);
  const paper = perfStats(paperPos);
  const active = view === "live" ? live : paper;

  const hasLive = live.pnls.length > 0;
  const hasPaper = paper.pnls.length > 0;
  const hasAny = hasLive || hasPaper;

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Performance storica</span>
        <Seg
          value={view}
          onChange={v => setView(v as "live" | "paper")}
          options={[
            ["live", `Live (${live.pnls.length})`],
            ["paper", `Paper (${paper.pnls.length})`],
          ]}
        />
        {active.pnls.length > 0 && (
          <div className="right">
            <span className={"badge " + (active.total >= 0 ? "long" : "short")}>
              {fmt.pnl(active.total)} totale
            </span>
          </div>
        )}
      </div>
      {!hasAny ? (
        <div style={{ padding: "32px 0", textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
          Nessun trade chiuso
        </div>
      ) : active.pnls.length === 0 ? (
        <div style={{ padding: "32px 0", textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
          Nessun trade {view} chiuso
        </div>
      ) : (
        <div className="card-body">
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16, marginBottom: 20 }}>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>Win rate</div>
              <div className="mono" style={{ fontSize: "var(--fz-xl)", color: active.wr >= 50 ? "var(--long)" : "var(--short)" }}>
                {active.wr.toFixed(0)}<span className="muted" style={{ fontSize: "var(--fz-sm)" }}>%</span>
              </div>
              <div className="muted" style={{ fontSize: "var(--fz-xs)", marginTop: 2 }}>{active.wins}W · {active.losses}L</div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>P&amp;L {view}</div>
              <div className="mono" style={{ fontSize: "var(--fz-xl)", color: active.total >= 0 ? "var(--long)" : "var(--short)" }}>
                {fmt.pnl(active.total)}
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>Best trade</div>
              <div className="mono up" style={{ fontSize: "var(--fz-lg)" }}>{active.best !== null ? fmt.pnl(active.best) : "—"}</div>
              <div className="muted" style={{ fontSize: "var(--fz-xs)", marginTop: 2 }}>
                worst {active.worst !== null ? fmt.pnl(active.worst) : "—"}
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>Durata media</div>
              <div className="mono" style={{ fontSize: "var(--fz-xl)" }}>
                {active.avgHoldH !== null ? `${active.avgHoldH.toFixed(1)}h` : "—"}
              </div>
            </div>
          </div>
          {active.cumPnl.length >= 2 && (
            <div>
              <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>Equity curve {view}</div>
              <Spark data={active.cumPnl} color={active.total >= 0 ? "var(--long)" : "var(--short)"} w={400} h={56} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
