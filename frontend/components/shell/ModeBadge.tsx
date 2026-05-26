"use client";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export type TradingMode = "signal" | "paper" | "live";

const MODE_META: Record<
  TradingMode,
  { label: string; tone: string; description: string }
> = {
  signal: {
    label: "SIGNAL_ONLY",
    tone: "border-info/40 bg-info/10 text-info",
    description: "Genera segnali, nessuna posizione aperta.",
  },
  paper: {
    label: "PAPER_TRADING",
    tone: "border-warn/40 bg-warn/10 text-warn",
    description: "Esegue posizioni simulate su equity virtuale.",
  },
  live: {
    label: "AUTO_TRADE",
    tone: "border-danger/40 bg-danger/10 text-danger",
    description: "Paper + posizioni live su Hyperliquid.",
  },
};

export function ModeBadge({ mode }: { mode: TradingMode }) {
  const meta = MODE_META[mode];
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div
          className={cn(
            "inline-flex h-7 items-center gap-2 rounded-md border px-3 font-mono text-xs tracking-wide",
            meta.tone,
          )}
        >
          <span className="h-1.5 w-1.5 rounded-full bg-current" />
          {meta.label}
        </div>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        <div className="space-y-0.5">
          <div className="font-medium">{meta.label}</div>
          <div className="text-fg-3">{meta.description}</div>
          <div className="text-fg-4">Modificabile da Settings / .env</div>
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
