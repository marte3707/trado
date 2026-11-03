"use client";

import { Activity, BarChart3, Bitcoin, Clock3, Flame } from "lucide-react";

import { Card } from "@/components/ui/card";
import { fmt } from "@/components/atoms";
import { cn } from "@/lib/utils";
import type { AppSettings, MarketIntelligence } from "@/lib/api";

function fearGreedTone(value: number | null | undefined): "danger" | "warn" | "muted" | "success" {
  if (value == null) return "muted";
  if (value < 25) return "danger";
  if (value < 45) return "warn";
  if (value < 65) return "muted";
  return "success";
}

function compactUsd(value: number | null | undefined): string {
  if (value == null) return "—";
  if (Math.abs(value) >= 1e12) return `$${(value / 1e12).toFixed(2)}T`;
  if (Math.abs(value) >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (Math.abs(value) >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
  return `$${value.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

function asNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function asBool(value: unknown): boolean {
  return Boolean(value);
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function normalizeTrend(value: string | null): string {
  if (!value) return "—";
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
    .join(" ");
}

function trendTone(value: string | null, enabled: boolean): Tone {
  if (!enabled || !value) return "muted";
  const normalized = value.toUpperCase();
  if (normalized.includes("BEAR") || normalized.includes("DOWN")) return "danger";
  if (normalized.includes("BULL") || normalized.includes("UP")) return "success";
  if (normalized.includes("REVERS") || normalized.includes("RISK")) return "warn";
  return "muted";
}

function readBtcMacro(settings: AppSettings | null | undefined) {
  const btc = (settings?.btc_macro_protection ?? {}) as Record<string, unknown>;
  return {
    enabled: asBool(btc.enabled),
    timeframe: asString(btc.timeframe) ?? "—",
    trend: asString(btc._last_btc_trend),
    directionalTrend: asString(btc._last_directional_trend),
    deltaPct: asNumber(btc._last_delta_pct),
    volRatio: asNumber(btc._last_vol_ratio),
    checkedAt: asString(btc._last_checked_at),
  };
}

export function MarketIntelligenceStrip({
  data,
  settings,
}: {
  data: MarketIntelligence | null | undefined;
  settings?: AppSettings | null;
}) {
  const fg = data?.fear_greed?.value ?? null;
  const fgClass = data?.fear_greed?.classification ?? "—";
  const fgTone = fearGreedTone(fg);

  const updatedAt = data?.global_metrics?.last_updated ?? null;
  const staleText = `Agg. ${fmt.relTime(updatedAt)}`;
  const altIndex = data?.altcoin_season?.altcoin_index ?? null;
  const btcDom = data?.global_metrics?.btc_dominance_pct ?? null;
  const mcap = data?.global_metrics?.total_market_cap_usd ?? null;
  const mcapDelta = data?.global_metrics?.market_cap_change_24h_pct ?? null;
  const btcPrice = data?.global_metrics?.btc_price_usd ?? null;
  const btcChange = data?.global_metrics?.btc_price_change_24h_pct ?? null;
  const btcMacro = readBtcMacro(settings);
  const btcMacroTone = trendTone(btcMacro.trend, btcMacro.enabled);

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-fg">Market snapshot</h2>
          <p className="mt-0.5 text-xs text-fg-3">BTC, breadth e liquidita globali</p>
        </div>
        <div className="flex items-center gap-1.5 rounded-md border border-border bg-bg-2 px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-fg-3">
          <Clock3 className="h-3 w-3" />
          {staleText}
        </div>
      </div>
      <Card className="grid grid-cols-1 divide-y divide-border md:grid-cols-4 md:divide-x md:divide-y-0">
        <Cell
          icon={<Flame className="h-3.5 w-3.5" />}
          label="Fear & Greed"
          value={fg != null ? String(fg) : "—"}
          sub={fgClass}
          tone={fgTone}
          gauge={fg != null ? { value: fg, max: 100, tone: fgTone } : undefined}
        />
        <Cell
          icon={<Activity className="h-3.5 w-3.5" />}
          label="Altseason Index"
          value={altIndex != null ? String(Math.round(altIndex)) : "—"}
          sub={altIndex != null ? (altIndex > 75 ? "Altseason" : altIndex > 50 ? "Mixed" : "Bitcoin season") : "—"}
          gauge={altIndex != null ? { value: altIndex, max: 100, tone: "muted" } : undefined}
        />
        <Cell
          icon={<Bitcoin className="h-3.5 w-3.5" />}
          label="BTC"
          value={btcPrice ? `$${btcPrice.toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "—"}
          sub={`Dom. ${btcDom != null ? btcDom.toFixed(1) + "%" : "—"} · Macro ${btcMacro.timeframe}`}
          delta={btcChange != null ? { value: fmt.pct(btcChange), tone: btcChange >= 0 ? "up" : "down" } : null}
          details={
            <div className="mt-1.5 space-y-2">
              <div className="grid grid-cols-2 gap-1.5">
                <MiniMetric
                  label="Macro"
                  value={btcMacro.enabled ? normalizeTrend(btcMacro.trend) : "Off"}
                  tone={btcMacroTone}
                />
                <MiniMetric
                  label="Vol"
                  value={btcMacro.volRatio != null ? `${btcMacro.volRatio.toFixed(2)}x` : "—"}
                  tone={btcMacro.volRatio != null && btcMacro.volRatio >= 1 ? "warn" : "muted"}
                />
              </div>
              <div className="truncate text-[10px] text-fg-4">
                Macro check {fmt.relTime(btcMacro.checkedAt)}
              </div>
            </div>
          }
        />
        <Cell
          icon={<BarChart3 className="h-3.5 w-3.5" />}
          label="Market cap"
          value={compactUsd(mcap)}
          sub={`Vol 24h ${compactUsd(data?.global_metrics?.total_volume_24h_usd)}`}
          delta={mcapDelta != null ? { value: fmt.pct(mcapDelta), tone: mcapDelta >= 0 ? "up" : "down" } : null}
        />
      </Card>
    </section>
  );
}

type Tone = "danger" | "warn" | "muted" | "success";

function Cell({
  icon,
  label,
  value,
  sub,
  tone = "muted",
  gauge,
  delta,
  details,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: string;
  tone?: Tone;
  gauge?: { value: number; max: number; tone: Tone };
  delta?: { value: string; tone: "up" | "down" } | null;
  details?: React.ReactNode;
}) {
  const toneClass: Record<Tone, string> = {
    danger: "text-danger",
    warn: "text-warn",
    muted: "text-fg",
    success: "text-success",
  };
  return (
    <div className="flex flex-col gap-1.5 p-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-fg-3">
          {icon}
          {label}
        </div>
        {delta && (
          <span
            className={cn(
              "font-mono text-[10px]",
              delta.tone === "up" ? "text-success" : "text-danger",
            )}
          >
            {delta.value}
          </span>
        )}
      </div>
      <div className="flex items-baseline gap-2">
        <span className={cn("font-mono text-xl font-semibold tabular-nums", toneClass[tone])}>
          {value}
        </span>
        {sub && <span className="truncate text-xs text-fg-3">{sub}</span>}
      </div>
      {gauge && (
        <div className="h-1 w-full overflow-hidden rounded-full bg-bg-3">
          <div
            className={cn(
              "h-full transition-[width] duration-300",
              gauge.tone === "danger" && "bg-danger",
              gauge.tone === "warn" && "bg-warn",
              gauge.tone === "success" && "bg-success",
              gauge.tone === "muted" && "bg-fg-3",
            )}
            style={{ width: `${Math.max(0, Math.min(100, (gauge.value / gauge.max) * 100))}%` }}
          />
        </div>
      )}
      {details}
    </div>
  );
}

function MiniMetric({
  label,
  value,
  tone = "muted",
}: {
  label: string;
  value: string;
  tone?: Tone;
}) {
  const toneClass: Record<Tone, string> = {
    danger: "text-danger",
    warn: "text-warn",
    muted: "text-fg-2",
    success: "text-success",
  };

  return (
    <div className="min-w-0 rounded-md border border-border bg-bg-2/45 px-2 py-1">
      <div className="text-[9px] font-medium uppercase tracking-wider text-fg-4">{label}</div>
      <div className={cn("truncate font-mono text-[11px] tabular-nums", toneClass[tone])}>
        {value}
      </div>
    </div>
  );
}
