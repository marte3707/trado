"use client";

import { HelpCircle, Search } from "lucide-react";

import { ROUTE_MAP, type Route } from "@/components/shell/routes";
import { ModeBadge, type TradingMode } from "@/components/shell/ModeBadge";
import { TopbarStatus } from "@/components/shell/TopbarStatus";
import { KillSwitchButton } from "@/components/shell/KillSwitchButton";
import { ThresholdMenu } from "@/components/shell/ThresholdMenu";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export interface TopbarProps {
  route: Route;
  mode: TradingMode;
  wsLive: boolean;
  onOpenSearch: () => void;
  onOpenHelp: () => void;
}

export function Topbar({ route, mode, wsLive, onOpenSearch, onOpenHelp }: TopbarProps) {
  const meta = ROUTE_MAP[route];

  return (
    <header
      className="flex h-14 items-center gap-3 border-b border-border bg-bg px-5"
      style={{ gridArea: "top" }}
    >
      {/* Title */}
      <div className="flex min-w-0 flex-col leading-tight">
        <span className="truncate text-sm font-semibold tracking-tight text-fg">
          {meta?.label ?? "Trado"}
        </span>
        {meta?.description && (
          <span className="truncate text-xs text-fg-3">{meta.description}</span>
        )}
      </div>

      {/* Global search */}
      <button
        onClick={onOpenSearch}
        className="ml-6 inline-flex h-8 min-w-65 items-center gap-2 rounded-md border border-border bg-bg-1 px-3 text-sm text-fg-3 transition-colors hover:bg-bg-2 hover:text-fg-2"
        aria-label="Apri command menu"
      >
        <Search className="h-3.5 w-3.5" />
        <span>Cerca mercato, segnale o azione...</span>
        <kbd className="ml-auto inline-flex items-center rounded border border-border bg-bg-2 px-1.5 py-0.5 font-mono text-[10px] text-fg-3">
          ⌘K
        </kbd>
      </button>

      <div className="flex-1" />

      {/* Right cluster */}
      <ThresholdMenu />
      <Separator orientation="vertical" className="h-6" />
      <TopbarStatus wsLive={wsLive} />
      <ModeBadge mode={mode} />
      <KillSwitchButton />
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            onClick={onOpenHelp}
            className="grid h-8 w-8 place-items-center rounded-md border border-border bg-bg-1 text-fg-3 transition-colors hover:bg-bg-2 hover:text-fg-2"
            aria-label="Scorciatoie da tastiera"
          >
            <HelpCircle className="h-3.5 w-3.5" />
          </button>
        </TooltipTrigger>
        <TooltipContent side="bottom">Scorciatoie (⇧?)</TooltipContent>
      </Tooltip>
    </header>
  );
}
