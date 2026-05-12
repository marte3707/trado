"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Icon, ScoreChip, DirBadge, TfChip, MarketBadge, StratList, Kv, Seg, fmt, StatusBadge } from "@/components/atoms";
import { Health, IndicatorSnapshot, Market, Position, Signal, apiGet } from "@/lib/api";
import { ChartPanel, ChartOverlays, LiveChartUpdate } from "@/components/ChartPanel";

type Candle = {
  open_time: string; open: string; high: string; low: string; close: string; volume: string;
};

interface Props {
  markets: Market[];
  signals: Signal[];
  positions: Position[];
  health: Health;
  chartMarkets: Market[];
  initialCandles: Candle[];
  initialMarket: Market | null;
  initialTf: string;
  focusMarketKey?: string;
  focusTf?: string;
}

function marketKey(m: Market) { return `${m.market_type}:${m.symbol}`; }

function asNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatIndicatorScalar(value: unknown): string {
  const n = asNumber(value);
  if (n !== null) return n.toLocaleString("it-IT", { maximumFractionDigits: 4 });
  if (typeof value === "string" && value) return value;
  return "—";
}

function formatCompactEur(value: number | null): string {
  if (value == null) return "—";
  if (Math.abs(value) >= 1e9) return `€${(value / 1e9).toFixed(2)}B`;
  if (Math.abs(value) >= 1e6) return `€${(value / 1e6).toFixed(1)}M`;
  if (Math.abs(value) >= 1e3) return `€${(value / 1e3).toFixed(1)}K`;
  return fmt.money(value);
}

function candleStats(candles: Candle[]) {
  const parsed = candles
    .map((candle) => ({
      time: new Date(candle.open_time).getTime(),
      open: asNumber(candle.open),
      close: asNumber(candle.close),
      volume: asNumber(candle.volume)
    }))
    .filter((candle): candle is { time: number; open: number; close: number; volume: number } =>
      Number.isFinite(candle.time) && candle.open !== null && candle.close !== null && candle.volume !== null
    );
  const latest = parsed.at(-1);
  if (!latest) return { price: null, change24: null, vol24: null };
  const cutoff = latest.time - 24 * 60 * 60 * 1000;
  const window = parsed.filter((candle) => candle.time >= cutoff);
  const prior = [...parsed].reverse().find((candle) => candle.time <= cutoff);
  const reference = prior?.close ?? window[0]?.open ?? null;
  const change24 = reference && reference !== 0 ? ((latest.close - reference) / reference) * 100 : null;
  const vol24 = window.reduce((sum, candle) => sum + candle.close * candle.volume, 0);
  return { price: latest.close, change24, vol24 };
}

function positionSourceSignalId(position: Position) {
  const parsed = asNumber(position.metadata_json?.source_signal_id);
  return parsed === null ? null : Math.trunc(parsed);
}

function isSignalOpen(signal: Signal, positions: Position[]) {
  const status = signal.status.toLowerCase();
  if (status === "new" || status === "active") return true;
  return positions.some((position) => (
    position.status.toLowerCase() === "open" && positionSourceSignalId(position) === signal.id
  ));
}

function openPositionForSignal(signal: Signal, positions: Position[]) {
  return positions.find((position) => (
    position.status.toLowerCase() === "open" && positionSourceSignalId(position) === signal.id
  )) ?? null;
}

function displaySignalStatus(signal: Signal, positions: Position[]) {
  const position = openPositionForSignal(signal, positions);
  if (position) return { label: `${signal.status} · posizione aperta`, tone: "long" };
  if (signal.status === "new" || signal.status === "active") return { label: signal.status, tone: "info" };
  if (signal.status === "ignored" || signal.status === "closed") return { label: signal.status, tone: "muted" };
  return { label: signal.status, tone: signal.status === "executed" ? "warn" : "muted" };
}

