"use client";

import { ExternalLink, Eye, EyeOff, Send, X } from "lucide-react";

import { Sheet, SheetContent } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  DirBadge,
  MarketBadge,
  ScoreChip,
  StatusBadge,
  TfChip,
  fmt,
} from "@/components/atoms";
import { StrategyContributionsChart } from "@/components/signals/StrategyContributionsChart";
import {
  useDeleteSignal,
  useExecuteSignalPaper,
  useIgnoreSignal,
  useReopenSignal,
} from "@/lib/hooks/mutations";
import { useDecisionLogsFiltered } from "@/lib/hooks/queries";
import { cn } from "@/lib/utils";
import type { Position, Signal } from "@/lib/api";

interface Props {
  signal: Signal | null;
  positions: Position[];
  onClose: () => void;
  onOpenChart?: (signal: Signal) => void;
}

function asNumber(v: unknown): number | null {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function SignalAnatomyDrawer({ signal, positions, onClose, onOpenChart }: Props) {
  const open = signal !== null;

  const executePaper = useExecuteSignalPaper();
  const ignore = useIgnoreSignal();
  const reopen = useReopenSignal();
  const remove = useDeleteSignal();
  const { data: signalLogs = [] } = useDecisionLogsFiltered(
    { signal_id: signal?.id, limit: 20 },
    { enabled: signal !== null },
  );

  if (!signal) return null;

  const linkedPosition = positions.find((p) => {
    const src = asNumber(p.metadata_json?.source_signal_id);
    return src === signal.id;
  });

  const entry = asNumber(signal.entry_price);
  const sl = asNumber(signal.stop_loss);
  const tp1 = asNumber(signal.take_profit_1);
  const tp2 = asNumber(signal.take_profit_2);
  const tp3 = asNumber(signal.take_profit_3);
  const rr = asNumber(signal.risk_reward);

  const isIgnored = signal.status === "ignored";
  const isExecuted = signal.status === "executed" || signal.status === "closed";

  return (
    <Sheet open={open} onOpenChange={(v: boolean) => !v && onClose()}>
      <SheetContent
        withOverlay={false}
        hideClose
        className="flex w-full flex-col gap-0 p-0 sm:max-w-md md:max-w-lg"
      >
        {/* Header */}
        <div className="flex shrink-0 items-start justify-between border-b border-border px-5 py-4">
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-2">
              <span className="font-mono text-base font-semibold text-fg">
                {signal.market?.symbol ?? "—"}
              </span>
              {signal.market && <MarketBadge market={signal.market.market_type} />}
              <TfChip tf={signal.timeframe} />
            </div>
            <div className="flex items-center gap-2">
              <DirBadge dir={signal.direction} />
              <ScoreChip score={signal.score} />
              <StatusBadge status={signal.status} />
              <span className="text-[11px] text-fg-3">{fmt.relTime(signal.created_at)}</span>
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
            {/* Strategy contributions */}
            <Section title="Strategy contributions">
              <StrategyContributionsChart
                breakdown={signal.strategy_breakdown ?? {}}
                signalDirection={signal.direction}
              />
              <div className="mt-2 flex items-center gap-3 text-[10px] text-fg-3">
                <LegendDot tone="success" /> concorda
                <LegendDot tone="danger" /> oppone
                <LegendDot tone="muted" /> neutro
              </div>
            </Section>

            {/* Trigger reason */}
            {signal.reason && (
              <Section title="Trigger reason">
                <div className="rounded-md border border-border bg-bg-2 p-3 font-mono text-xs leading-relaxed text-fg-2">
                  {signal.reason}
                </div>
              </Section>
            )}

            {/* Risk plan */}
            <Section title="Risk plan">
              <div className="grid grid-cols-2 gap-3">
                <Kv label="Entry" value={entry != null ? fmt.price(entry) : "—"} tone="muted" />
                <Kv label="Stop loss" value={sl != null ? fmt.price(sl) : "—"} tone="short" />
                <Kv label="TP1" value={tp1 != null ? fmt.price(tp1) : "—"} tone="long" />
                <Kv label="TP2" value={tp2 != null ? fmt.price(tp2) : "—"} tone="long" />
                <Kv label="TP3" value={tp3 != null ? fmt.price(tp3) : "—"} tone="long" />
                <Kv
                  label="Risk/Reward"
                  value={rr != null ? rr.toFixed(2) : "—"}
                  tone={rr != null && rr >= 2 ? "long" : "muted"}
                />
              </div>
            </Section>

            {/* Decision log */}
            {signalLogs.length > 0 && (
              <Section title={`Decision log (${signalLogs.length})`}>
                <ul className="space-y-1.5">
                  {signalLogs.map((log) => (
                    <li key={log.id} className="rounded-md border border-border bg-bg-2 px-3 py-2">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-[10px] uppercase tracking-wide text-fg-3">
                          {log.event_type.replace(/_/g, " ")}
                        </span>
                        <span className="font-mono text-[10px] text-fg-4">
                          {fmt.relTime(log.timestamp)}
                        </span>
                      </div>
                      {log.message && (
                        <div className="mt-1 text-xs leading-relaxed text-fg-2">{log.message}</div>
                      )}
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {/* Linked position */}
            {linkedPosition && (
              <Section title="Posizione collegata">
                <div className="rounded-md border border-border bg-bg-2 p-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span
                        className={cn(
                          "inline-flex h-5 items-center rounded-md border px-1.5 font-mono text-[10px]",
                          linkedPosition.side === "long"
                            ? "border-success/30 bg-success/10 text-success"
                            : "border-danger/30 bg-danger/10 text-danger",
                        )}
                      >
                        {linkedPosition.side.toUpperCase()}
                      </span>
                      <span className="font-mono text-sm font-medium text-fg">
                        {linkedPosition.market?.symbol}
                      </span>
                      <span className="text-[10px] uppercase tracking-wider text-fg-3">
                        {linkedPosition.mode}
                      </span>
                    </div>
                    <span
                      className={cn(
                        "font-mono text-sm",
                        linkedPosition.status === "open" ? "text-success" : "text-fg-3",
                      )}
                    >
                      {linkedPosition.status.toUpperCase()}
                    </span>
                  </div>
                  <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-xs text-fg-3">
                    <span>Entry {fmt.price(linkedPosition.entry_price)}</span>
                    <span>Size {fmt.price(linkedPosition.size)}</span>
                    <span>
                      PnL{" "}
                      <span
                        className={cn(
                          (asNumber(linkedPosition.realized_pnl ?? linkedPosition.unrealized_pnl) ?? 0) >= 0
                            ? "text-success"
                            : "text-danger",
                        )}
                      >
                        {fmt.pnl(asNumber(linkedPosition.realized_pnl ?? linkedPosition.unrealized_pnl))}
                      </span>
                    </span>
                  </div>
                </div>
              </Section>
            )}
          </div>
        </ScrollArea>

        <Separator />

        {/* Footer actions */}
        <div className="flex shrink-0 items-center gap-2 px-5 py-3">
          <Button
            variant="outline"
            size="sm"
            disabled={!signal.market}
            onClick={() => signal && onOpenChart?.(signal)}
          >
            <ExternalLink className="mr-1.5 h-3.5 w-3.5" />
            Apri chart
          </Button>

          {!isExecuted && (
            <Button
              size="sm"
              disabled={executePaper.isPending}
              onClick={() => executePaper.mutate(signal.id)}
            >
              <Send className="mr-1.5 h-3.5 w-3.5" />
              {executePaper.isPending ? "Esecuzione..." : "Paper trade"}
            </Button>
          )}

          {!isIgnored && !isExecuted && (
            <Button
              variant="ghost"
              size="sm"
              disabled={ignore.isPending}
              onClick={() => ignore.mutate(signal.id)}
            >
              <EyeOff className="mr-1.5 h-3.5 w-3.5" />
              Ignora
            </Button>
          )}
          {isIgnored && (
            <Button
              variant="ghost"
              size="sm"
              disabled={reopen.isPending}
              onClick={() => reopen.mutate(signal.id)}
            >
              <Eye className="mr-1.5 h-3.5 w-3.5" />
              Riapri
            </Button>
          )}

          <div className="flex-1" />
          <Button
            variant="ghost"
            size="sm"
            disabled={remove.isPending}
            onClick={() => {
              if (confirm("Eliminare definitivamente questo segnale?")) {
                remove.mutate(signal.id, { onSuccess: onClose });
              }
            }}
            className="text-fg-3 hover:text-danger"
          >
            Elimina
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</h3>
      {children}
    </section>
  );
}

function Kv({ label, value, tone }: { label: string; value: string; tone?: "long" | "short" | "muted" }) {
  return (
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-0.5 font-mono text-sm tabular-nums",
          tone === "long" && "text-success",
          tone === "short" && "text-danger",
          tone === "muted" && "text-fg",
          !tone && "text-fg",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function LegendDot({ tone }: { tone: "success" | "danger" | "muted" }) {
  return (
    <span
      className={cn(
        "h-2 w-2 rounded-sm",
        tone === "success" && "bg-success",
        tone === "danger" && "bg-danger",
        tone === "muted" && "bg-fg-3",
      )}
    />
  );
}
