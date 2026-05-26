"use client";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { AutoTunerHeader } from "@/components/calibration/AutoTunerHeader";
import { AbAllocationDonut } from "@/components/calibration/AbAllocationDonut";
import { SequenceRoadmap } from "@/components/calibration/SequenceRoadmap";
import { useTunerSourceTests, useTunerStatus } from "@/lib/hooks/queries";
import { cn } from "@/lib/utils";
import type { AutoTunerStatus } from "@/lib/api";

interface Props {
  initialStatus: AutoTunerStatus | null;
}

export function AutoTunerTab({ initialStatus }: Props) {
  const { data: status = initialStatus } = useTunerStatus({
    initialData: initialStatus ?? undefined,
  });
  const { data: sourceTests } = useTunerSourceTests();

  const abProgress = status?.ab_progress ?? null;
  const isAbActive =
    Boolean(abProgress) ||
    (status?.current_step_mode ?? status?.config.mode) === "ab_test";

  return (
    <div className="space-y-4">
      <AutoTunerHeader status={status ?? null} />

      <div className={cn("grid gap-4", isAbActive ? "lg:grid-cols-2" : "")}>
        {isAbActive && <AbAllocationDonut abProgress={abProgress} />}
        <SequenceRoadmap status={status ?? null} />
      </div>

      {/* Source A/B tests panel */}
      <Card className="p-0 overflow-hidden">
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <div>
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Source A/B tests
            </div>
            <div className="mt-0.5 text-xs text-fg-3">
              Test attivi su source overlay (whale flow, fear&greed, ecc.)
            </div>
          </div>
          {sourceTests?.enabled === false && (
            <Badge variant="outline" className="border-warn/40 bg-warn/10 text-warn">
              Disabilitati
            </Badge>
          )}
        </div>
        {sourceTests && sourceTests.tests.length > 0 ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Source</TableHead>
                <TableHead>Step key</TableHead>
                <TableHead>Stato</TableHead>
                <TableHead className="text-right">Disabled</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sourceTests.tests.map((t) => (
                <TableRow key={t.key}>
                  <TableCell className="font-mono font-medium text-fg">{t.source}</TableCell>
                  <TableCell className="font-mono text-fg-2">{t.key}</TableCell>
                  <TableCell>
                    {t.active ? (
                      <Badge variant="outline" className="border-success/40 bg-success/10 text-success">
                        Attivo
                      </Badge>
                    ) : t.available ? (
                      <Badge variant="outline" className="border-info/40 bg-info/10 text-info">
                        Disponibile
                      </Badge>
                    ) : (
                      <Badge variant="outline">In attesa</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs text-fg-3">
                    {t.currently_disabled ? "sì" : "—"}
                  </TableCell>
                </TableRow>
              ))}
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