function preferredIndicatorValue(name: string, values: Record<string, unknown>) {
  const keysByName: Record<string, string[]> = {
    rsi: ["rsi"],
    macd: ["histogram", "macd"],
    ema_crossover: ["ema_9", "ema_21", "ema_200"],
    supertrend: ["trend", "direction"],
    bollinger_bands: ["percent_b", "middle"],
    vwap: ["vwap"],
    volume_spike: ["volume", "volume_ma"],
    support_resistance: ["nearest_support", "nearest_resistance"],
    fibonacci: ["nearest_level", "trend"],
    candlestick_patterns: ["pattern", "confidence"]
  };
  const keys = keysByName[name] ?? [];
  for (const key of keys) {
    if (values[key] != null) return [key, values[key]] as const;
  }
  const scalar = Object.entries(values).find(([key, value]) =>
    !key.startsWith("_") && (typeof value === "number" || typeof value === "string")
  );
  return scalar ?? null;
}

function inferredIndicatorTone(name: string, values: Record<string, unknown>): "ok" | "warn" | "bad" {
  const confidence = asNumber(values._confidence ?? values.confidence);
  const direction = String(values._direction ?? values.direction ?? values.trend ?? "").toLowerCase();
  if (confidence !== null) {
    if (direction.includes("short") || direction === "down") return confidence >= 65 ? "bad" : "warn";
    if (direction.includes("long") || direction === "up") return confidence >= 55 ? "ok" : "warn";
    return confidence >= 50 ? "warn" : "bad";
  }
  if (name === "rsi") {
    const rsi = asNumber(values.rsi);
    if (rsi === null) return "warn";
    if (rsi >= 35 && rsi <= 65) return "ok";
    if ((rsi >= 30 && rsi < 35) || (rsi > 65 && rsi <= 70)) return "warn";
    return "bad";
  }
  if (name === "macd") {
    const hist = asNumber(values.histogram);
    if (hist === null) return "warn";
    return hist > 0 ? "ok" : hist < 0 ? "bad" : "warn";
  }
  if (direction === "up" || direction.includes("long")) return "ok";
  if (direction === "down" || direction.includes("short")) return "bad";
  return "warn";
}

