"use client";

import { useState } from "react";
import { Settings2 } from "lucide-react";

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Slider } from "@/components/ui/slider";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StrategyParamsDialog } from "@/components/strategies/StrategyParamsDialog";
import { useUpdateStrategy } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";
import type { Strategy } from "@/lib/api";

interface Props {
  strategies: Strategy[];
}

const TF_ORDER = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"];

export function StrategyTimeframeAccordion({ strategies }: Props) {
  const [editing, setEditing] = useState<Strategy | null>(null);

  const groups: Record<string, Strategy[]> = strategies.reduce(
    (acc, s) => {
      (acc[s.timeframe] = acc[s.timeframe] ?? []).push(s);
      return acc;
    },
    {} as Record<string, Strategy[]>,
  );

  const sortedTfs = [
    ...TF_ORDER.filter((tf) => tf in groups),
    ...Object.keys(groups).filter((tf) => !TF_ORDER.includes(tf)),
  ];

  // Default open: timeframe con almeno una strategia attiva
  const defaultOpen = sortedTfs.filter((tf) => groups[tf].some((s) => s.enabled));

  return (
    <>
      <Card className="p-0 overflow-hidden">
        <Accordion type="multiple" defaultValue={defaultOpen} className="w-full">
          {sortedTfs.map((tf) => {
            const items = groups[tf];
            const activeCount = items.filter((s) => s.enabled).length;
            const totalWeight = items.reduce((sum, s) => sum + (s.enabled ? s.weight : 0), 0);
            return (
              <AccordionItem key={tf} value={tf} className="px-6">
                <AccordionTrigger>
                  <div className="flex w-full items-center gap-4">
                    <span className="font-mono text-base font-semibold text-fg">{tf}</span>
                    <Badge variant="outline" className="font-mono">
                      {activeCount}/{items.length} attive
                    </Badge>
                    <span className="hidden flex-1 text-right font-mono text-xs text-fg-3 md:block">
                      peso attivo {totalWeight}
                    </span>
                  </div>
                </AccordionTrigger>
                <AccordionContent>
                  <div className="space-y-2 pb-4">
                    {items
                      .slice()
                      .sort((a, b) => a.strategy_name.localeCompare(b.strategy_name))
                      .map((s) => (
                        <StrategyRow key={s.id} strategy={s} onEdit={() => setEditing(s)} />
                      ))}
                  </div>
                </AccordionContent>
              </AccordionItem>
            );
          })}
        </Accordion>
      </Card>

      <StrategyParamsDialog strategy={editing} onClose={() => setEditing(null)} />
    </>
  );
}

function StrategyRow({ strategy, onEdit }: { strategy: Strategy; onEdit: () => void }) {
  const { mutate: update, isPending } = useUpdateStrategy();
  const [localWeight, setLocalWeight] = useState(strategy.weight);

  const paramsCount = Object.keys(strategy.params ?? {}).length;

  return (
    <div
      className={cn(
        "grid items-center gap-4 rounded-md border border-border bg-bg-2/40 px-4 py-3",
        !strategy.enabled && "opacity-60",
      )}
      style={{ gridTemplateColumns: "auto 1fr auto 200px auto" }}
    >
      <Switch
        checked={strategy.enabled}
        disabled={isPending}
        onCheckedChange={(v: boolean) =>
          update({ id: strategy.id, patch: { enabled: v } })
        }
      />
      <div className="min-w-0">
        <div className="truncate font-mono text-sm font-medium text-fg">
          {strategy.strategy_name}
        </div>
        <div className="text-[11px] text-fg-3">
          {paramsCount} parametri configurabili
        </div>
      </div>
      <span className="font-mono text-xs text-fg-3 tabular-nums">peso {localWeight}</span>
      <Slider
        value={[localWeight]}
        min={0}
        max={50}
        step={1}
        disabled={isPending}
        onValueChange={(v: number[]) => setLocalWeight(v[0])}
        onValueCommit={() =>
          localWeight !== strategy.weight &&
          update({ id: strategy.id, patch: { weight: localWeight } })
        }
      />
      <Button variant="outline" size="sm" onClick={onEdit} disabled={paramsCount === 0}>
        <Settings2 className="mr-1.5 h-3.5 w-3.5" />
        Parametri
      </Button>
    </div>
  );
}
