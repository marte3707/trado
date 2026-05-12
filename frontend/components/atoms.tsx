"use client";

import React from "react";

/* ─── Icon ─────────────────────────────────────────────────────────────── */
const stroke = {
  fill: "none" as const,
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

const ICONS: Record<string, React.ReactNode> = {
  dash: <><rect x="3" y="3" width="7" height="7" rx="1" {...stroke}/><rect x="14" y="3" width="7" height="7" rx="1" {...stroke}/><rect x="3" y="14" width="7" height="7" rx="1" {...stroke}/><rect x="14" y="14" width="7" height="7" rx="1" {...stroke}/></>,
  signal: <><path d="M3 18l5-8 4 4 7-10" {...stroke}/><circle cx="20" cy="5" r="1.4" {...stroke}/></>,
  coin: <><circle cx="12" cy="12" r="9" {...stroke}/><path d="M9 9h5a2 2 0 0 1 0 4H9m0 0h6a2 2 0 0 1 0 4H9m0-8v10m3-10v10" {...stroke}/></>,
  settings: <><circle cx="12" cy="12" r="3" {...stroke}/><path d="M12 2v3m0 14v3m10-10h-3M5 12H2m15.5-7.5l-2.1 2.1M8.6 15.4l-2.1 2.1m0-13l2.1 2.1m6.8 6.8l2.1 2.1" {...stroke}/></>,
  history: <><path d="M3 12a9 9 0 1 0 3-6.7L3 8" {...stroke}/><path d="M3 3v5h5" {...stroke}/><path d="M12 7v5l3 2" {...stroke}/></>,
  bell: <><path d="M6 8a6 6 0 0 1 12 0c0 7 3 7 3 9H3c0-2 3-2 3-9" {...stroke}/><path d="M10 21a2 2 0 0 0 4 0" {...stroke}/></>,
  watch: <><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" {...stroke}/><circle cx="12" cy="12" r="3" {...stroke}/></>,
  search: <><circle cx="11" cy="11" r="7" {...stroke}/><path d="m20 20-3.5-3.5" {...stroke}/></>,
  power: <><path d="M12 3v9" {...stroke}/><path d="M5.6 7.6a8 8 0 1 0 12.8 0" {...stroke}/></>,
  chevron: <><path d="m9 6 6 6-6 6" {...stroke}/></>,
  chevron_left: <><path d="m15 6-6 6 6 6" {...stroke}/></>,
  chevron_down: <><path d="m6 9 6 6 6-6" {...stroke}/></>,
  plus: <><path d="M12 5v14M5 12h14" {...stroke}/></>,
  x: <><path d="m6 6 12 12M6 18 18 6" {...stroke}/></>,
  check: <><path d="m5 12 5 5L20 7" {...stroke}/></>,
  up: <><path d="m6 15 6-6 6 6" {...stroke}/></>,
  down: <><path d="m6 9 6 6 6-6" {...stroke}/></>,
  arrow_ur: <><path d="M7 17 17 7M9 7h8v8" {...stroke}/></>,
  arrow_r: <><path d="M5 12h14M13 6l6 6-6 6" {...stroke}/></>,
  info: <><circle cx="12" cy="12" r="9" {...stroke}/><path d="M12 8h.01M11 12h1v5h1" {...stroke}/></>,
  filter: <><path d="M3 5h18l-7 9v5l-4 2v-7L3 5z" {...stroke}/></>,
  bolt: <><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" {...stroke}/></>,
  sliders: <><path d="M4 7h10M18 7h2M4 17h2M10 17h10M4 12h6M14 12h6" {...stroke}/><circle cx="16" cy="7" r="2" {...stroke}/><circle cx="8" cy="17" r="2" {...stroke}/><circle cx="12" cy="12" r="2" {...stroke}/></>,
  tg: <><path d="M21 4 3 11l6 2 2 7 4-5 5 4 1-15z" {...stroke}/></>,
  browser: <><rect x="3" y="4" width="18" height="16" rx="2" {...stroke}/><path d="M3 9h18" {...stroke}/></>,
  wifi: <><path d="M5 12a10 10 0 0 1 14 0" {...stroke}/><path d="M8.5 15.5a5 5 0 0 1 7 0" {...stroke}/><circle cx="12" cy="19" r="1" fill="currentColor"/></>,
  refresh: <><path d="M3 12a9 9 0 0 1 15-6.7L21 8" {...stroke}/><path d="M21 3v5h-5" {...stroke}/><path d="M21 12a9 9 0 0 1-15 6.7L3 16" {...stroke}/><path d="M3 21v-5h5" {...stroke}/></>,
  dl: <><path d="M12 3v12m0 0-4-4m4 4 4-4M4 17v3h16v-3" {...stroke}/></>,
};

export const Icon = ({ name, size = 14 }: { name: string; size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: "block" }}>
    {ICONS[name] ?? null}
  </svg>
);

/* ─── Score pill ────────────────────────────────────────────────────────── */
export const ScoreChip = ({ score }: { score: number; dir?: string }) => {
  let bucket = "weak";
  if (score >= 90) bucket = "strong";
  else if (score >= 75) bucket = "good";
  else if (score >= 60) bucket = "ok";
  else if (score >= 40) bucket = "med";
  const cls = `score s-${bucket}`;
  return (
    <span className={cls}>
      <span className="score-bar"><i style={{ width: `${Math.max(0, Math.min(100, score))}%` }}/></span>
      <span>{score}</span>
    </span>
  );
};

/* ─── Direction badge ───────────────────────────────────────────────────── */
export function normalizeDir(dir: string): "LONG" | "SHORT" | "NEUTRAL" {
  if (dir.includes("LONG")) return "LONG";
  if (dir.includes("SHORT")) return "SHORT";
  return "NEUTRAL";
}

