"use client";

import React, { useEffect, useMemo, useState } from "react";

import { DirBadge, Icon, Kv, MarketBadge, ScoreChip, StatusBadge, TfChip, fmt, normalizeDir } from "@/components/atoms";
import { StrategyContributionsChart } from "@/components/signals/StrategyContributionsChart";
import type { Candle, Health, IndicatorSnapshot, Market, Position, Signal } from "@/lib/api";
import { useSignalSnapshot } from "@/lib/hooks/queries";

interface Props {
  markets: Market[];
  signals: Signal[];
  positions: Position[];
  health: Health;
  chartMarkets: Market[];
  initialCandles: Candle[];
  initialMarket: Market | null;
  initialTf: string;
  focusSignalId?: number;
  focusMarketKey?: string;
  focusTf?: string;
}

type StrategyEntry = {
  name: string;
  direction: string;
  confidence: number | null;
  weight: number | null;
  contribution: number | null;
  reason: string;
  indicatorValues: Record<string, unknown>;
};

function marketKey(market: Market) {
  return `${market.market_type}:${market.symbol}`;
}

function asNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("it-IT", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

function formatScalar(value: unknown): string {
  const n = asNumber(value);
  if (n !== null) return n.toLocaleString("it-IT", { maximumFractionDigits: 5 });
  if (typeof value === "string" && value) return value;
  if (typeof value === "boolean") return value ? "true" : "false";
  return "-";
}

function parseStrategyEntries(breakdown: Record<string, unknown>): StrategyEntry[] {
  return Object.entries(breakdown ?? {})
    .filter(([name, raw]) => !name.startsWith("_") && raw && typeof raw === "object")
    .map(([name, raw]) => {
      const obj = raw as Record<string, unknown>;
      const indicatorValues = (
        obj.indicator_values && typeof obj.indicator_values === "object"
          ? obj.indicator_values
          : {}
      ) as Record<string, unknown>;
      return {
        name,
        direction: typeof obj.direction === "string" ? obj.direction : "NEUTRAL",
        confidence: asNumber(obj.confidence),
        weight: asNumber(obj.weight),
        contribution: asNumber(obj.contribution),
        reason: typeof obj.reason === "string" ? obj.reason : "",
        indicatorValues,
      };
    })
    .sort((a, b) => (b.contribution ?? -1) - (a.contribution ?? -1));
}

function breakdownObject(signal: Signal | null, key: string): Record<string, unknown> | null {
  const raw = signal?.strategy_breakdown?.[key];
  return raw && typeof raw === "object" ? raw as Record<string, unknown> : null;
}

function positionSourceSignalId(position: Position) {
  const parsed = asNumber(position.metadata_json?.source_signal_id);
  return parsed === null ? null : Math.trunc(parsed);
}

function linkedPositionsForSignal(signal: Signal | null, fallbackPositions: Position[], snapshotPositions: Position[]) {
  if (!signal) return [];
  if (snapshotPositions.length > 0) return snapshotPositions;
  return fallbackPositions.filter((position) => positionSourceSignalId(position) === signal.id);
}

function selectedSignalFallback(
  signals: Signal[],
  focusSignalId: number | undefined,
  selectedKey: string,
  selectedTf: string,
) {
  if (focusSignalId != null) {
    const focused = signals.find((signal) => signal.id === focusSignalId);
    if (focused) return focused;
  }
  const marketSignals = signals.filter((signal) => signal.market && marketKey(signal.market) === selectedKey);
  const timeframeMatch = marketSignals.find((signal) => !selectedTf || signal.timeframe === selectedTf);
  return timeframeMatch ?? marketSignals[0] ?? signals[0] ?? null;
}

export function CoinScreen({
  markets,
  signals,
  positions,
  health,
  chartMarkets,
  initialCandles,
  initialMarket,
  initialTf,
  focusSignalId,
  focusMarketKey,
  focusTf,
}: Props) {
  const allMarkets = useMemo(() => chartMarkets.length > 0 ? chartMarkets : markets, [chartMarkets, markets]);
  const initialKey = focusMarketKey ?? (initialMarket ? marketKey(initialMarket) : signals[0]?.market ? marketKey(signals[0].market) : "");
  const [selectedKey, setSelectedKey] = useState(initialKey);
  const [selectedTf, setSelectedTf] = useState(focusTf ?? initialTf ?? signals[0]?.timeframe ?? "");
  const [selectedSignalId, setSelectedSignalId] = useState<number | null>(focusSignalId ?? null);

  useEffect(() => {
    if (focusMarketKey) setSelectedKey(focusMarketKey);
    if (focusTf) setSelectedTf(focusTf);
    if (focusSignalId != null) setSelectedSignalId(focusSignalId);
  }, [focusMarketKey, focusSignalId, focusTf]);

  const fallbackSignal = useMemo(
    () => selectedSignalFallback(signals, selectedSignalId ?? undefined, selectedKey, selectedTf),
    [selectedKey, selectedSignalId, selectedTf, signals],
  );
  const effectiveSignalId = selectedSignalId ?? fallbackSignal?.id ?? null;
  const { data: snapshot, isFetching, isError } = useSignalSnapshot(effectiveSignalId);
  const signal = snapshot?.signal ?? fallbackSignal;
  const selectedMarket = signal?.market ?? allMarkets.find((market) => marketKey(market) === selectedKey) ?? initialMarket;
  const sourceTime = snapshot?.source_candle_open_time ?? signal?.source_candle_open_time ?? null;
  const candles = snapshot?.candles ?? (signal && initialMarket?.id === signal.market_id && initialTf === signal.timeframe ? initialCandles : []);
  const indicators = snapshot?.indicators ?? [];
  const linkedPositions = linkedPositionsForSignal(signal, positions, snapshot?.positions ?? []);
  const signalLogs = snapshot?.decision_logs ?? [];
  const strategyEntries = useMemo(() => parseStrategyEntries(signal?.strategy_breakdown ?? {}), [signal]);
  const scoreMeta = breakdownObject(signal, "_score");
  const activeMarketSignals = useMemo(() => {
    if (!selectedMarket) return signals;
    return signals.filter((item) => item.market && marketKey(item.market) === marketKey(selectedMarket));
  }, [selectedMarket, signals]);

  useEffect(() => {
    if (!signal?.market) return;
    setSelectedKey(marketKey(signal.market));
    setSelectedTf(signal.timeframe);
  }, [signal?.id, signal?.market, signal?.timeframe]);

  if (!signal) {
    return (
      <div className="page">
        <div className="card">
          <div className="card-body" style={{ padding: 28, textAlign: "center", color: "var(--fg-3)" }}>
            Nessun segnale disponibile per costruire lo snapshot.
          </div>
        </div>
      </div>
    );
  }

  const primaryPosition = linkedPositions[0] ?? null;
  const expectedR = signal.calibrated_expected_r;
  const winRate = signal.calibrated_win_rate;

  return (
    <div className="page">
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 18, marginBottom: "var(--gap)" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
            <h1 className="page-title" style={{ margin: 0 }}>{selectedMarket?.symbol ?? "-"}</h1>
            {selectedMarket && <MarketBadge market={selectedMarket.market_type} />}
            <TfChip tf={signal.timeframe} />
            <span className="badge muted">#{signal.id}</span>
          </div>
          <div className="page-sub">
            Snapshot generazione segnale · source candle {formatDateTime(sourceTime)} · creato {formatDateTime(signal.created_at)}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", justifyContent: "flex-end" }}>
          <DirBadge dir={signal.direction} />
          <ScoreChip score={signal.score} dir={signal.direction} />
          <StatusBadge status={primaryPosition?.status === "open" ? "active" : signal.status} />
        </div>
      </div>

      <div className="stat-grid" style={{ marginBottom: "var(--gap)", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" }}>
        <SmStat label="Entry" value={fmt.price(signal.entry_price)} />
        <SmStat label="Stop" value={fmt.price(signal.stop_loss)} tone="down" />
        <SmStat label="TP1 / TP2" value={`${fmt.price(signal.take_profit_1)} / ${fmt.price(signal.take_profit_2)}`} tone="up" />
        <SmStat label="Risk reward" value={signal.risk_reward ?? "-"} />
        <SmStat label="Network" value={health.network} />
      </div>

      {isError && (
        <div className="card" style={{ marginBottom: "var(--gap)", borderColor: "var(--warn)" }}>
          <div className="card-body" style={{ color: "var(--warn)", fontSize: "var(--fz-sm)" }}>
            Snapshot API non disponibile. Mostro i dati gia presenti nel feed segnali.
          </div>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 360px), 1fr))", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Signal context</span>
            <span className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>
              {isFetching ? "refresh..." : "DB snapshot"}
            </span>
          </div>
          <SignalContextChart candles={candles} signal={signal} sourceTime={sourceTime} />
        </div>

        <div className="card">
          <div className="card-head">
            <span className="title">Trigger</span>
            <span className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>{signal.confidence_label}</span>
          </div>
          <div className="card-body" style={{ display: "grid", gap: 14 }}>
            <div style={{ padding: 12, background: "var(--bg-2)", borderRadius: "var(--radius-sm)", color: "var(--fg-2)", fontSize: "var(--fz-sm)", lineHeight: 1.45 }}>
              {signal.reason || "Nessun trigger reason salvato."}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12 }}>
              <Kv label="Long raw" value={formatScalar(scoreMeta?.long_raw)} tone="long" />
              <Kv label="Short raw" value={formatScalar(scoreMeta?.short_raw)} tone="short" />
              <Kv label="Voting weight" value={formatScalar(scoreMeta?.voting_weight)} />
              <Kv label="All weight" value={formatScalar(scoreMeta?.all_weight)} />
              <Kv label="Expected R" value={expectedR != null ? expectedR.toFixed(2) : "-"} />
              <Kv label="Win rate" value={winRate != null ? fmt.pct(winRate * 100) : "-"} />
            </div>
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Strategy votes</span>
            <span className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>{strategyEntries.length} strategie</span>
          </div>
          <div className="card-body">
            <StrategyContributionsChart breakdown={signal.strategy_breakdown ?? {}} signalDirection={signal.direction} />
            <StrategyTable entries={strategyEntries} signalDirection={signal.direction} />
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="title">Indicator evidence</span>
            <span className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>{formatDateTime(sourceTime)}</span>
          </div>
          <IndicatorEvidence indicators={indicators} strategyEntries={strategyEntries} />
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))", gap: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Execution state</span>
            <span className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>{linkedPositions.length} posizioni</span>
          </div>
          <ExecutionState signal={signal} positions={linkedPositions} />
        </div>

        <div className="card">
          <div className="card-head">
            <span className="title">Signal history</span>
            <span className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>{selectedMarket?.symbol ?? "-"}</span>
          </div>
          <SignalHistory
            signals={activeMarketSignals}
            selectedId={signal.id}
            onSelect={(next) => {
              setSelectedSignalId(next.id);
              setSelectedKey(marketKey(next.market));
              setSelectedTf(next.timeframe);
            }}
          />
        </div>
      </div>

      <div className="card" style={{ marginTop: "var(--gap)" }}>
        <div className="card-head">
          <span className="title">Decision timeline</span>
          <span className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>{signalLogs.length} eventi</span>
        </div>
        <DecisionTimeline logs={signalLogs} />
      </div>
    </div>
  );
}