function indicatorRow(row: IndicatorSnapshot) {
  const values = row.values ?? {};
  const entries = Object.entries(values).filter(([key]) => !key.startsWith("_"));
  const scalar = preferredIndicatorValue(row.indicator_name, values);
  const confidence = asNumber(values._confidence ?? values.confidence);
  const reason = typeof values._reason === "string" ? values._reason : "";
  const detail = entries
    .filter(([key]) => key !== scalar?.[0])
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${formatIndicatorScalar(value)}`)
    .join(" · ");
  return {
    name: row.indicator_name,
    value: scalar ? formatIndicatorScalar(scalar[1]) : "—",
    status: inferredIndicatorTone(row.indicator_name, values),
    detail: reason || (confidence !== null ? `confidence ${confidence}` : detail) || new Intl.DateTimeFormat("it-IT", { dateStyle: "short", timeStyle: "short" }).format(new Date(row.timestamp)),
  };
}

export function CoinScreen({ markets, signals, positions, health, chartMarkets, initialCandles, initialMarket, initialTf, focusMarketKey, focusTf }: Props) {
  const [selectedKey, setSelectedKey] = useState(focusMarketKey ?? (initialMarket ? marketKey(initialMarket) : ""));
  const [tf, setTf] = useState(focusTf ?? (initialTf || signals[0]?.timeframe || ""));
  const [candles, setCandles] = useState(initialCandles);
  const [indicators, setIndicators] = useState<IndicatorSnapshot[]>([]);
  const [loadingCandles, setLoadingCandles] = useState(false);
  const [search, setSearch] = useState("");
  const [liveStats, setLiveStats] = useState<{ price: number | null; vol24: number | null; updatedAt: number | null }>({
    price: null,
    vol24: null,
    updatedAt: null
  });
  const [overlays, setOverlays] = useState<ChartOverlays>({
    ema: false, bollinger: false, vwap: false, supertrend: false, levels: false, signals: false
  });

  const allMarkets = chartMarkets.length > 0 ? chartMarkets : markets;
  const selectedMarket = allMarkets.find(m => marketKey(m) === selectedKey) ?? allMarkets[0] ?? null;

  const visibleMarkets = allMarkets.filter(m =>
    !search || m.symbol.toLowerCase().includes(search.toLowerCase())
  );

  const selectedMarketKey = selectedMarket ? marketKey(selectedMarket) : selectedKey;
  const marketSignals = signals.filter(s => s.market && marketKey(s.market) === selectedMarketKey);
  const openMarketSignals = marketSignals.filter(s => isSignalOpen(s, positions));
  const activeSignal = openMarketSignals[0] ?? null;
  const activePosition = activeSignal ? openPositionForSignal(activeSignal, positions) : null;
  const timeframeOptions = Array.from(new Set([initialTf, ...signals.map(s => s.timeframe)].filter(Boolean)));
  const derivedStats = useMemo(() => candleStats(candles), [candles]);
  const meta = selectedMarket?.metadata_json ?? {};
  const metaPrice = asNumber(meta.last_price ?? meta.mark_price);
  const metaChange24 = asNumber(meta.price_change_24h_pct);
  const metaVol24 = asNumber(meta.volume_24h_usd);
  const price = liveStats.price ?? derivedStats.price ?? metaPrice;
  const change24 = derivedStats.change24 ?? metaChange24;
  const vol24 = liveStats.vol24 ?? derivedStats.vol24 ?? metaVol24;
  const handleLiveUpdate = useCallback((update: LiveChartUpdate) => {
    setLiveStats(prev => ({
      price: update.price,
      vol24: (prev.vol24 ?? 0) + update.quoteVolumeDelta,
      updatedAt: Date.now()
    }));
  }, []);

  async function loadCandles(market: Market | null, timeframe: string) {
    if (!market) { setCandles([]); return; }
    setLoadingCandles(true);
    try {
      const data = await apiGet<Candle[]>(
        `/api/candles?symbol=${encodeURIComponent(market.symbol)}&market_type=${encodeURIComponent(market.market_type)}&timeframe=${encodeURIComponent(timeframe)}`
      );
      setCandles(data);
    } catch {
      setCandles([]);
    } finally {
      setLoadingCandles(false);
    }
  }

  async function loadIndicators(market: Market | null, timeframe: string) {
    if (!market) { setIndicators([]); return; }
    try {
      const data = await apiGet<IndicatorSnapshot[]>(
        `/api/indicators?symbol=${encodeURIComponent(market.symbol)}&market_type=${encodeURIComponent(market.market_type)}&timeframe=${encodeURIComponent(timeframe)}`
      );
      setIndicators(data);
    } catch {
      setIndicators([]);
    }
  }

  useEffect(() => {
    if (selectedMarket) {
      loadCandles(selectedMarket, tf);
      loadIndicators(selectedMarket, tf);
    }
  }, [selectedKey, tf]);

  useEffect(() => {
    if (focusMarketKey) setSelectedKey(focusMarketKey);
    if (focusTf) setTf(focusTf);
  }, [focusMarketKey, focusTf]);

  useEffect(() => {
    setLiveStats({ price: derivedStats.price, vol24: derivedStats.vol24, updatedAt: null });
  }, [derivedStats.price, derivedStats.vol24, selectedMarketKey, tf]);

  return (
    <div className="page">
      {/* Header */}
      <div style={{ display: "flex", alignItems: "flex-end", gap: 18, marginBottom: "var(--gap)" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
            <h1 className="page-title" style={{ margin: 0 }}>
              {selectedMarket?.symbol ?? "—"}
              <span className="muted" style={{ fontWeight: 300 }}> / USDC</span>
            </h1>
            {selectedMarket && <MarketBadge market={selectedMarket.market_type} />}
            <span className="badge muted">{selectedMarket?.base ?? "—"}</span>
          </div>
          <div className="page-sub">Hyperliquid · {selectedMarket?.market_type === "perp" ? "Perpetual" : "Spot"}</div>
        </div>
        <div style={{ marginLeft: 24 }}>
          {price != null ? (
            <>
              <div className="mono" style={{ fontSize: "var(--fz-3xl)", fontWeight: 500, lineHeight: 1, letterSpacing: "-0.02em" }}>
                €{fmt.price(price)}
              </div>
              {change24 != null && (
                <div className={"mono " + (change24 >= 0 ? "up" : "down")} style={{ marginTop: 4, fontSize: "var(--fz-md)" }}>
                  {fmt.pct(change24)} <span className="muted">24h</span>
                </div>
              )}
            </>
          ) : (
            <div className="muted mono">—</div>
          )}
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="text"
            placeholder="Cerca symbol…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="filter"
            style={{ height: 28, paddingLeft: 10, width: 140, fontSize: "var(--fz-sm)" }}
          />
          <select
            value={selectedKey}
            onChange={e => setSelectedKey(e.target.value)}
            className="filter"
            style={{ height: 28, paddingLeft: 10, paddingRight: 26, fontSize: "var(--fz-sm)" }}
          >
            {visibleMarkets.map(m => (
              <option key={marketKey(m)} value={marketKey(m)}>{m.symbol} {m.market_type.toUpperCase()}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Stat strip */}
      <div className="stat-grid" style={{ marginBottom: "var(--gap)", gridTemplateColumns: "repeat(5, 1fr)" }}>
        <SmStat label="24h volume" value={formatCompactEur(vol24)} />
        <SmStat label="24h change" value={change24 != null ? fmt.pct(change24) : "—"} tone={change24 != null ? (change24 >= 0 ? "up" : "down") : ""} />
        <SmStat label="Prezzo live" value={price != null ? `€${fmt.price(price)}` : "—"} />
        <SmStat label="Segnali aperti" value={`${openMarketSignals.length}`} />
        <SmStat label="Network" value={health.network} />
      </div>

      {/* Chart + indicators */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 300px", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <div className="chart">
          <div className="chart-head">
            <span className="mono fg" style={{ fontSize: "var(--fz-md)" }}>
              {selectedMarket?.symbol ?? "—"}/USDC · {tf}
            </span>
            <Seg
              value={tf}
              onChange={v => setTf(v)}
              options={timeframeOptions.map(t => [t, t] as [string, string])}
            />
            {loadingCandles && <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>loading…</span>}
            <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
              {([ ["ema","EMA"],["bollinger","BB"],["vwap","VWAP"],["supertrend","ST"],["levels","Livelli"],["signals","Segnali"] ] as [keyof ChartOverlays, string][]).map(([key, label]) => (
                <ChartToggle key={key} on={overlays[key]} label={label}
                  onClick={() => setOverlays(o => ({ ...o, [key]: !o[key] }))} />
              ))}
            </div>
          </div>
          <ChartPanel
            candles={candles}
            signals={marketSignals}
            market={selectedMarket}
            timeframe={tf}
            network={health.network}
            overlays={overlays}
            onLiveUpdate={handleLiveUpdate}
          />
        </div>

        <div className="card">
          <div className="card-head">
            <span className="title">Indicatori</span>
            <span className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>{tf}</span>
          </div>
          {indicators.length === 0 && (
            <div style={{ padding: 24, textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
              Nessun indicatore salvato nel DB per questo mercato/timeframe
            </div>
          )}
          {indicators.map(indicatorRow).map((row, i) => (
            <IndRow key={i} name={row.name} value={row.value} status={row.status} detail={row.detail} />
          ))}
        </div>
      </div>

      {/* Active signal + history on this coin */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Segnale attivo</span>
            {activeSignal && <ScoreChip score={activeSignal.score} dir={activeSignal.direction} />}
            <div className="right">
              {activeSignal
                ? <StatusBadge status={activePosition ? "active" : activeSignal.status} />
                : <span className="badge muted">nessun segnale</span>
              }
            </div>
          </div>
          {activeSignal ? (
            <div className="card-body">
              <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 14, marginBottom: 12 }}>
                <Kv label="Entry" value={fmt.price(activeSignal.entry_price)} />
                <Kv label="SL" value={fmt.price(activeSignal.stop_loss)} tone="short" />
                <Kv label="TP1" value={fmt.price(activeSignal.take_profit_1)} tone="long" />
                <Kv label="TP2" value={fmt.price(activeSignal.take_profit_2)} tone="long" />
                {activeSignal.take_profit_3 && <Kv label="TP3" value={fmt.price(activeSignal.take_profit_3)} tone="long" />}
                <Kv label="R/R" value={activeSignal.risk_reward ?? "—"} />
                <Kv label="TF" value={activeSignal.timeframe} />
                <Kv label="Score" value={String(activeSignal.score)} />
                {activePosition && <Kv label="Position" value={`#${activePosition.id} open`} tone="long" />}
              </div>
              {activeSignal.reason && (
                <div style={{ padding: 12, background: "var(--bg-2)", borderRadius: "var(--radius-sm)", fontSize: "var(--fz-sm)", color: "var(--fg-2)", marginBottom: 12 }}>
                  {activeSignal.reason}
                </div>
              )}
              <div style={{ display: "flex", gap: 6 }}>
                <StratList strats={Object.keys(activeSignal.strategy_breakdown ?? {})} max={5} />
              </div>
            </div>
          ) : (
            <div style={{ padding: 24, textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
              Nessun segnale attivo su {selectedMarket?.symbol ?? "questo mercato"}
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <span className="title">Storico segnali</span>
            <div className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>{selectedMarket?.symbol ?? "—"}</div>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th>TF</th><th>Dir</th><th>Score</th><th style={{ textAlign: "right" }}>Entry</th><th>Status</th><th>Età</th>
              </tr>
            </thead>
            <tbody>
              {marketSignals.length === 0 && (
                <tr><td colSpan={6} style={{ textAlign: "center", padding: "24px 0", color: "var(--fg-3)" }}>Nessun segnale</td></tr>
              )}
              {marketSignals.slice(0, 8).map(s => {
                const status = displaySignalStatus(s, positions);
                return (
                  <tr key={s.id}>
                    <td><TfChip tf={s.timeframe} /></td>
                    <td><DirBadge dir={s.direction} /></td>
                    <td><ScoreChip score={s.score} dir={s.direction} /></td>
                    <td className="num fg">{fmt.price(s.entry_price)}</td>
                    <td>
                      <span className={"badge " + status.tone}>
                        {status.label}
                      </span>
                    </td>
                    <td className="muted mono">{fmt.relTime(s.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

/* ── helpers ─────────────────────────────────────────────────────────────── */
function ChartToggle({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button
      className="btn ghost sm"
      onClick={onClick}
      style={{
        borderColor: on ? "var(--accent-border)" : "transparent",
        color: on ? "var(--accent)" : "var(--fg-3)",
        background: on ? "var(--accent-soft)" : "transparent",
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
      }}
    >
      <Icon name={on ? "check" : "plus"} size={11} />
      {label}
    </button>
  );
}

function SmStat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="stat" style={{ padding: "12px 14px" }}>
      <div className="stat-label">{label}</div>
      <div className={"mono " + (tone === "up" ? "up" : tone === "down" ? "down" : "")} style={{ fontSize: "var(--fz-lg)", fontWeight: 500 }}>
        {value}
      </div>
    </div>
  );
}

function IndRow({ name, value, status, detail }: { name: string; value: string | number; status: "ok" | "warn" | "bad"; detail: string }) {
  const dot = status === "ok" ? "green" : status === "warn" ? "amber" : "red";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 16px", borderBottom: "1px solid var(--border)" }}>
      <span className={"dot " + dot} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: "var(--fz-sm)" }}>{name}</div>
        <div className="muted" style={{ fontSize: 10, marginTop: 1 }}>{detail}</div>
      </div>
      <div className="mono fg" style={{ fontSize: "var(--fz-sm)" }}>{value}</div>
    </div>
  );
}
