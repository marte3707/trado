"use client";

import { Activity, Globe, Lock, Unlock, Wifi, WifiOff } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useHealth } from "@/lib/hooks/queries";
import { cn } from "@/lib/utils";
import { StatusDot, type DotStatus } from "@/components/shell/StatusDot";

function workerStatus(secondsSince: number | null | undefined): {
  status: DotStatus;
  label: string;
} {
  if (secondsSince == null) return { status: "muted", label: "n/d" };
  if (secondsSince < 120) return { status: "ok", label: "attivo" };
  if (secondsSince < 600) return { status: "warn", label: "rallentato" };
  return { status: "danger", label: "fermo" };
}

function formatRel(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s fa`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m fa`;
  return `${Math.round(seconds / 3600)}h fa`;
}

export function TopbarStatus({ wsLive }: { wsLive: boolean }) {
  const { data: health } = useHealth();

  const worker = workerStatus(health?.worker?.seconds_since_last_run);
  const liveAllowed = Boolean(health?.live_trading_allowed);
  const network = health?.network ?? "n/d";
  const wsStatus: DotStatus = wsLive ? "ok" : "danger";

  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button
              className="inline-flex h-8 items-center gap-2 rounded-md border border-border bg-bg-1 px-2.5 text-fg-2 transition-colors hover:bg-bg-2"
              aria-label="Stato sistema"
            >
              <Globe className="h-3.5 w-3.5" />
              <span className="font-mono text-[10px] uppercase tracking-wide">{network}</span>
              <Separator orientation="vertical" className="h-3.5" />
              <StatusDot status={worker.status} />
              <StatusDot status={wsStatus} />
              {liveAllowed ? (
                <Unlock className="h-3.5 w-3.5 text-success" />
              ) : (
                <Lock className="h-3.5 w-3.5 text-fg-3" />
              )}
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent side="bottom">Stato sistema</TooltipContent>
      </Tooltip>

      <PopoverContent align="end" className="w-80 p-0">
        <div className="border-b border-border px-4 py-3">
          <div className="text-xs font-medium uppercase tracking-wide text-fg-3">Stato sistema</div>
          <div className="mt-1 text-sm text-fg">
            {health?.status === "ok" ? "Operativo" : "Degradato"}
          </div>
        </div>

        <div className="divide-y divide-border">
          <Row
            icon={<Globe className="h-4 w-4" />}
            title="Rete"
            value={network.toUpperCase()}
            sub={`Operation mode: ${health?.mode ?? "—"}`}
            status="info"
          />

          <Row
            icon={<Activity className="h-4 w-4" />}
            title="Worker"
            value={worker.label}
            sub={`Ultimo ciclo ${formatRel(health?.worker?.seconds_since_last_run)} · ${health?.worker?.phase ?? "idle"}`}
            status={worker.status}
            footer={
              health?.worker?.last_error_message ? (
                <div className="mt-1 text-xs text-danger">{health.worker.last_error_message}</div>
              ) : undefined
            }
          />

          <Row
            icon={wsLive ? <Wifi className="h-4 w-4" /> : <WifiOff className="h-4 w-4" />}
            title="WebSocket"
            value={wsLive ? "Live" : "Offline"}
            sub="Connessione Hyperliquid"
            status={wsStatus}
          />

          <Row
            icon={liveAllowed ? <Unlock className="h-4 w-4" /> : <Lock className="h-4 w-4" />}
            title="Live trading"
            value={liveAllowed ? "Sbloccato" : "Bloccato"}
            sub={
              health?.live_readiness?.checks?.length
                ? `${health.live_readiness.checks.length} check pendenti`
                : "Tutti i guardrail verdi"
            }
            status={liveAllowed ? "ok" : "warn"}
            footer={
              health?.live_readiness?.checks?.length ? (
                <ul className="mt-1.5 space-y-0.5 text-xs text-fg-3">
                  {health.live_readiness.checks.slice(0, 4).map((c) => (
                    <li key={c} className="flex items-start gap-1.5">
                      <span className="mt-1 inline-block h-1 w-1 shrink-0 rounded-full bg-warn" />
                      {c}
                    </li>
                  ))}
                </ul>
              ) : undefined
            }
          />
        </div>
      </PopoverContent>
    </Popover>
  );
}

function Row({
  icon,
  title,
  value,
  sub,
  status,
  footer,
}: {
  icon: React.ReactNode;
  title: string;
  value: string;
  sub?: string;
  status: DotStatus;
  footer?: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-3 px-4 py-3">
      <div className={cn("mt-0.5 text-fg-3")}>{icon}</div>
      <div className="flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-fg">{title}</span>
          <span className="ml-auto inline-flex items-center gap-1.5 font-mono text-xs text-fg-2">
            <StatusDot status={status} size={6} />
            {value}
          </span>
        </div>
        {sub && <div className="mt-0.5 text-xs text-fg-3">{sub}</div>}
        {footer}
      </div>
    </div>
  );
}
