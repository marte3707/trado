"use client";

import { Copy, ExternalLink, X } from "lucide-react";
import { toast } from "sonner";

import { Sheet, SheetContent } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { fmt, MarketBadge } from "@/components/atoms";
import {
  LEVEL_LABEL,
  MODE_LABEL,
  type AlertItem,
  type AlertLevel,
  type AlertMode,
} from "@/components/alerts/alertHelpers";
import { cn } from "@/lib/utils";
import type { Market } from "@/lib/api";

interface Props {
  alert: AlertItem | null;
  markets: Market[];
  onClose: () => void;
  onNavigateSignal?: (signalId: number) => void;
  onNavigatePosition?: (positionId: number) => void;
  onOpenMarket?: (market: Market) => void;
}

function asString(v: unknown): string | undefined {
  if (typeof v === "string" && v.trim()) return v;
  return undefined;
}

function resolveMarket(
  alert: AlertItem,
  markets: Market[],
): { market: Market | null; fallbackSymbol: string | null } {
  if (alert.marketId != null) {
    const m = markets.find((mk) => mk.id === alert.marketId);
    if (m) return { market: m, fallbackSymbol: null };
  }
  // Cerca nel payload symbol/market/coin (per eventi system che potrebbero non avere market_id)
  const data = alert.data;
  const symbol =
    asString(data.symbol) ?? asString(data.market) ?? asString(data.coin);
  if (symbol) {
    const found = markets.find(
      (mk) =>
        mk.symbol.toUpperCase() === symbol.toUpperCase() ||
        mk.base.toUpperCase() === symbol.toUpperCase(),
    );
    return { market: found ?? null, fallbackSymbol: symbol };
  }
  return { market: null, fallbackSymbol: null };
}

const LEVEL_TONE: Record<AlertLevel, string> = {
  strong: "border-success/40 bg-success/15 text-success",
  good: "border-success/30 bg-success/10 text-success",
  info: "border-info/30 bg-info/10 text-info",
  warn: "border-warn/40 bg-warn/15 text-warn",
  risk: "border-danger/40 bg-danger/15 text-danger",
};

const MODE_TONE: Record<AlertMode, string> = {
  live: "border-danger/30 bg-danger/10 text-danger",
  paper: "border-warn/30 bg-warn/10 text-warn",
  system: "border-info/30 bg-info/10 text-info",
  unknown: "border-border bg-bg-2 text-fg-3",
};

export function AlertDetailDrawer({
  alert,
  markets,
  onClose,
  onNavigateSignal,
  onNavigatePosition,
  onOpenMarket,
}: Props) {
  if (!alert) return null;
  const open = true;

  const payload = JSON.stringify(alert.data, null, 2);
  const { market, fallbackSymbol } = resolveMarket(alert, markets);
  const tickerLabel = market?.symbol ?? fallbackSymbol ?? null;

  function copyPayload() {
    navigator.clipboard
      .writeText(payload)
      .then(() => toast.success("Payload copiato"))
      .catch(() => toast.error("Copia fallita"));
  }

  return (
    <Sheet open={open} onOpenChange={(v: boolean) => !v && onClose()}>
      <SheetContent
        withOverlay={false}
        hideClose
        className="flex w-full flex-col gap-0 p-0 sm:max-w-md md:max-w-lg"
      >
        {/* Header */}
        <div className="flex shrink-0 items-start justify-between border-b border-border px-5 py-4">
          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
              <Badge variant="outline" className={cn("font-mono uppercase", LEVEL_TONE[alert.level])}>
                {LEVEL_LABEL[alert.level]}
              </Badge>
              <Badge variant="outline" className={cn("font-mono uppercase", MODE_TONE[alert.mode])}>
                {MODE_LABEL[alert.mode]}
              </Badge>
              <span className="text-[11px] text-fg-3">{fmt.relTime(alert.ts)}</span>
            </div>
            <div className="text-sm font-semibold text-fg">{alert.title}</div>
            <div className="font-mono text-[10px] uppercase tracking-wider text-fg-4">
              {alert.eventType}
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-fg-3 transition-colors hover:bg-bg-2 hover:text-fg"
            aria-label="Chiudi"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <ScrollArea className="flex-1">
          <div className="space-y-5 px-5 py-4">
            {/* Message */}
            {alert.message && (
              <Section title="Messaggio">
                <div className="rounded-md border border-border bg-bg-2 p-3 font-mono text-xs leading-relaxed text-fg-2">
                  {alert.message}
                </div>
              </Section>
            )}

            {/* Market */}
            {tickerLabel && (
              <Section title="Market">
                <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-bg-2 p-3">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-fg">{tickerLabel}</span>
                    {market && <MarketBadge market={market.market_type} />}
                    {market && (
                      <span className="font-mono text-[10px] uppercase tracking-wider text-fg-3">
                        {market.base}/{market.quote}
                      </span>
                    )}
                  </div>
                  {market && onOpenMarket && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onOpenMarket(market)}
                      className="shrink-0"
                    >
                      <ExternalLink className="mr-1.5 h-3.5 w-3.5" />
                      Apri chart
                    </Button>
                  )}
                </div>
              </Section>
            )}

            {/* References */}
            <Section title="Riferimenti">
              <div className="grid grid-cols-3 gap-3">
                <RefKv label="Market id" value={alert.marketId} />
                <RefKv label="Signal id" value={alert.signalId} />
                <RefKv label="Position id" value={alert.positionId} />
              </div>
            </Section>

            {/* Data payload */}
            <Section
              title="Data payload"
              right={
                <Button variant="ghost" size="sm" onClick={copyPayload}>
                  <Copy className="mr-1.5 h-3 w-3" />
                  Copia
                </Button>
              }
            >
              <pre className="max-h-[420px] overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[11px] leading-relaxed text-fg-2">
                {payload}
              </pre>
            </Section>
          </div>
        </ScrollArea>

        <Separator />

        <div className="flex shrink-0 flex-wrap items-center gap-2 px-5 py-3">
          {alert.signalId && onNavigateSignal && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onNavigateSignal(alert.signalId!)}
            >
              <ExternalLink className="mr-1.5 h-3.5 w-3.5" />
              Vai al segnale #{alert.signalId}
            </Button>
          )}
          {alert.positionId && onNavigatePosition && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onNavigatePosition(alert.positionId!)}
            >
              <ExternalLink className="mr-1.5 h-3.5 w-3.5" />
              Vai alla posizione #{alert.positionId}
            </Button>
          )}
          <div className="flex-1" />
          <Button variant="ghost" size="sm" onClick={onClose}>
            Chiudi
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function Section({
  title,
  right,
  children,
}: {
  title: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</h3>
        {right}
      </div>
      {children}
    </section>
  );
}

function RefKv({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-0.5 font-mono text-sm tabular-nums",
          value != null ? "text-fg" : "text-fg-4",
        )}
      >
        {value ?? "—"}
      </div>
    </div>
  );
}
