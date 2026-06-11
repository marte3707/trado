"use client";

import { Newspaper, ShieldAlert } from "lucide-react";

import { fmt } from "@/components/atoms";
import { Card } from "@/components/ui/card";
import type { AppSettings } from "@/lib/api";
import { cn } from "@/lib/utils";

type Tone = "danger" | "warn" | "muted" | "success";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function asText(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function asTextList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(String).map((item) => item.trim()).filter(Boolean)
    : [];
}

function levelTone(level: string, active: boolean): Tone {
  if (!active || level === "off") return "success";
  if (level === "extreme") return "danger";
  if (level === "high" || level === "watch") return "warn";
  return "muted";
}

function toneClasses(tone: Tone) {
  return {
    danger: "border-danger/35 bg-danger/10 text-danger",
    warn: "border-warn/35 bg-warn/10 text-warn",
    muted: "border-border bg-bg-2 text-fg-2",
    success: "border-success/30 bg-success/10 text-success",
  }[tone];
}

function normalizeLabel(value: string | null): string {
  if (!value) return "n/a";
  return value.replaceAll("_", " ");
}

export function MacroRiskStateCard({ settings }: { settings: AppSettings }) {
  const state = asRecord(settings.macro_risk_state);
  const active = Boolean(state.active);
  const level = String(state.level ?? "off").toLowerCase();
  const tone = levelTone(level, active);
  const score = asNumber(state.combined_score) ?? 0;
  const newsScore = asNumber(state.news_score) ?? 0;
  const marketScore = asNumber(state.market_stress_score) ?? 0;
  const reason = asText(state.reason);
  const sources = asTextList(state.sources);
  const policy = asRecord(state.policy);
  const disabledTfs = asTextList(policy.disable_timeframes);
  const topNews = Array.isArray(state.top_news)
    ? state.top_news.map(asRecord).slice(0, 2)
    : [];

  return (
    <Card className="space-y-3 p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-fg-3">
          <ShieldAlert className="h-3.5 w-3.5" />
          Macro risk state
        </div>
        <span
          className={cn(
            "rounded-md border px-2 py-1 text-[10px] font-semibold uppercase tracking-wider",
            toneClasses(tone),
          )}
        >
          {active ? level : "off"}
        </span>
      </div>

      <div className="flex items-baseline gap-2">
        <span
          className={cn(
            "font-mono text-2xl font-semibold tabular-nums",
            tone === "danger" && "text-danger",
            tone === "warn" && "text-warn",
            tone === "success" && "text-success",
          )}
        >
          {score}
        </span>
        <span className="truncate text-xs text-fg-3">
          news {newsScore} · market {marketScore}
        </span>
      </div>

      <div className="rounded-md border border-border bg-bg-2/45 px-2 py-2">
        <div className="text-[9px] font-medium uppercase tracking-wider text-fg-4">Reason</div>
        <div className="mt-1 text-xs leading-snug text-fg-2">
          {normalizeLabel(reason)}
          {sources.length > 0 && (
            <span className="text-fg-4"> · sources: {sources.join(", ")}</span>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-1.5">
        <MiniMetric
          label="Size"
          value={`x${asNumber(policy.size_multiplier)?.toFixed(2) ?? "1.00"}`}
        />
        <MiniMetric label="Score add" value={`+${asNumber(policy.min_score_add) ?? 0}`} />
        <MiniMetric
          label="Disabled TF"
          value={disabledTfs.length ? disabledTfs.join(", ") : "none"}
        />
        <MiniMetric
          label="Until"
          value={state.active_until ? fmt.relTime(String(state.active_until)) : "n/a"}
        />
      </div>

      {topNews.length > 0 ? (
        <div className="space-y-1.5">
          {topNews.map((item, index) => (
            <div
              key={`${item.url ?? item.title ?? index}`}
              className="min-w-0 rounded-md border border-border bg-bg-2/35 px-2 py-1.5"
            >
              <div className="flex items-center gap-1.5 text-[9px] font-medium uppercase tracking-wider text-fg-4">
                <Newspaper className="h-3 w-3" />
                {String(item.source ?? "news")} · sev {String(item.severity ?? "—")}
              </div>
              {typeof item.url === "string" && item.url ? (
                <a
                  href={item.url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-0.5 block truncate text-[11px] text-fg-2 underline decoration-border underline-offset-2 hover:text-fg"
                  title={String(item.title ?? item.url)}
                >
                  {String(item.title ?? item.url)}
                </a>
              ) : (
                <div className="mt-0.5 truncate text-[11px] text-fg-2">
                  {String(item.title ?? "—")}
                </div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <p className="text-[10px] leading-snug text-fg-4">
          Nessuna headline rilevante nel lookback corrente.
        </p>
      )}
    </Card>
  );
}

function MiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-bg-2/45 px-2 py-1">
      <div className="text-[9px] font-medium uppercase tracking-wider text-fg-4">{label}</div>
      <div className="truncate font-mono text-[11px] tabular-nums text-fg-2">{value}</div>
    </div>
  );
}
