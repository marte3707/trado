"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Icon, ScoreChip, DirBadge, TfChip, fmt } from "@/components/atoms";
import { AppSettings, DecisionLog, Market, apiPatch } from "@/lib/api";
import { isPushSupported, subscribePush, unsubscribePush } from "@/lib/push";

interface Props {
  logs: DecisionLog[];
  markets: Market[];
  settings: AppSettings;
  onRefresh: () => void;
}

type AlertLevel = "strong" | "good" | "info" | "warn" | "risk";
type FeedFilter = "all" | "strong" | "risk";

interface AlertItem {
  ts: string;
  level: AlertLevel;
  sym?: string;
  tf?: string;
  dir?: string;
  score?: number;
  msg: string;
  detail?: string;
  channels: string[];
}

const MAX_FEED_ALERTS = 100;
const ALERTS_PAGE_SIZE = 20;

const EVENT_LABELS: Record<string, string> = {
  live_order_rejected:                   "Ordine live rifiutato",
  live_order_rejected_by_exchange:       "Ordine live rifiutato dall'exchange",
  live_order_submitted:                  "Ordine live inviato",
  live_protection_orders_submitted:      "Ordini di protezione inviati",
  live_protection_orders_partial_failure:"Protezioni parzialmente fallite",
  paper_order_executed:                  "Posizione paper aperta",
  paper_order_skipped_duplicate_ticker:  "Paper saltato (duplicato)",
  paper_position_auto_closed:            "Posizione paper chiusa (auto)",
  paper_position_partial_closed:         "Posizione paper chiusa parzialmente",
  position_closed:                       "Posizione chiusa",
  position_partially_closed:             "Posizione chiusa parzialmente",
  live_close_rejected_by_exchange:       "Chiusura live rifiutata",
};

function fmtPrice(v: unknown): string {
  const n = Number(v);
  return isNaN(n) ? String(v) : `$${n.toLocaleString("en-US", { maximumFractionDigits: 4 })}`;
}

function fmtPnl(v: unknown): string {
  const n = Number(v);
  return isNaN(n) ? String(v) : `${n >= 0 ? "+" : ""}${n.toFixed(2)} USDC`;
}

function extractDetail(event_type: string, data: Record<string, unknown>): string | undefined {
  switch (event_type) {
    case "live_order_rejected": {
      const reasons = data.reasons as string[] | undefined;
      return reasons?.length ? reasons.map(r => `• ${r}`).join("  ") : undefined;
    }
    case "live_order_rejected_by_exchange": {
      const resp = data.response as Record<string, unknown> | undefined;
      if (resp?.error) return String(resp.error);
      return undefined;
    }
    case "live_order_submitted": {
      const side = data.side as string | undefined;
      const size = data.size as string | undefined;
      const price = data.entry_price as string | undefined;
      if (!side || !size) return undefined;
      return [side.toUpperCase(), size, price ? `@ ${fmtPrice(price)}` : undefined].filter(Boolean).join(" ");
    }
    case "paper_order_executed": {
      const side = data.side as string | undefined;
      const size = data.size as string | undefined;
      if (!side || !size) return undefined;
      return `${side.toUpperCase()} · size ${size}`;
    }
    case "position_closed":
    case "paper_position_auto_closed": {
      const parts: string[] = [];
      if (data.exit_price) parts.push(`@ ${fmtPrice(data.exit_price)}`);
      if (data.realized_pnl) parts.push(`PnL ${fmtPnl(data.realized_pnl)}`);
      if (data.last_exit_reason) parts.push(String(data.last_exit_reason));
      return parts.length ? parts.join(" · ") : undefined;
    }
    case "position_partially_closed":
    case "paper_position_partial_closed": {
      const parts: string[] = [];
      if (data.exit_price) parts.push(`@ ${fmtPrice(data.exit_price)}`);
      if (data.realized_pnl) parts.push(`PnL parziale ${fmtPnl(data.realized_pnl)}`);
      if (data.remaining_size) parts.push(`rimanente ${data.remaining_size}`);
      return parts.length ? parts.join(" · ") : undefined;
    }
    case "live_protection_orders_partial_failure": {
      const errors = data.errors as string[] | undefined;
      return errors?.length ? errors.join("; ") : undefined;
    }
  }
  return undefined;
}

