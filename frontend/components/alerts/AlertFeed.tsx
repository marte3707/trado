"use client";

import { useMemo, useState } from "react";
import { Search } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { fmt } from "@/components/atoms";
import {
  LEVEL_LABEL,
  MODE_LABEL,
  logToAlert,
  type AlertItem,
  type AlertLevel,
  type AlertMode,
} from "@/components/alerts/alertHelpers";
import { cn } from "@/lib/utils";
import type { DecisionLog } from "@/lib/api";

interface Props {
  logs: DecisionLog[];
  selectedId: number | null;
  onSelect: (alert: AlertItem) => void;
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

type LevelFilter = "all" | AlertLevel;
type ModeFilter = "all" | AlertMode;

export function AlertFeed({ logs, selectedId, onSelect }: Props) {
  const [search, setSearch] = useState("");
  const [levelFilter, setLevelFilter] = useState<LevelFilter>("all");
  const [modeFilter, setModeFilter] = useState<ModeFilter>("live");

  const alerts = useMemo(() => logs.map(logToAlert), [logs]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return alerts.filter((a) => {
      if (levelFilter !== "all" && a.level !== levelFilter) return false;
      if (modeFilter !== "all" && a.mode !== modeFilter) return false;
      if (q) {
        const haystack = `${a.title} ${a.eventType} ${a.message} ${a.mode}`.toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    });
  }, [alerts, search, levelFilter, modeFilter]);

  return (
    <Card className="flex h-full flex-col overflow-hidden">
      <div className="space-y-3 border-b border-border px-6 py-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Feed alert
            </div>
            <div className="mt-0.5 text-xs text-fg-3">
              {filtered.length} di {alerts.length} eventi · click per dettaglio
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[200px] flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-3" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Cerca tipo evento, mercato, motivo..."
              className="pl-8"
            />
          </div>
          <ToggleGroup
            type="single"
            value={levelFilter}
            onValueChange={(v: string) => v && setLevelFilter(v as LevelFilter)}
            size="sm"
          >
            <ToggleGroupItem value="all">All</ToggleGroupItem>
            <ToggleGroupItem value="strong">Strong</ToggleGroupItem>
            <ToggleGroupItem value="risk">Risk</ToggleGroupItem>
          </ToggleGroup>
          <ToggleGroup
            type="single"
            value={modeFilter}
            onValueChange={(v: string) => v && setModeFilter(v as ModeFilter)}
            size="sm"
          >
            <ToggleGroupItem value="all">Tutti</ToggleGroupItem>
            <ToggleGroupItem value="live">Live</ToggleGroupItem>
            <ToggleGroupItem value="paper">Paper</ToggleGroupItem>
            <ToggleGroupItem value="system">System</ToggleGroupItem>
          </ToggleGroup>
        </div>
      </div>

      <ScrollArea className="flex-1">
        {filtered.length === 0 ? (
          <div className="grid h-48 place-items-center text-sm text-fg-3">
            Nessun alert corrisponde ai filtri.
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {filtered.map((a) => {
              const isSelected = a.id === selectedId;
              return (
                <li key={a.id}>
                  <button
                    onClick={() => onSelect(a)}
                    className={cn(
                      "flex w-full items-start gap-3 px-6 py-3 text-left transition-colors hover:bg-bg-2",
                      isSelected && "border-l-2 border-l-success bg-bg-2 pl-[22px]",
                    )}
                  >
                    <span className="mt-0.5 w-14 shrink-0 font-mono text-[10px] uppercase tracking-wider text-fg-3">
                      {fmt.relTime(a.ts)}
                    </span>
                    <Badge
                      variant="outline"
                      className={cn(
                        "mt-0.5 shrink-0 font-mono uppercase tracking-wide",
                        LEVEL_TONE[a.level],
                      )}
                    >
                      {LEVEL_LABEL[a.level]}
                    </Badge>
                    <Badge
                      variant="outline"
                      className={cn(
                        "mt-0.5 shrink-0 font-mono uppercase tracking-wide",
                        MODE_TONE[a.mode],
                      )}
                    >
                      {MODE_LABEL[a.mode]}
                    </Badge>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-fg">{a.title}</div>
                      {a.message && (
                        <div className="mt-0.5 truncate text-xs text-fg-3">{a.message}</div>
                      )}
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </ScrollArea>
    </Card>
  );
}