export const DirBadge = ({ dir }: { dir: string }) => {
  const norm = normalizeDir(dir);
  const label = dir.replace("_", " ");
  if (norm === "LONG")  return <span className="dir long mono">↑ {label}</span>;
  if (norm === "SHORT") return <span className="dir short mono">↓ {label}</span>;
  return <span className="dir neutral mono">— NEUTRAL</span>;
};

/* ─── TF chip ───────────────────────────────────────────────────────────── */
export const TfChip = ({ tf }: { tf: string }) => (
  <span className="tf-chip">{tf}</span>
);

/* ─── Market badge ──────────────────────────────────────────────────────── */
export const MarketBadge = ({ market }: { market: string }) => (
  <span className={"badge " + (market === "spot" ? "info" : "muted")}>
    {market === "spot" ? "SPOT" : "PERP"}
  </span>
);

/* ─── Segmented control ─────────────────────────────────────────────────── */
export const Seg = ({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
}) => (
  <div className="seg">
    {options.map(([v, label]) => (
      <button
        key={v}
        className={"seg-opt " + (value === v ? "active" : "")}
        onClick={() => onChange(v)}
      >
        {label}
      </button>
    ))}
  </div>
);

/* ─── Strategy badge list ───────────────────────────────────────────────── */
export const StratList = ({ strats, max = 3 }: { strats: string[]; max?: number }) => (
  <span style={{ display: "inline-flex", gap: 3, flexWrap: "wrap" as const, alignItems: "center" }}>
    {strats.slice(0, max).map((s) => (
      <span key={s} className="badge muted" style={{ fontSize: 9 }}>{s}</span>
    ))}
    {strats.length > max && (
      <span className="muted mono" style={{ fontSize: 10 }}>+{strats.length - max}</span>
    )}
  </span>
);

/* ─── Key-value display ─────────────────────────────────────────────────── */
export const Kv = ({ label, value, tone }: { label: string; value: string; tone?: string }) => {
  const color =
    tone === "long"  ? "var(--long)"  :
    tone === "short" ? "var(--short)" :
    tone === "muted" ? "var(--fg-3)"  : "var(--fg)";
  return (
    <div>
      <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>
        {label}
      </div>
      <div className="mono" style={{ fontSize: "var(--fz-md)", color }}>{value}</div>
    </div>
  );
};

/* ─── Section label ─────────────────────────────────────────────────────── */
export const SectionLabel = ({ children }: { children: React.ReactNode }) => (
  <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--fg-3)", margin: "4px 0 8px" }}>
    {children}
  </div>
);

/* ─── Formatting utilities ──────────────────────────────────────────────── */
const DEFAULT_CURRENCY = "EUR";

export const fmt = {
  price(p: number | string | null | undefined): string {
    if (p == null || p === "") return "—";
    const n = typeof p === "string" ? parseFloat(p) : p;
    if (!isFinite(n)) return "—";
    if (n >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
    if (n >= 1)    return n.toFixed(n >= 100 ? 2 : 3);
    if (n >= 0.001) return n.toFixed(4);
    return n.toPrecision(3);
  },
  pct(x: number | null | undefined, withSign = true): string {
    if (x == null || !isFinite(x)) return "—";
    return (x > 0 && withSign ? "+" : "") + x.toFixed(2) + "%";
  },
  money(x: number | null | undefined): string {
    if (x == null || !isFinite(x)) return "—";
    return x.toLocaleString("it-IT", { style: "currency", currency: DEFAULT_CURRENCY, minimumFractionDigits: 2, maximumFractionDigits: 2 });
  },
  pnl(x: number | null | undefined): string {
    if (x == null || !isFinite(x)) return "—";
    const formatted = Math.abs(x).toLocaleString("it-IT", { style: "currency", currency: DEFAULT_CURRENCY, minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (x >= 0 ? "+" : "-") + formatted;
  },
  relTime(iso: string | null): string {
    if (!iso) return "—";
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "ora";
    if (mins < 60) return `${mins}m fa`;
    const h = Math.floor(mins / 60);
    if (h < 24) return `${h}h fa`;
    return new Intl.DateTimeFormat("it-IT", { dateStyle: "short" }).format(new Date(iso));
  },
};

/* ─── Sparkline ─────────────────────────────────────────────────────────── */
export const Spark = ({
  data,
  w = 60,
  h = 18,
  color = "currentColor",
}: {
  data: number[];
  w?: number;
  h?: number;
  color?: string;
}) => {
  if (!data?.length) return null;
  const min = Math.min(...data), max = Math.max(...data);
  const r = max - min || 1;
  const pts = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - ((v - min) / r) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
};

export function sparkFor(sym: string, up = true): number[] {
  let seed = 0;
  for (let i = 0; i < sym.length; i++) seed = (seed * 31 + sym.charCodeAt(i)) & 0xffff;
  const out: number[] = [];
  let v = 50;
  for (let i = 0; i < 24; i++) {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    const drift = up ? 0.4 : -0.4;
    v += ((seed % 100) - 50) / 10 + drift;
    out.push(v);
  }
  return out;
}

/* ─── Status badge (signals) ────────────────────────────────────────────── */
export const StatusBadge = ({ status }: { status: string }) => {
  const map: Record<string, [string, string]> = {
    new:      ["long",  "new"],
    active:   ["long",  "active"],
    ignored:  ["muted", "ignored"],
    executed: ["info",  "executed"],
    closed:   ["muted", "closed"],
    invalidated: ["short", "invalid"],
  };
  const [cls, label] = map[status] ?? ["muted", status];
  return <span className={"badge " + cls}>{label}</span>;
};
