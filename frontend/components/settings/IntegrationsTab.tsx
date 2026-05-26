"use client";

import { ExternalLink } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import {
  useCoinGeckoUsage,
  useCoinMarketCapUsage,
} from "@/lib/hooks/queries";
import { cn } from "@/lib/utils";

function asNumber(v: unknown): number | null {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function flatten(
  obj: Record<string, unknown>,
  prefix = "",
): Array<{ path: string; value: unknown }> {
  const out: Array<{ path: string; value: unknown }> = [];
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (isPlainObject(v)) {
      out.push(...flatten(v, path));
    } else {
      out.push({ path, value: v });
    }
  }
  return out;
}

function valueLabel(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (Array.isArray(v)) return v.length === 0 ? "—" : `${v.length} items`;
  return String(v);
}

export function IntegrationsTab() {
  const { data: cmc } = useCoinMarketCapUsage();
  const { data: cg } = useCoinGeckoUsage();

  return (
    <div className="space-y-4">
      <Card className="p-6">
        <SectionHeader
          title="CoinMarketCap"
          enabled={cmc?.enabled}
          available={cmc?.available}
          checkedAt={cmc?.checked_at}
          source={cmc?.source}
        />
        {cmc ? (
          <div className="mt-4 space-y-3">
            <UsageRows usage={cmc.usage as Record<string, unknown>} plan={cmc.plan as Record<string, unknown>} />
            {cmc.disabled_reason && (
              <div className="rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-[11px] text-warn">
                {cmc.disabled_reason}{" "}
                {cmc.disabled_until && (
                  <span className="text-fg-3">· fino a {cmc.disabled_until}</span>
                )}
              </div>
            )}
          </div>
        ) : (
          <EmptyHint />
        )}
      </Card>

      <Card className="p-6">
        <SectionHeader
          title="CoinGecko"
          enabled={cg?.enabled}
          checkedAt={cg?.checked_at}
          source={cg?.source}
        />
        {cg ? (
          <div className="mt-4">
            <UsageRows usage={cg.data as Record<string, unknown>} plan={undefined} />
          </div>
        ) : (
          <EmptyHint />
        )}
      </Card>

      <Card className="p-5">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Telegram & Push
        </div>
        <p className="mt-1 text-xs text-fg-3">
          La configurazione canali notifica (Telegram, Web Push, Dashboard) si trova nella schermata{" "}
          <span className="inline-flex items-center gap-1 font-mono text-fg-2">
            <ExternalLink className="h-3 w-3" />
            Alert center
          </span>{" "}
          → pannello sinistro.
        </p>
      </Card>
    </div>
  );
}

function SectionHeader({
  title,
  enabled,
  available,
  checkedAt,
  source,
}: {
  title: string;
  enabled?: boolean;
  available?: boolean;
  checkedAt?: string | null;
  source?: string;
}) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <div className="text-base font-semibold text-fg">{title}</div>
        <div className="mt-0.5 text-xs text-fg-3">
          Source: <span className="font-mono">{source ?? "—"}</span>
          {checkedAt && (
            <span className="ml-2">· check {new Date(checkedAt).toLocaleString("it-IT")}</span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2">
        {enabled != null && (
          <Badge
            variant="outline"
            className={cn(
              "font-mono uppercase",
              enabled
                ? "border-success/40 bg-success/10 text-success"
                : "border-border bg-bg-2 text-fg-3",
            )}
          >
            {enabled ? "Enabled" : "Disabled"}
          </Badge>
        )}
        {available != null && (
          <Badge
            variant="outline"
            className={cn(
              "font-mono uppercase",
              available
                ? "border-success/40 bg-success/10 text-success"
                : "border-danger/40 bg-danger/10 text-danger",
            )}
          >
            {available ? "Available" : "Down"}
          </Badge>
        )}
      </div>
    </div>
  );
}

function UsageRows({
  usage,
  plan,
}: {
  usage: Record<string, unknown> | undefined;
  plan: Record<string, unknown> | undefined;
}) {
  if (!usage || Object.keys(usage).length === 0) {
    return <EmptyHint />;
  }
  // Flatten nested objects in path -> value pairs (es. CoinGecko data ha sub-keys nidificate).
  const entries = flatten(usage);
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      {entries.map(({ path, value }) => {
        const used = asNumber(value);
        const limit = asNumber(plan?.[`${path}_limit`] ?? plan?.[path]);
        const pct = used != null && limit != null && limit > 0 ? (used / limit) * 100 : null;
        return (
          <div key={path} className="rounded-md border border-border bg-bg-2/30 p-3">
            <div className="flex items-center justify-between gap-2">
              <span
                className="truncate font-mono text-xs uppercase text-fg-3"
                title={path}
              >
                {path}
              </span>
              <span className="shrink-0 font-mono text-sm text-fg">
                {used != null ? used.toLocaleString("en-US") : valueLabel(value)}
                {limit != null && <span className="text-fg-3">/{limit}</span>}
              </span>
            </div>
            {pct != null && (
              <Progress
                value={pct}
                className="mt-2"
                indicatorClassName={cn(
                  pct >= 90 && "bg-danger",
                  pct >= 70 && pct < 90 && "bg-warn",
                )}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

function EmptyHint() {
  return (
    <div className="mt-3 text-xs text-fg-3">Nessun dato disponibile per questa integrazione.</div>
  );
}