function logsToAlerts(logs: DecisionLog[], marketIndex: Map<number, string>): AlertItem[] {
  return logs.slice(0, MAX_FEED_ALERTS).map(log => {
    const t = log.event_type.toLowerCase();
    const score = Number(log.data.score);
    let level: AlertLevel = "info";
    if (t.includes("error") || t.includes("fail") || t === "live_order_rejected") level = "risk";
    else if (t.includes("strong") || score >= 90) level = "strong";
    else if (t.includes("signal") || t === "live_order_submitted" || t === "paper_order_executed") level = "good";
    else if (t.includes("warn") || t.includes("kill") || t.includes("partial")) level = "warn";
    return {
      ts: fmt.relTime(log.timestamp),
      level,
      sym: log.market_id != null ? marketIndex.get(log.market_id) : undefined,
      tf: typeof log.data.timeframe === "string" ? log.data.timeframe : undefined,
      dir: typeof log.data.side === "string" ? log.data.side : typeof log.data.direction === "string" ? log.data.direction : undefined,
      score: Number.isFinite(score) ? score : undefined,
      msg: EVENT_LABELS[log.event_type] ?? log.message,
      detail: extractDetail(log.event_type, log.data),
      channels: ["dash"],
    };
  });
}

export function AlertsScreen({ logs, markets, settings, onRefresh }: Props) {
  const marketIndex = useMemo(() => new Map(markets.map(m => [m.id, m.base])), [markets]);
  const alertSettings = settings.alerts ?? {};
  const [tgEnabled, setTgEnabled] = useState(Boolean(alertSettings.telegram_enabled));
  const [pushEnabled, setPushEnabled] = useState(Boolean(alertSettings.browser_push_enabled));
  const [dashEnabled, setDashEnabled] = useState(Boolean(alertSettings.dashboard_enabled));
  const [minScore, setMinScore] = useState(Number(alertSettings.min_score ?? settings.risk?.min_signal_score ?? 0));
  const [onlyStrong, setOnlyStrong] = useState(Boolean(alertSettings.only_strong));
  const [stateAlerts, setStateAlerts] = useState(Boolean(alertSettings.position_state_alerts));
  const [riskWarn, setRiskWarn] = useState(Boolean(alertSettings.risk_warnings));
  const [muteDuringAuto, setMuteDuringAuto] = useState(Boolean(alertSettings.mute_during_auto_trade));
  const [quietFrom, setQuietFrom] = useState(String(alertSettings.quiet_from ?? ""));
  const [quietTo, setQuietTo] = useState(String(alertSettings.quiet_to ?? ""));
  const [feedFilter, setFeedFilter] = useState<FeedFilter>("all");
  const [search, setSearch] = useState("");
  const [feedPage, setFeedPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [pushBusy, setPushBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pushSupported = isPushSupported();

  async function togglePush() {
    if (!pushSupported) return;
    setPushBusy(true);
    try {
      if (!pushEnabled) {
        const ok = await subscribePush();
        if (ok) setPushEnabled(true);
        else setError("Permesso notifiche negato o browser non supportato");
      } else {
        await unsubscribePush();
        setPushEnabled(false);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Errore push");
    } finally {
      setPushBusy(false);
    }
  }

  useEffect(() => {
    const next = settings.alerts ?? {};
    setTgEnabled(Boolean(next.telegram_enabled));
    setPushEnabled(Boolean(next.browser_push_enabled));
    setDashEnabled(Boolean(next.dashboard_enabled));
    setMinScore(Number(next.min_score ?? settings.risk?.min_signal_score ?? 0));
    setOnlyStrong(Boolean(next.only_strong));
    setStateAlerts(Boolean(next.position_state_alerts));
    setRiskWarn(Boolean(next.risk_warnings));
    setMuteDuringAuto(Boolean(next.mute_during_auto_trade));
    setQuietFrom(String(next.quiet_from ?? ""));
    setQuietTo(String(next.quiet_to ?? ""));
  }, [settings]);

  async function saveAlerts() {
    setBusy(true);
    setError(null);
    try {
      await apiPatch("/api/settings", {
        alerts: {
          value: {
            ...alertSettings,
            dashboard_enabled: dashEnabled,
            telegram_enabled: tgEnabled,
            browser_push_enabled: pushEnabled,
            min_score: minScore,
            only_strong: onlyStrong,
            position_state_alerts: stateAlerts,
            risk_warnings: riskWarn,
            mute_during_auto_trade: muteDuringAuto,
            quiet_from: quietFrom || null,
            quiet_to: quietTo || null,
          },
        },
      });
      onRefresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Errore salvataggio alert");
    } finally {
      setBusy(false);
    }
  }

  const alerts = useMemo(() => logsToAlerts(logs, marketIndex), [logs, marketIndex]);
  const searchTokens = useMemo(() => search.trim().toLowerCase().split(/\s+/).filter(Boolean), [search]);
  const filtered = useMemo(() => {
    const base: AlertItem[] = feedFilter === "all" ? alerts :
      feedFilter === "strong" ? alerts.filter(a => a.level === "strong" || a.level === "good") :
      alerts.filter(a => a.level === "risk" || a.level === "warn");

    if (searchTokens.length === 0) return base;

    return base.filter((alert) => {
      const haystack = [
        alert.sym,
        alert.tf,
        alert.dir,
        alert.level,
        alert.msg,
        alert.detail,
        alert.score != null ? String(alert.score) : null,
      ].filter(Boolean).join(" ").toLowerCase();
      return searchTokens.every(token => haystack.includes(token));
    });
  }, [alerts, feedFilter, searchTokens]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / ALERTS_PAGE_SIZE));
  const safePage = Math.min(feedPage, pageCount);
  const pagedAlerts = filtered.slice((safePage - 1) * ALERTS_PAGE_SIZE, safePage * ALERTS_PAGE_SIZE);

  useEffect(() => {
    setFeedPage(1);
  }, [feedFilter, search]);

  useEffect(() => {
    if (feedPage > pageCount) setFeedPage(pageCount);
  }, [feedPage, pageCount]);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Alert</h1>
          <div className="page-sub">Configura canali e ricevi notifiche immediate sui segnali rilevanti</div>
        </div>
        <div className="page-actions">
          <button className="btn primary" disabled={busy} onClick={saveAlerts}>Salva</button>
        </div>
      </div>

      {error && (
        <div style={{ padding: "8px 14px", marginBottom: "var(--gap)", background: "var(--short-soft)", border: "1px solid var(--short-border)", borderRadius: "var(--radius)", color: "var(--short)", fontSize: "var(--fz-sm)" }}>
          {error}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: "var(--gap)" }}>
        {/* Channels + rules */}
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap)" }}>

          {/* Canali */}
          <div className="card">
            <div className="card-head"><span className="title">Canali</span></div>
            <ChannelRow
              icon="dash" name="Dashboard" desc="Feed in-app + notifica visiva"
              enabled={dashEnabled} onToggle={() => setDashEnabled(!dashEnabled)}
              status="attivo"
            />
            <ChannelRow
              icon="tg" name="Telegram" desc="Configurato dal backend"
              enabled={tgEnabled} onToggle={() => setTgEnabled(!tgEnabled)}
              status={tgEnabled ? "abilitato" : "non configurato"}
            />
            <ChannelRow
              icon="browser" name="Browser push" desc="Web Push API"
              enabled={pushEnabled} onToggle={togglePush}
              status={!pushSupported ? "browser non supportato" : pushBusy ? "..." : pushEnabled ? "permesso concesso" : "non attivato"}
              last
            />
          </div>

          {/* Regole */}
          <div className="card">
            <div className="card-head"><span className="title">Regole</span></div>
            <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div>
                <div style={{ display: "flex", alignItems: "baseline", marginBottom: 6, gap: 6 }}>
                  <span style={{ flex: 1, fontSize: "var(--fz-sm)" }}>Score minimo per alert</span>
                  <span className="mono" style={{ fontSize: "var(--fz-md)" }}>{minScore}</span>
                </div>
                <input
                  type="range" min={40} max={100} step={1} value={minScore}
                  onChange={e => setMinScore(+e.target.value)}
                  style={{ width: "100%", accentColor: "var(--accent)" }}
                />
                <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>solo segnali ≥ soglia</div>
              </div>
              <RuleToggle label="Solo STRONG (score ≥ 90)" on={onlyStrong} onChange={setOnlyStrong} />
              <RuleToggle label="Alert per cambi di stato posizione" on={stateAlerts} onChange={setStateAlerts} />
              <RuleToggle label="Risk warning (DD, exposure)" on={riskWarn} onChange={setRiskWarn} />
              <RuleToggle label="Mute durante AUTO_TRADE" on={muteDuringAuto} onChange={setMuteDuringAuto} />
            </div>
          </div>

          {/* Quiet hours */}
          <div className="card">
            <div className="card-head"><span className="title">Quiet hours</span></div>
            <div className="card-body" style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <div style={{ flex: 1 }}>
                <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Da</div>
                <input
                  type="time" value={quietFrom} onChange={e => setQuietFrom(e.target.value)}
                  className="filter"
                  style={{ width: "100%", height: 32, paddingLeft: 10, fontSize: "var(--fz-sm)" }}
                />
              </div>
              <span className="muted" style={{ marginTop: 14 }}>→</span>
              <div style={{ flex: 1 }}>
                <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>A</div>
                <input
                  type="time" value={quietTo} onChange={e => setQuietTo(e.target.value)}
                  className="filter"
                  style={{ width: "100%", height: 32, paddingLeft: 10, fontSize: "var(--fz-sm)" }}
                />
              </div>
            </div>
          </div>
        </div>

        {/* Feed */}
        <div className="card">
          <div className="card-head">
            <span className="title">Feed alert</span>
            <span className="badge muted">{filtered.length}/{alerts.length} recenti</span>
            <div className="right">
              <div style={{ position: "relative", display: "inline-flex", alignItems: "center" }}>
                <span style={{ position: "absolute", left: 9, color: "var(--fg-3)", pointerEvents: "none" }}>
                  <Icon name="search" size={12} />
                </span>
                <input
                  type="text"
                  value={search}
                  onChange={e => setSearch(e.target.value)}
                  placeholder="Cerca alert..."
                  className="filter"
                  style={{ width: 190, paddingLeft: 28 }}
                />
              </div>
              <div className="seg">
                {(["all","strong","risk"] as FeedFilter[]).map(v => (
                  <button key={v} className={"seg-opt " + (feedFilter === v ? "active" : "")} onClick={() => setFeedFilter(v)}>
                    {v === "all" ? "Tutti" : v === "strong" ? "Strong" : "Risk"}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div>
            {filtered.length === 0 && (
              <div style={{ padding: "32px", textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
                Nessun alert nel feed
              </div>
            )}
            {pagedAlerts.map((a, i) => (
              <AlertRow key={`${a.ts}-${a.msg}-${i}`} a={a} isLast={i === pagedAlerts.length - 1} />
            ))}
          </div>
          {filtered.length > ALERTS_PAGE_SIZE && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, padding: "10px 18px", borderTop: "1px solid var(--border)" }}>
              <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>
                {(safePage - 1) * ALERTS_PAGE_SIZE + 1}–{Math.min(filtered.length, safePage * ALERTS_PAGE_SIZE)} di {filtered.length}
              </span>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <button className="btn ghost sm" onClick={() => setFeedPage(p => Math.max(1, p - 1))} disabled={safePage <= 1} title="Precedente">
                  <Icon name="chevron_left" size={11} />
                </button>
                <span className="muted mono" style={{ minWidth: 44, textAlign: "center", fontSize: "var(--fz-xs)" }}>
                  {safePage}/{pageCount}
                </span>
                <button className="btn ghost sm" onClick={() => setFeedPage(p => Math.min(pageCount, p + 1))} disabled={safePage >= pageCount} title="Successiva">
                  <Icon name="chevron" size={11} />
                </button>
              </div>
            </div>
          )}
          <div style={{ padding: "10px 18px", borderTop: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 8, fontSize: "var(--fz-sm)", color: "var(--fg-3)" }}>
            <span className={logs.length > 0 ? "dot green" : "dot red"} />
            <span>Feed da log di sistema · ultimi {Math.min(logs.length, MAX_FEED_ALERTS)} di {logs.length} eventi</span>
            <span style={{ marginLeft: "auto" }} className="mono">aggiornato ora</span>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Channel row ──────────────────────────────────────────────────────────── */
function ChannelRow({ icon, name, desc, enabled, onToggle, status, last }: {
  icon: string; name: string; desc: string; enabled: boolean;
  onToggle: () => void; status: string; last?: boolean;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "14px 18px", borderBottom: last ? "none" : "1px solid var(--border)" }}>
      <div style={{ width: 32, height: 32, borderRadius: "var(--radius-sm)", background: "var(--bg-2)", display: "grid", placeItems: "center", color: "var(--fg-2)" }}>
        <Icon name={icon} size={16} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: "var(--fz-md)", fontWeight: 500 }}>{name}</div>
        <div className="muted" style={{ fontSize: 10, marginTop: 1 }}>{desc}</div>
        {enabled && <div className="up mono" style={{ fontSize: 10, marginTop: 4 }}>● {status}</div>}
        {!enabled && <div className="muted mono" style={{ fontSize: 10, marginTop: 4 }}>○ disattivato</div>}
      </div>
      <div className={"toggle " + (enabled ? "on" : "")} onClick={onToggle} />
    </div>
  );
}

