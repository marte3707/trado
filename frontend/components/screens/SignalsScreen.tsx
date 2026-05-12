"use client";

import React, {  useState } from "react";
import {
  Icon, ScoreChip, DirBadge, TfChip, MarketBadge, StratList, Kv, SectionLabel, fmt, StatusBadge, Seg
} from "@/components/atoms";
import {
  Health, Market, Signal, Strategy, apiPost, apiDelete
} from "@/lib/api";

interface Props {
  signals: Signal[];
  strategies: Strategy[];
  markets: Market[];
  health: Health;
  threshold: number;
  onRefresh: () => void;
  onOpenChart: (signal: Signal) => void;
}

export function SignalsScreen({ signals, strategies, health, threshold, onRefresh, onOpenChart }: Props) {
  const [market, setMarket] = useState("all");
  const [dir, setDir] = useState("all");
  const [tf, setTf] = useState("all");
  const [stratFilter, setStratFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("open");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const OPEN_STATUSES = ["new", "active", "executed"];
  const CLOSED_STATUSES = ["closed", "ignored", "invalidated"];

  const filtered = signals.filter(s => {
    if (s.score < threshold) return false;
    if (statusFilter === "open" && !OPEN_STATUSES.includes(s.status)) return false;
    if (statusFilter === "closed" && !CLOSED_STATUSES.includes(s.status)) return false;
    if (market !== "all" && s.market?.market_type !== market) return false;
    if (dir !== "all") {
      if (dir === "LONG" && !s.direction.includes("LONG")) return false;
      if (dir === "SHORT" && !s.direction.includes("SHORT")) return false;
      if (dir === "NEUTRAL" && (s.direction.includes("LONG") || s.direction.includes("SHORT"))) return false;
    }
    if (tf !== "all" && s.timeframe !== tf) return false;
    if (stratFilter !== "all" && !Object.keys(s.strategy_breakdown ?? {}).includes(stratFilter)) return false;
    return true;
  });

  const strong = filtered.filter(s => s.score >= threshold).slice(0, 3);
  const rest = filtered.filter(s => !strong.includes(s));

  const allTfs = Array.from(new Set([...strategies.map(s => s.timeframe), ...signals.map(s => s.timeframe)])).sort();
  const allStrats = Array.from(new Set(strategies.map(s => s.strategy_name)));

  async function action(id: number, label: string, path: string) {
    setBusy(`${label}-${id}`);
    setError(null);
    try {
      await apiPost<unknown>(path);
      onRefresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Errore");
    } finally {
      setBusy(null);
    }
  }

  async function del(id: number) {
    if (!confirm(`Eliminare il segnale #${id}?`)) return;
    setBusy(`del-${id}`);
    setError(null);
    try {
      await apiDelete<unknown>(`/api/signals/${id}`);
      onRefresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Errore");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Segnali live</h1>
          <div className="page-sub">{filtered.length} segnali · soglia score ≥ {threshold} · {strong.length} strong</div>
        </div>
        <div className="page-actions">
          <button className="btn ghost" onClick={onRefresh}><Icon name="refresh" size={12} /></button>
        </div>
      </div>

      {error && (
        <div style={{ padding: "8px 14px", marginBottom: "var(--gap)", background: "var(--short-soft)", border: "1px solid var(--short-border)", borderRadius: "var(--radius)", color: "var(--short)", fontSize: "var(--fz-sm)" }}>
          {error} <button className="btn ghost sm" onClick={() => setError(null)} style={{ marginLeft: 8 }}><Icon name="x" size={11} /></button>
        </div>
      )}

      {/* Filter bar */}
      <div className="card" style={{ marginBottom: "var(--gap)", padding: "10px 14px", display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <Seg value={statusFilter} onChange={setStatusFilter} options={[["all","Tutti"],["open","Aperti"],["closed","Chiusi"]]} />
        <Seg value={market} onChange={setMarket} options={[["all","All"],["spot","Spot"],["perp","Perp"]]} />
        <Seg value={dir} onChange={setDir} options={[["all","All"],["LONG","Long"],["SHORT","Short"],["NEUTRAL","Neutral"]]} />
        <Seg value={tf} onChange={setTf} options={[["all","All"], ...allTfs.map(t => [t, t] as [string, string])]} />
        <select
          value={stratFilter}
          onChange={e => setStratFilter(e.target.value)}
          className="filter"
          style={{ height: 28, paddingLeft: 10, paddingRight: 26, fontSize: "var(--fz-sm)" }}
        >
          <option value="all">Strategia: tutte</option>
          {allStrats.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
          <span className="dot green" /> {filtered.length} matches
        </div>
      </div>

      {/* Strong signal cards */}
      {strong.length > 0 && (
        <>
          <SectionLabel>Strong signals</SectionLabel>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
            {strong.map(s => (
              <BigSignalCard
                key={s.id}
                sig={s}
                health={health}
                busy={busy}
                onPaper={() => action(s.id, "paper", `/api/signals/${s.id}/execute-paper`)}
                onIgnore={() => action(s.id, "ignore", `/api/signals/${s.id}/ignore`)}
                onLive={() => action(s.id, "live", `/api/signals/${s.id}/execute-live`)}
                onDelete={() => del(s.id)}
                onOpenChart={() => onOpenChart(s)}
              />
            ))}
          </div>
        </>
      )}

      {/* Rest table */}
      <SectionLabel>{rest.length} altri segnali</SectionLabel>
      <div className="card">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 20 }} />
              <th>Symbol</th>
              <th>TF</th>
              <th>Dir</th>
              <th>Score</th>
              <th style={{ textAlign: "right" }}>Entry</th>
              <th style={{ textAlign: "right" }}>SL</th>
              <th style={{ textAlign: "right" }}>TP1 · TP2 · TP3</th>
              <th style={{ textAlign: "right" }}>R/R</th>
              <th>Strats</th>
              <th>Età</th>
              <th>Status</th>
              <th style={{ textAlign: "right" }} />
            </tr>
          </thead>
          <tbody>
            {rest.length === 0 && (
              <tr><td colSpan={13} style={{ textAlign: "center", padding: "32px 0", color: "var(--fg-3)" }}>
                {filtered.length === 0 ? "Nessun segnale corrisponde ai filtri" : "Tutti i segnali sono strong"}
              </td></tr>
            )}
            {rest.map(s => (
              <tr
                key={s.id}
                onClick={() => onOpenChart(s)}
                style={{ cursor: s.market ? "pointer" : "default" }}
                title={s.market ? `Apri grafico ${s.market.symbol}` : undefined}
              >
                <td>
                  <span className={"dot " + (s.direction.includes("LONG") ? "green" : s.direction.includes("SHORT") ? "red" : "amber")} />
                </td>
                <td className="fg">
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <strong>{s.market?.symbol}</strong>
                    <MarketBadge market={s.market?.market_type ?? "perp"} />
                  </div>
                </td>
                <td><TfChip tf={s.timeframe} /></td>
                <td><DirBadge dir={s.direction} /></td>
                <td><ScoreChip score={s.score} dir={s.direction} /></td>
                <td className="num fg">{fmt.price(s.entry_price)}</td>
                <td className="num down">{fmt.price(s.stop_loss)}</td>
                <td className="num up" style={{ whiteSpace: "nowrap" }}>
                  {fmt.price(s.take_profit_1)} <span className="muted">·</span> {fmt.price(s.take_profit_2)} <span className="muted">·</span>{" "}
                  {s.take_profit_3 ? fmt.price(s.take_profit_3) : <span className="dim">—</span>}
                </td>
                <td className="num">{s.risk_reward ?? "—"}</td>
                <td><StratList strats={Object.keys(s.strategy_breakdown ?? {})} max={3} /></td>
                <td className="muted mono">{fmt.relTime(s.created_at)}</td>
                <td><StatusBadge status={s.status} /></td>
                <td className="row-action" style={{ textAlign: "right" }}>
                  <div style={{ display: "inline-flex", gap: 4 }}>
                    <button
                      className="btn ghost sm"
                      disabled={s.status !== "new" || busy !== null}
                      onClick={(event) => {
                        event.stopPropagation();
                        action(s.id, "paper", `/api/signals/${s.id}/execute-paper`);
                      }}
                      title="Paper trade"
                    >Paper</button>
                    <button
                      className="btn ghost sm"
                      disabled={s.status !== "new" || busy !== null}
                      onClick={(event) => {
                        event.stopPropagation();
                        action(s.id, "ignore", `/api/signals/${s.id}/ignore`);
                      }}
                      title="Ignora"
                    >
                      <Icon name="x" size={11} />
                    </button>
                    {health.live_trading_allowed && (
                      <button
                        className="btn sm"
                        disabled={s.status !== "new" || busy !== null}
                        onClick={(event) => {
                          event.stopPropagation();
                          action(s.id, "live", `/api/signals/${s.id}/execute-live`);
                        }}
                        title="Esegui live"
                      >Live</button>
                    )}
                    <button
                      className="btn ghost sm"
                      disabled={busy !== null}
                      onClick={(event) => {
                        event.stopPropagation();
                        del(s.id);
                      }}
                      title="Elimina segnale"
                      style={{ color: "var(--short)" }}
                    >
                      <Icon name="x" size={11} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ── Big signal card (strong signals) ─────────────────────────────────────── */
function BigSignalCard({ sig, health, busy, onPaper, onIgnore, onLive, onDelete, onOpenChart }: {
  sig: Signal;
  health: Health;
  busy: string | null;
  onPaper: () => void;
  onIgnore: () => void;
  onLive: () => void;
  onDelete: () => void;
  onOpenChart: () => void;
}) {
  const isLong = sig.direction.includes("LONG");
  return (
    <div
      className="card"
      onClick={onOpenChart}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") onOpenChart();
      }}
      title={sig.market ? `Apri grafico ${sig.market.symbol}` : undefined}
      style={{ borderColor: isLong ? "var(--long-border)" : "var(--short-border)", cursor: sig.market ? "pointer" : "default" }}
    >
      <div style={{ padding: 14, paddingBottom: 0, display: "flex", alignItems: "center", gap: 8 }}>
        <strong style={{ fontSize: "var(--fz-xl)", letterSpacing: "-0.01em" }}>{sig.market?.symbol}</strong>
        <MarketBadge market={sig.market?.market_type ?? "perp"} />
        <TfChip tf={sig.timeframe} />
        <div style={{ marginLeft: "auto" }}><ScoreChip score={sig.score} dir={sig.direction} /></div>
      </div>
      <div style={{ padding: "10px 14px 0", display: "flex", alignItems: "baseline", gap: 8 }}>
        <DirBadge dir={sig.direction} />
        <span className="muted mono" style={{ fontSize: "var(--fz-sm)" }}>· {fmt.relTime(sig.created_at)}</span>
      </div>
      <div style={{ padding: "12px 14px", display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
        <Kv label="Entry" value={fmt.price(sig.entry_price)} />
        <Kv label="SL" value={fmt.price(sig.stop_loss)} tone="short" />
        <Kv label="R/R" value={sig.risk_reward ?? "—"} />
        <Kv label="TP1" value={fmt.price(sig.take_profit_1)} tone="long" />
        <Kv label="TP2" value={fmt.price(sig.take_profit_2)} tone="long" />
        <Kv label="TP3" value={fmt.price(sig.take_profit_3)} tone={sig.take_profit_3 ? "long" : "muted"} />
      </div>
      <div style={{ padding: "0 14px 12px", fontSize: "var(--fz-sm)", color: "var(--fg-2)" }}>
        {sig.reason?.slice(0, 120)}{(sig.reason?.length ?? 0) > 120 ? "…" : ""}
      </div>
      <div style={{ padding: "10px 14px", borderTop: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 6 }}>
        <StratList strats={Object.keys(sig.strategy_breakdown ?? {})} max={5} />
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button className="btn ghost sm" disabled={sig.status !== "new" || busy !== null} onClick={(event) => { event.stopPropagation(); onIgnore(); }}>Ignora</button>
          <button className="btn sm" disabled={sig.status !== "new" || busy !== null} onClick={(event) => { event.stopPropagation(); onPaper(); }}>Paper</button>
          {health.live_trading_allowed && (
            <button className="btn primary sm" disabled={sig.status !== "new" || busy !== null} onClick={(event) => { event.stopPropagation(); onLive(); }}>Live</button>
          )}
          <button
            className="btn ghost sm"
            disabled={busy !== null}
            onClick={(event) => {
              event.stopPropagation();
              onDelete();
            }}
            style={{ color: "var(--short)" }}
            title="Elimina segnale"
          >
            <Icon name="x" size={11} />
          </button>
        </div>
      </div>
    </div>
  );
}