function SmStat({ label, value, tone = "" }: { label: string; value: string; tone?: string }) {
  return (
    <div className="stat-card">
      <span className="stat-label">{label}</span>
      <strong className={"stat-value mono " + tone}>{value}</strong>
    </div>
  );
}

function StrategyTable({ entries, signalDirection }: { entries: StrategyEntry[]; signalDirection: string }) {
  if (entries.length === 0) {
    return <div style={{ padding: 20, textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>Nessun breakdown strategia disponibile.</div>;
  }
  const signalDir = normalizeDir(signalDirection);
  return (
    <div style={{ overflowX: "auto", marginTop: 12 }}>
      <table className="tbl">
        <thead>
          <tr>
            <th>Strategia</th>
            <th>Dir</th>
            <th className="num">Conf</th>
            <th className="num">Peso</th>
            <th className="num">Contrib</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => {
            const dir = normalizeDir(entry.direction);
            const tone = dir === "NEUTRAL" ? "muted" : dir === signalDir ? "long" : "short";
            return (
              <tr key={entry.name}>
                <td className="mono fg">{entry.name}</td>
                <td><span className={"badge " + tone}>{entry.direction}</span></td>
                <td className="num mono">{entry.confidence != null ? `${entry.confidence}%` : "-"}</td>
                <td className="num mono">{entry.weight ?? "-"}</td>
                <td className="num mono">{entry.contribution != null ? entry.contribution.toFixed(2) : "-"}</td>
                <td className="muted" style={{ minWidth: 220 }}>{entry.reason || "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function IndicatorEvidence({ indicators, strategyEntries }: { indicators: IndicatorSnapshot[]; strategyEntries: StrategyEntry[] }) {
  const rows = indicators.length > 0
    ? indicators.map((indicator) => ({ name: indicator.indicator_name, values: indicator.values }))
    : strategyEntries
        .filter((entry) => Object.keys(entry.indicatorValues).length > 0)
        .map((entry) => ({ name: entry.name, values: entry.indicatorValues }));

  if (rows.length === 0) {
    return (
      <div style={{ padding: 24, textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
        Nessun indicatore persistito per la candela sorgente.
      </div>
    );
  }

  return (
    <div className="card-body" style={{ display: "grid", gap: 10 }}>
      {rows.map((row) => {
        const entries = Object.entries(row.values ?? {}).filter(([key]) => !key.startsWith("_")).slice(0, 6);
        return (
          <div key={row.name} style={{ border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", padding: 10, background: "var(--bg-2)" }}>
            <div className="mono fg" style={{ fontSize: "var(--fz-sm)", marginBottom: 8 }}>{row.name}</div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 8 }}>
              {entries.map(([key, value]) => (
                <div key={key}>
                  <div className="muted" style={{ fontSize: 10, textTransform: "uppercase" }}>{key}</div>
                  <div className="mono" style={{ fontSize: "var(--fz-sm)" }}>{formatScalar(value)}</div>
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ExecutionState({ signal, positions }: { signal: Signal; positions: Position[] }) {
  if (positions.length === 0) {
    return (
      <div className="card-body" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12 }}>
        <Kv label="Status" value={signal.status} />
        <Kv label="Mode" value="signal only" tone="muted" />
        <Kv label="Entry" value={fmt.price(signal.entry_price)} />
        <Kv label="R/R" value={signal.risk_reward ?? "-"} />
      </div>
    );
  }

  return (
    <div className="card-body" style={{ display: "grid", gap: 10 }}>
      {positions.map((position) => {
        const pnl = asNumber(position.realized_pnl ?? position.unrealized_pnl);
        return (
          <div key={position.id} style={{ border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", padding: 10, background: "var(--bg-2)" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span className={"badge " + (position.side === "long" ? "long" : "short")}>{position.side}</span>
                <span className="mono fg">#{position.id}</span>
                <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>{position.mode}</span>
              </div>
              <span className={"badge " + (position.status === "open" ? "long" : "muted")}>{position.status}</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 10, marginTop: 10 }}>
              <Kv label="Entry" value={fmt.price(position.entry_price)} />
              <Kv label="Size" value={fmt.price(position.size)} />
              <Kv label="PnL" value={fmt.pnl(pnl)} tone={pnl != null && pnl >= 0 ? "long" : "short"} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SignalHistory({ signals, selectedId, onSelect }: { signals: Signal[]; selectedId: number; onSelect: (signal: Signal) => void }) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="tbl">
        <thead>
          <tr>
            <th>Id</th>
            <th>TF</th>
            <th>Dir</th>
            <th>Score</th>
            <th>Status</th>
            <th>Eta</th>
          </tr>
        </thead>
        <tbody>
          {signals.slice(0, 12).map((signal) => (
            <tr
              key={signal.id}
              onClick={() => onSelect(signal)}
              style={{ cursor: "pointer", background: signal.id === selectedId ? "var(--bg-2)" : undefined }}
            >
              <td className="mono fg">#{signal.id}</td>
              <td><TfChip tf={signal.timeframe} /></td>
              <td><DirBadge dir={signal.direction} /></td>
              <td><ScoreChip score={signal.score} dir={signal.direction} /></td>
              <td><StatusBadge status={signal.status} /></td>
              <td className="muted mono">{fmt.relTime(signal.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DecisionTimeline({ logs }: { logs: { id: number; timestamp: string; event_type: string; message: string; data: Record<string, unknown> }[] }) {
  if (logs.length === 0) {
    return <div className="card-body" style={{ color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>Nessun decision log collegato al segnale.</div>;
  }
  return (
    <div className="card-body" style={{ display: "grid", gap: 8 }}>
      {logs.map((log) => (
        <div key={log.id} style={{ display: "grid", gridTemplateColumns: "160px 1fr", gap: 12, borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
          <div className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>{formatDateTime(log.timestamp)}</div>
          <div>
            <div className="mono fg" style={{ fontSize: "var(--fz-sm)" }}>{log.event_type.replaceAll("_", " ")}</div>
            {log.message && <div className="muted" style={{ fontSize: "var(--fz-sm)", marginTop: 2 }}>{log.message}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

function SignalContextChart({ candles, signal, sourceTime }: { candles: Candle[]; signal: Signal; sourceTime: string | null }) {
  const parsed = useMemo(() => candles.map((candle) => ({
    time: new Date(candle.open_time).getTime(),
    open: asNumber(candle.open),
    high: asNumber(candle.high),
    low: asNumber(candle.low),
    close: asNumber(candle.close),
  })).filter((candle): candle is { time: number; open: number; high: number; low: number; close: number } =>
    Number.isFinite(candle.time) && candle.open !== null && candle.high !== null && candle.low !== null && candle.close !== null
  ), [candles]);

  if (parsed.length === 0) {
    return (
      <div style={{ minHeight: 320, display: "grid", placeItems: "center", color: "var(--fg-3)", textAlign: "center", padding: 24 }}>
        <div>
          <Icon name="info" size={22} />
          <div style={{ marginTop: 10, fontSize: "var(--fz-sm)" }}>Candele DB non disponibili per questo snapshot.</div>
        </div>
      </div>
    );
  }

  const width = 920;
  const height = 340;
  const pad = { top: 22, right: 62, bottom: 28, left: 18 };
  const prices = parsed.flatMap((candle) => [candle.high, candle.low]);
  for (const value of [signal.entry_price, signal.stop_loss, signal.take_profit_1, signal.take_profit_2, signal.take_profit_3]) {
    const n = asNumber(value);
    if (n !== null) prices.push(n);
  }
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const range = max - min || 1;
  const xStep = (width - pad.left - pad.right) / Math.max(1, parsed.length - 1);
  const candleWidth = Math.max(3, Math.min(9, xStep * 0.58));
  const y = (price: number) => pad.top + ((max - price) / range) * (height - pad.top - pad.bottom);
  const sourceMs = sourceTime ? new Date(sourceTime).getTime() : null;
  const sourceIndex = sourceMs === null ? -1 : parsed.findIndex((candle) => candle.time === sourceMs);
  const markerIndex = sourceIndex >= 0 ? sourceIndex : Math.max(0, parsed.length - 1);
  const markerX = pad.left + markerIndex * xStep;
  const isLong = signal.direction.includes("LONG");

  const line = (price: string | null, color: string, label: string) => {
    const n = asNumber(price);
    if (n === null) return null;
    const yy = y(n);
    return (
      <g key={label}>
        <line x1={pad.left} x2={width - pad.right + 10} y1={yy} y2={yy} stroke={color} strokeDasharray="5 5" strokeWidth="1" />
        <text x={width - pad.right + 14} y={yy + 4} fill={color} fontSize="10" fontFamily="monospace">{label}</text>
      </g>
    );
  };

  return (
    <div style={{ padding: 12 }}>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height: 360, display: "block" }} role="img" aria-label="Signal context chart">
        <rect x="0" y="0" width={width} height={height} fill="var(--bg-1)" />
        {[0, 0.25, 0.5, 0.75, 1].map((tick) => {
          const yy = pad.top + tick * (height - pad.top - pad.bottom);
          return <line key={tick} x1={pad.left} x2={width - pad.right} y1={yy} y2={yy} stroke="var(--border)" strokeWidth="1" opacity="0.55" />;
        })}
        {parsed.map((candle, index) => {
          const x = pad.left + index * xStep;
          const up = candle.close >= candle.open;
          const color = up ? "var(--long)" : "var(--short)";
          const bodyTop = y(Math.max(candle.open, candle.close));
          const bodyBottom = y(Math.min(candle.open, candle.close));
          const bodyHeight = Math.max(1, bodyBottom - bodyTop);
          const isSource = index === sourceIndex;
          return (
            <g key={`${candle.time}-${index}`} opacity={isSource ? 1 : 0.78}>
              {isSource && <rect x={x - xStep / 2} y={pad.top} width={xStep} height={height - pad.top - pad.bottom} fill="var(--accent-soft)" />}
              <line x1={x} x2={x} y1={y(candle.high)} y2={y(candle.low)} stroke={color} strokeWidth="1" />
              <rect x={x - candleWidth / 2} y={bodyTop} width={candleWidth} height={bodyHeight} fill={color} rx="1" />
            </g>
          );
        })}
        {line(signal.entry_price, "var(--fg)", "ENTRY")}
        {line(signal.stop_loss, "var(--short)", "SL")}
        {line(signal.take_profit_1, "var(--long)", "TP1")}
        {line(signal.take_profit_2, "var(--long)", "TP2")}
        {line(signal.take_profit_3, "var(--long)", "TP3")}
        <g transform={`translate(${markerX}, ${isLong ? height - pad.bottom + 2 : pad.top - 4})`}>
          <path d={isLong ? "M0,-18 L8,-6 L3,-6 L3,0 L-3,0 L-3,-6 L-8,-6 Z" : "M0,18 L8,6 L3,6 L3,0 L-3,0 L-3,6 L-8,6 Z"} fill={isLong ? "var(--long)" : "var(--short)"} />
          <text x="12" y={isLong ? -7 : 12} fill="var(--fg)" fontSize="11" fontFamily="monospace">signal {signal.score}</text>
        </g>
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, color: "var(--fg-3)", fontSize: "var(--fz-xs)" }}>
        <span>{formatDateTime(candles[0]?.open_time)}</span>
        <span>{sourceTime ? `source ${formatDateTime(sourceTime)}` : "source candle non salvata"}</span>
        <span>{formatDateTime(candles.at(-1)?.open_time)}</span>
      </div>
    </div>
  );
}
