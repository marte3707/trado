"use client";

import { useMemo } from "react";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { fmt } from "@/components/atoms";
import { cn } from "@/lib/utils";
import type { SignalRejections } from "@/lib/api";

interface Props {
  rejections: SignalRejections;
  className?: string;
  limit?: number;
}

export function RejectionReasonsList({ rejections, className, limit = 10 }: Props) {
  const total = rejections.total ?? 0;

  const topReasons = useMemo(() => {
    return Object.entries(rejections.by_reason ?? {})
      .map(([reason, count]) => ({ reason, count: count as number }))
      .sort((a, b) => b.count - a.count)
      .slice(0, limit);
  }, [rejections.by_reason, limit]);

  return (
    <Card className={cn("flex flex-col p-0 overflow-hidden", className)}>
      <div className="flex items-center justify-between border-b border-border px-6 py-4">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Rejection reasons
          </div>
          <div className="mt-0.5 text-xs text-fg-3">Click su una riga per vedere i segnali recenti</div>
        </div>
        <Badge variant="outline" className="font-mono">
          {total} totali
        </Badge>
      </div>

      <ScrollArea className="flex-1">
        {topReasons.length === 0 ? (
          <div className="grid h-24 place-items-center text-xs text-fg-3">
            Nessuna rejection negli ultimi cicli.
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {topReasons.map((r) => {
              const share = total > 0 ? (r.count / total) * 100 : 0;
              const matchingRecent = (rejections.recent ?? [])
                .filter((ev) => ev.reasons.includes(r.reason))
                .slice(0, 8);
              return (
                <li key={r.reason}>
                  <Popover>
                    <PopoverTrigger asChild>
                      <button
                        className="w-full px-6 py-3 text-left transition-colors hover:bg-bg-2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-fg-3"
                        title={`Clicca per vedere i segnali rifiutati per: ${r.reason}`}
                      >
                        <div className="flex items-center justify-between gap-3">
                          <span className="truncate font-mono text-sm text-fg-2" title={r.reason}>
                            {r.reason}
                          </span>
                          <span className="flex shrink-0 items-center gap-3">
                            <span className="font-mono text-sm text-fg tabular-nums">{r.count}</span>
                            <span className="font-mono text-[10px] text-fg-3 tabular-nums">
                              {share.toFixed(1)}%
                            </span>
                          </span>
                        </div>
                        <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-bg-3">
                          <div className="h-full bg-warn" style={{ width: `${share}%` }} />
                        </div>
                      </button>
                    </PopoverTrigger>
                    <PopoverContent side="left" align="start" className="w-80 p-0">
                      <div className="border-b border-border px-4 py-3">
                        <div className="font-mono text-xs font-medium text-fg">{r.reason}</div>
                        <div className="mt-0.5 text-[10px] text-fg-3">
                          {r.count} rejection · {matchingRecent.length} esempi recenti
                        </div>
                      </div>
                      {matchingRecent.length === 0 ? (
                        <div className="grid h-16 place-items-center text-xs text-fg-3">
                          Nessun esempio nel lookback corrente.
                        </div>
                      ) : (
                        <ul className="divide-y divide-border">
                          {matchingRecent.map((ev) => (
                            <li key={ev.id} className="px-4 py-2">
                              <div className="flex items-center justify-between gap-2">
                                <span className="font-mono text-xs text-fg">
                                  Signal #{ev.id}
                                </span>
                                <span className="font-mono text-[10px] text-fg-3">
                                  {fmt.relTime(ev.timestamp)}
                                </span>
                              </div>
                              {ev.reasons.length > 1 && (
                                <div className="mt-0.5 flex flex-wrap gap-1">
                                  {ev.reasons
                                    .filter((rr) => rr !== r.reason)
                                    .map((rr) => (
                                      <span
                                        key={rr}
                                        className="inline-flex items-center rounded border border-border px-1 font-mono text-[9px] text-fg-3"
                                      >
                                        +{rr}
                                      </span>
                                    ))}
                                </div>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                    </PopoverContent>
                  </Popover>
                </li>
              );
            })}
          </ul>
        )}
      </ScrollArea>

      {rejections.recent && rejections.recent.length > 0 && (
        <div className="border-t border-border px-6 py-3 text-[11px] text-fg-3">
          <span className="font-medium uppercase tracking-wider">Esempio recente:</span>{" "}
          <span className="font-mono">
            signal #{rejections.recent[0].id} · {fmt.relTime(rejections.recent[0].timestamp)} ·{" "}
            {rejections.recent[0].reasons.slice(0, 2).join(", ")}
          </span>
        </div>
      )}
    </Card>
  );
}
