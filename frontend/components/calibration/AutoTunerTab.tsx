"use client";

import { Play } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { AutoTunerHeader } from "@/components/calibration/AutoTunerHeader";
import { AbAllocationDonut } from "@/components/calibration/AbAllocationDonut";
import { SequenceRoadmap } from "@/components/calibration/SequenceRoadmap";
import { useTunerSourceTests, useTunerStatus } from "@/lib/hooks/queries";
import { useStartSourceTest } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";
import type { AutoTunerSourceTest, AutoTunerStatus } from "@/lib/api";

interface Props {
  initialStatus: AutoTunerStatus | null;
}

function overlayLabel(overlay: Record<string, unknown>): string {
  const disabled = (overlay.disabled_signal_sources as string[] | undefined) ?? [];
  if (disabled.length === 0) return "tutte le source attive";
  return disabled.map((s) => `${s} OFF`).join(", ");
}

function testArmSummary(test: AutoTunerSourceTest): string {
  const controlDisabled = (test.control_overlay?.disabled_signal_sources as string[] | undefined) ?? [];
  const testDisabled = (test.test_overlay?.disabled_signal_sources as string[] | undefined) ?? [];
  const activated = controlDisabled.filter((s) => !testDisabled.includes(s));
  if (activated.length === 0) return "nessun cambio (source già attiva)";
  return activated.map((s) => `${s} abilitato`).join(", ");
}

export function AutoTunerTab({ initialStatus }: Props) {
  const { data: status = initialStatus } = useTunerStatus({
    initialData: initialStatus ?? undefined,
  });
  const { data: sourceTests } = useTunerSourceTests();
  const { mutate: startSourceTest, isPending: isStarting } = useStartSourceTest();

  const abProgress = status?.ab_progress ?? null;
  const isAbActive =
    Boolean(abProgress) ||
    (status?.current_step_mode ?? status?.config.mode) === "ab_test";

  const activeSourceKey = sourceTests?.active_step_key;
  const blockedBySequential =
    sourceTests?.blocked && !activeSourceKey?.endsWith("_source");

  return (
    <div className="space-y-4">
      <AutoTunerHeader status={status ?? null} />

      <div className={cn("grid gap-4", isAbActive ? "lg:grid-cols-2" : "")}>
        {isAbActive && <AbAllocationDonut abProgress={abProgress} />}
        <SequenceRoadmap status={status ?? null} />
      </div>

      {/* Source A/B tests panel */}
      <Card className="overflow-hidden p-0">
        <div className="flex items-start justify-between border-b border-border px-6 py-4">
          <div>
            <div className="flex items-center gap-2">
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Source A/B tests
              </div>
              {sourceTests?.enabled === false && (
                <Badge variant="outline" className="border-warn/40 bg-warn/10 text-[10px] text-warn">
                  Tuner disabilitato
                </Badge>
              )}
            </div>
            <div className="mt-0.5 text-xs text-fg-3">
              Test isolati su una singola source. Richiede avvio manuale — un test alla volta.
            </div>
          </div>
        </div>

        {blockedBySequential && (
          <div className="border-b border-border bg-warn/5 px-6 py-2.5 text-xs text-warn">
            Step sequenziale in corso (
            <span className="font-mono">{status?.current_step_key ?? "—"}</span>
            ). I source test non possono partire finché lo step non viene concluso.
          </div>
        )}

        {sourceTests && sourceTests.tests.length > 0 ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Source</TableHead>
                <TableHead>
                  <Tooltip>
                    <TooltipTrigger className="cursor-help underline decoration-dotted underline-offset-2">
                      Braccio test
                    </TooltipTrigger>
                    <TooltipContent className="max-w-64">
                      Cosa cambia nel braccio test rispetto al controllo (stato attuale delle source
                      disabilitate). Il tuner confronta le metriche dei due bracci con un test A/B
                      statistico e decide se promuovere la modifica.
                    </TooltipContent>
                  </Tooltip>
                </TableHead>
                <TableHead>Stato</TableHead>
                <TableHead className="text-right">Azione</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sourceTests.tests.map((t) => {
                const armSummary = testArmSummary(t);
                const canStart = t.available && !t.active && !isStarting;
                const disabledReason = !t.available
                  ? activeSourceKey && activeSourceKey !== t.key
                    ? "Altro source test già in corso"
                    : "Step sequenziale attivo"
                  : null;

                return (
                  <TableRow key={t.key}>
                    <TableCell>
                      <div className="font-mono font-medium text-fg">{t.source}</div>
                      <div className="font-mono text-[10px] text-fg-3">{t.key}</div>
                    </TableCell>
                    <TableCell>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <div className="cursor-help space-y-0.5">
                            <div className="text-xs text-fg-2">{armSummary}</div>
                            {t.currently_disabled && (
                              <div className="text-[10px] text-fg-4">attualmente disabilitata</div>
                            )}
                          </div>
                        </TooltipTrigger>
                        <TooltipContent className="max-w-xs">
                          <div className="space-y-1 text-[11px]">
                            <div>
                              <span className="text-fg-3">Control: </span>
                              <span className="font-mono">{overlayLabel(t.control_overlay)}</span>
                            </div>
                            <div>
                              <span className="text-fg-3">Test: </span>
                              <span className="font-mono">{overlayLabel(t.test_overlay)}</span>
                            </div>
                          </div>
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>
                    <TableCell>
                      {t.active ? (
                        <Badge variant="outline" className="border-info/40 bg-info/10 text-info">
                          In corso
                        </Badge>
                      ) : t.available ? (
                        <Badge variant="outline" className="border-success/40 bg-success/10 text-success">
                          Disponibile
                        </Badge>
                      ) : (
                        <Badge variant="outline">In attesa</Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {t.active ? (
                        <span className="text-[10px] text-fg-3">A/B in esecuzione</span>
                      ) : (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span>
                              <Button
                                variant="outline"
                                size="sm"
                                className="h-7 text-[11px]"
                                disabled={!canStart}
                                onClick={() => startSourceTest(t.source)}
                              >
                                <Play className="mr-1 h-3 w-3" />
                                Avvia test
                              </Button>
                            </span>
                          </TooltipTrigger>
                          {disabledReason && (
                            <TooltipContent>{disabledReason}</TooltipContent>
                          )}
                        </Tooltip>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        ) : (
          <div className="grid h-24 place-items-center text-xs text-fg-3">
            Nessun source test configurato.
          </div>
        )}
      </Card>
    </div>
  );
}