/* ── Rule toggle ──────────────────────────────────────────────────────────── */
function RuleToggle({ label, on, onChange }: { label: string; on: boolean; onChange: (v: boolean) => void }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <span style={{ flex: 1, fontSize: "var(--fz-sm)", color: on ? "var(--fg)" : "var(--fg-2)" }}>{label}</span>
      <div className={"toggle " + (on ? "on" : "")} onClick={() => onChange(!on)} />
    </div>
  );
}

/* ── Alert row ────────────────────────────────────────────────────────────── */
function AlertRow({ a, isLast }: { a: AlertItem; isLast: boolean }) {
  const levelMap: Record<AlertLevel, { color: string; label: string; icon: string }> = {
    strong: { color: "var(--long)",  label: "STRONG", icon: "bolt"   },
    good:   { color: "var(--long)",  label: "GOOD",   icon: "signal" },
    info:   { color: "var(--info)",  label: "INFO",   icon: "info"   },
    warn:   { color: "var(--warn)",  label: "WARN",   icon: "info"   },
    risk:   { color: "var(--short)", label: "RISK",   icon: "power"  },
  };
  const lvl = levelMap[a.level];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "60px 110px 1fr auto", gap: 14, alignItems: "center", padding: "12px 18px", borderBottom: isLast ? "none" : "1px solid var(--border)" }}>
      <span className="muted mono" style={{ fontSize: "var(--fz-sm)" }}>{a.ts}</span>
      <span className="badge" style={{ color: lvl.color, borderColor: `${lvl.color}33`, background: `${lvl.color}14`, display: "flex", alignItems: "center", gap: 5 }}>
        <Icon name={lvl.icon} size={10} /> {lvl.label}
      </span>
      <div>
        <div style={{ fontSize: "var(--fz-sm)", display: "flex", alignItems: "center", gap: 6, marginBottom: (a.detail || a.msg) ? 2 : 0 }}>
          {a.sym && <strong>{a.sym}</strong>}
          {a.tf && <TfChip tf={a.tf} />}
          {a.dir && <DirBadge dir={a.dir} />}
          {a.score != null && <ScoreChip score={a.score} dir={a.dir ?? "NEUTRAL"} />}
          <span style={{ fontWeight: 500, color: "var(--fg-2)" }}>{a.msg}</span>
        </div>
        {a.detail && (
          <div className="muted mono" style={{ fontSize: 11, lineHeight: 1.5, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{a.detail}</div>
        )}
      </div>
      <div style={{ display: "flex", gap: 4 }}>
        {a.channels.includes("dash") && <span style={{ color: "var(--fg-3)" }} title="Dashboard"><Icon name="dash" size={11} /></span>}
        {a.channels.includes("tg") && <span style={{ color: "var(--fg-3)" }} title="Telegram"><Icon name="tg" size={11} /></span>}
        {a.channels.includes("push") && <span style={{ color: "var(--fg-3)" }} title="Push"><Icon name="browser" size={11} /></span>}
      </div>
    </div>
  );
}
