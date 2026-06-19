"use client";

import { Activity, Play, RefreshCw, Square } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StatusDot, type DotStatus } from "@/components/shell/StatusDot";
import { useServiceAction } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";
import type { Health, SystemService, SystemServiceAction } from "@/lib/api";

interface Props {
  health: Health;
}

interface ServiceMeta {
  id: SystemService;
  label: string;
  description: string;
  status: DotStatus;
  statusLabel: string;
  detail: string;
}

function workerMeta(health: Health): ServiceMeta {
  const sec = health.worker?.seconds_since_last_run;
  const phase = health.worker?.phase ?? "idle";
  let status: DotStatus = "muted";
  let statusLabel = "n/d";
  if (sec != null) {
    if (sec < 120) {
      status = "ok";
      statusLabel = "attivo";
    } else if (sec < 600) {
      status = "warn";
      statusLabel = "rallentato";
    } else {
      status = "danger";
      statusLabel = "fermo";
    }
  }
  return {
    id: "worker",
    label: "Worker",
    description: "Loop async che genera segnali, gestisce posizioni e calibration",
    status,
    statusLabel,
    detail: `Phase: ${phase} · ultimo ciclo ${sec != null ? Math.round(sec) + "s fa" : "—"}`,
  };
}

function backendMeta(health: Health): ServiceMeta {
  const ok = health.status === "ok";
  return {
    id: "backend",
    label: "Backend API",
    description: "FastAPI server che espone REST per frontend e mobile",
    status: ok ? "ok" : "danger",
    statusLabel: ok ? "operativo" : "degradato",
    detail: `Mode: ${health.mode} · network: ${health.network}`,
  };
}

function botMeta(health: Health): ServiceMeta {
  const bot = health.bot;
  let status: DotStatus = "muted";
  let statusLabel = "n/d";
  if (bot) {
    status = bot.running ? "ok" : "danger";
    statusLabel = bot.running ? "attivo" : "fermo";
  }
  return {
    id: "bot",
    label: "Bot Telegram",
    description: "Bot per notifiche segnali, heartbeat e onboarding utenti",
    status,
    statusLabel,
    detail: bot?.running
      ? `PID: ${bot.pid ?? "—"} · ${bot.label}`
      : `Servizio non in esecuzione${bot ? ` · ${bot.label}` : ""}`,
  };
}

function marketDataMeta(health: Health): ServiceMeta {
  const marketData = health.market_data;
  let status: DotStatus = "muted";
  let statusLabel = "n/d";
  if (marketData) {
    status = marketData.healthy ? "ok" : marketData.running ? "warn" : "danger";
    statusLabel = marketData.healthy ? "streaming" : marketData.running ? "degradato" : "fermo";
  }
  const active = marketData?.active_subscriptions ?? 0;
  const expected = marketData?.expected_subscriptions ?? 0;
  const age = marketData?.seconds_since_last_message;
  return {
    id: "marketdata",
    label: "Market Data WS",
    description: "Candele Hyperliquid realtime con coda DB e fallback REST",
    status,
    statusLabel,
    detail: marketData
      ? `${active}/${expected} subscription · ultimo messaggio ${age != null ? Math.round(age) + "s fa" : "—"} · eventi ${marketData.pending_events ?? 0}`
      : "Stato websocket non disponibile",
  };
}

export function ServicesTab({ health }: Props) {
  const services: ServiceMeta[] = [
    backendMeta(health),
    marketDataMeta(health),
    workerMeta(health),
    botMeta(health),
  ];
  const { mutate: action, isPending } = useServiceAction();

  return (
    <div className="space-y-4">
      {services.map((svc) => (
        <Card key={svc.id} className="p-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex items-start gap-3">
              <StatusDot status={svc.status} size={10} />
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-base font-semibold text-fg">{svc.label}</span>
                  <Badge variant="outline" className={cn("font-mono uppercase", toneClass(svc.status))}>
                    {svc.statusLabel}
                  </Badge>
                </div>
                <div className="mt-0.5 text-xs text-fg-3">{svc.description}</div>
                <div className="mt-1.5 font-mono text-[11px] text-fg-3">{svc.detail}</div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={isPending}
                onClick={() => action({ service: svc.id, action: "start" as SystemServiceAction })}
              >
                <Play className="mr-1.5 h-3.5 w-3.5" />
                Start
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={isPending}
                onClick={() => action({ service: svc.id, action: "stop" as SystemServiceAction })}
              >
                <Square className="mr-1.5 h-3.5 w-3.5" />
                Stop
              </Button>
              <Button
                size="sm"
                disabled={isPending}
                onClick={() => action({ service: svc.id, action: "reboot" as SystemServiceAction })}
              >
                <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
                Reboot
              </Button>
            </div>
          </div>
        </Card>
      ))}

      {health.worker?.last_error_message && (
        <Card className="border-danger/40 bg-danger/5 p-5">
          <div className="flex items-start gap-3">
            <Activity className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
            <div>
              <div className="text-sm font-medium text-danger">Ultimo errore worker</div>
              <div className="mt-1 font-mono text-xs text-fg-2">
                {health.worker.last_error_message}
              </div>
              {health.worker.last_error_at && (
                <div className="mt-1.5 text-[10px] uppercase tracking-wider text-fg-3">
                  {health.worker.last_error_at}
                </div>
              )}
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}

function toneClass(status: DotStatus): string {
  if (status === "ok") return "border-success/40 bg-success/10 text-success";
  if (status === "warn") return "border-warn/40 bg-warn/10 text-warn";
  if (status === "danger") return "border-danger/40 bg-danger/10 text-danger";
  if (status === "info") return "border-info/40 bg-info/10 text-info";
  return "border-border bg-bg-2 text-fg-3";
}
