"use client";

import { Activity, ArrowRight } from "lucide-react";

import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { DirBadge, MarketBadge, ScoreChip, TfChip, fmt } from "@/components/atoms";
import { cn } from "@/lib/utils";
import type { Signal } from "@/lib/api";

interface TopSignalsListProps {
  signals: Signal[];
  threshold?: number;
  limit?: number;
  onOpen?: (signal: Signal) => void;
  className?: string;
}

export function TopSignalsList({
  signals,
  threshold = 0,
  limit = 6,
  onOpen,
  className,
}: TopSignalsListProps) {
  const items = signals
    .filter((s) => s.score >= threshold && s.status !== "ignored")
    .sort((a, b) => {
      // newer first if status=new, otherwise by score
      if (a.status === "new" && b.status !== "new") return -1;
      if (b.status === "new" && a.status !== "new") return 1;
      return b.score - a.score;
    })
    .slice(0, limit);

  return (
    <Card className={cn("flex flex-col", className)}>
      <div className="flex items-center justify-between border-b border-border px-6 py-4">
        <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Top signals
        </span>
        <span className="font-mono text-[10px] text-fg-3">
          {items.length}/{signals.length}
        </span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {items.length === 0 ? (
          <EmptyState
            variant="inline"
            icon={Activity}
            title="Nessun segnale sopra la soglia"
            description={`Soglia score corrente: ${threshold}. Abbassala in topbar (Score ≥) per allargare il filtro.`}
          />
        ) : (
          <ul className="divide-y divide-border">
            {items.map((s) => (
              <li key={s.id}>
                <button
                  className="group flex w-full items-center gap-2.5 px-6 py-3 text-left transition-colors hover:bg-bg-2"
                  onClick={() => onOpen?.(s)}
                >
                  <span className="font-mono text-sm font-medium tabular-nums text-fg">
                    {s.market?.symbol ?? "—"}
                  </span>
                  <TfChip tf={s.timeframe} />
                  {s.market && <MarketBadge market={s.market.market_type} />}
                  <DirBadge dir={s.direction} />
                  <span className="ml-auto flex items-center gap-2">
                    <ScoreChip score={s.score} />
                    <span className="hidden font-mono text-[10px] text-fg-4 sm:inline">
                      {fmt.relTime(s.created_at)}
                    </span>
                    <ArrowRight className="h-3.5 w-3.5 text-fg-4 transition-transform group-hover:translate-x-0.5 group-hover:text-fg-2" />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
