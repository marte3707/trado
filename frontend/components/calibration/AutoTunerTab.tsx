"use client";

import { Play, Sparkles, X } from "lucide-react";

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
import { useTunerSourceTests, useTunerStatus, useTunerStrategyTests } from "@/lib/hooks/queries";
import { useClearQueuedStrategyTest, useQueueStrategyTest, useStartSourceTest, useStartStrategyTest } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";
import type { AutoTunerSourceTest, AutoTunerStatus, AutoTunerStrategyTest } from "@/lib/api";

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

function strategyTestArmSummary(test: AutoTunerStrategyTest): string {
  return `${test.strategy} OFF su ${test.timeframe} nel braccio test`;
}

function recommendationLabel(value: string | null | undefined): string {
  if (value === "keep_enabled") return "Keep enabled";
  if (value === "candidate_disable") return "Candidate disable";
  if (value === "inconclusive") return "Inconclusive";
  return "—";
}

function fmtSigned(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

function fmtPct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function readNumber(record: Record<string, unknown> | undefined, key: string): number | null {
  if (!record) return null;
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function AutoTunerTab({ initialStatus }: Props) {
  const { data: status = initialStatus } = useTunerStatus({
    initialData: initialStatus ?? undefined,
  });
  const { data: sourceTests } = useTunerSourceTests();
  const { data: strategyTests } = useTunerStrategyTests();
  const { mutate: startSourceTest, isPending: isStartingSource } = useStartSourceTest();
  const { mutate: startStrategyTest, isPending: isStartingStrategy } = useStartStrategyTest();
  const { mutate: queueStrategyTest, isPending: isQueueingStrategy } = useQueueStrategyTest();
  const { mutate: clearQueuedStrategyTest, isPending: isClearingQueuedStrategy } = useClearQueuedStrategyTest();

  const abProgress = status?.ab_progress ?? null;
  const isAbActive =
    Boolean(abProgress) ||
    (status?.current_step_mode ?? status?.config.mode) === "ab_test";

  const activeSourceKey = sourceTests?.active_step_key;
  const blockedBySequential =
    sourceTests?.blocked && !activeSourceKey?.endsWith("_source");
  const activeStrategyKey = strategyTests?.active_step_key;
  const blockedStrategyTests =
    strategyTests?.blocked &&
    !(status?.target_timeframe && status?.target_strategy);
  const queuedStrategyTest = strategyTests?.queued_strategy_test ?? status?.queued_strategy_test ?? null;

  return (
    <div className="space-y-4">
      <AutoTunerHeader status={status ?? null} />

      <div className={cn("grid gap-4", isAbActive ? "lg:grid-cols-2" : "")}>
        {isAbActive && <AbAllocationDonut abProgress={abProgress} />}
        <SequenceRoadmap status={status ?? null} />
      </div>

      <RecentEvaluationsCard status={status ?? null} />

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
                const canStart = t.available && !t.active && !isStartingSource;
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

      <Card className="overflow-hidden p-0">
        <div className="flex items-start justify-between border-b border-border px-6 py-4">
          <div>
            <div className="flex items-center gap-2">
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Strategy A/B tests
              </div>
              {strategyTests?.enabled === false && (
                <Badge variant="outline" className="border-warn/40 bg-warn/10 text-[10px] text-warn">
                  Tuner disabilitato
                </Badge>
              )}
              {status?.target_timeframe && status?.target_strategy && (
                <Badge variant="outline" className="border-info/40 bg-info/10 text-[10px] text-info">
                  Attivo: {status.target_timeframe}/{status.target_strategy}
                </Badge>
              )}
            </div>
            <div className="mt-0.5 text-xs text-fg-3">
              Test A/B manuali su singola strategia e timeframe. La strategia resta attiva globalmente:
              nel braccio test viene solo esclusa runtime dal timeframe scelto.
            </div>
          </div>
        </div>

        {blockedStrategyTests && (
          <div className="border-b border-border bg-warn/5 px-6 py-2.5 text-xs text-warn">
            Strategy test bloccati da step tuner attivo (
            <span className="font-mono">{status?.current_step_key ?? activeStrategyKey ?? "—"}</span>
            ). Quando questo step si chiude, qui potrai avviare il test dal pulsante.
          </div>
        )}

        {strategyTests === null ? (
          <div className="grid h-24 place-items-center text-center text-xs text-fg-3">
            Strategy tests non disponibili dal backend.
            <br />
            Riavvia il backend se hai appena aggiornato il codice.
          </div>
        ) : strategyTests && strategyTests.tests.length > 0 ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Strategia</TableHead>
                <TableHead>Braccio test</TableHead>
                <TableHead>Ultimo esito</TableHead>
                <TableHead>Stato</TableHead>
                <TableHead className="text-right">Azione</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {strategyTests.tests.map((t) => {
                const isQueued = Boolean(t.queued);
                const hasOtherQueued = Boolean(queuedStrategyTest && queuedStrategyTest.step_key !== t.key);
                const canStart = t.available && !t.active && !isQueued && !hasOtherQueued && !isStartingStrategy;
                const canQueue =
                  !t.active &&
                  !isQueued &&
                  !hasOtherQueued &&
                  !t.available &&
                  !isQueueingStrategy;
                const canClearQueue = isQueued && !isClearingQueuedStrategy;
                const disabledReason = isQueued
                  ? `In coda: ${queuedStrategyTest?.timeframe}/${queuedStrategyTest?.strategy}`
                  : hasOtherQueued
                    ? `Altro test gia' in coda: ${queuedStrategyTest?.timeframe}/${queuedStrategyTest?.strategy}`
                    : !t.available
                      ? strategyTests.active_step_key && strategyTests.active_step_key !== t.key
                        ? `Bloccato da ${strategyTests.active_step_key}`
                        : "Step tuner attivo"
                      : null;

                return (
                  <TableRow key={t.key}>
                    <TableCell>
                      <div className="space-y-0.5">
                        <div className="font-mono font-medium text-fg">
                          {t.timeframe}/{t.strategy}
                        </div>
                        <div className="font-mono text-[10px] text-fg-3">{t.key}</div>
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="space-y-0.5">
                        <div className="text-xs text-fg-2">{strategyTestArmSummary(t)}</div>
                        <div className="text-[10px] text-fg-4">
                          Control invariato, test con esclusione selettiva
                        </div>
                      </div>
                    </TableCell>
                    <TableCell>
                      {t.last_recommendation ? (
                        <Badge
                          variant="outline"
                          className={cn(
                            t.last_recommendation === "candidate_disable"
                              ? "border-danger/40 bg-danger/10 text-danger"
                              : t.last_recommendation === "keep_enabled"
                                ? "border-success/40 bg-success/10 text-success"
                                : "border-warn/40 bg-warn/10 text-warn",
                          )}
                        >
                          {recommendationLabel(t.last_recommendation)}
                        </Badge>
                      ) : (
                        <span className="text-xs text-fg-3">Nessun test completato</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {t.active ? (
                        <Badge variant="outline" className="border-info/40 bg-info/10 text-info">
                          In corso
                        </Badge>
                      ) : isQueued ? (
                        <Badge variant="outline" className="border-warn/40 bg-warn/10 text-warn">
                          In coda
                        </Badge>
                      ) : t.available ? (
                        <Badge variant="outline" className="border-success/40 bg-success/10 text-success">
                          Avviabile
                        </Badge>
                      ) : (
                        <Badge variant="outline">Bloccato</Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {t.active ? (
                        <span className="text-[10px] text-fg-3">A/B in esecuzione</span>
                      ) : isQueued ? (
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-7 text-[11px]"
                          disabled={!canClearQueue}
                          onClick={() => clearQueuedStrategyTest()}
                        >
                          <X className="mr-1 h-3 w-3" />
                          Rimuovi coda
                        </Button>
                      ) : (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <div className="inline-flex gap-2">
                              <span>
                                <Button
                                  variant="outline"
                                  size="sm"
                                  className="h-7 text-[11px]"
                                  disabled={!canStart}
                                  onClick={() => startStrategyTest({ timeframe: t.timeframe, strategy: t.strategy })}
                                >
                                  <Sparkles className="mr-1 h-3 w-3" />
                                  Avvia test
                                </Button>
                              </span>
                              {!t.available && (
                                <span>
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    className="h-7 text-[11px]"
                                    disabled={!canQueue}
                                    onClick={() => queueStrategyTest({ timeframe: t.timeframe, strategy: t.strategy })}
                                  >
                                    <Play className="mr-1 h-3 w-3" />
                                    Metti in coda
                                  </Button>
                                </span>
                              )}
                            </div>
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
            Nessun strategy test configurato.
          </div>
        )}
      </Card>
    </div>
  );
}

function RecentEvaluationsCard({ status }: { status: AutoTunerStatus | null }) {
  const history = (status?.step_history ?? []).slice().reverse().slice(0, 8);

  return (
    <Card className="overflow-hidden p-0">
      <div className="border-b border-border px-6 py-4">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Recent evaluations
        </div>
        <div className="mt-0.5 text-xs text-fg-3">
          Esiti recenti del tuner con before/after e metriche A/B.
        </div>
      </div>

      {history.length === 0 ? (
        <div className="grid h-24 place-items-center text-xs text-fg-3">
          Nessuna valutazione disponibile.
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Step</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead>Trade</TableHead>
              <TableHead>WR</TableHead>
              <TableHead>PnL medio</TableHead>
              <TableHead>Dettaglio A/B</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {history.map((step, index) => {
              const ab = step.ab_test && typeof step.ab_test === "object" ? step.ab_test : undefined;
              const nControl = readNumber(ab, "n_control");
              const nTest = readNumber(ab, "n_test");
              const meanControl = readNumber(ab, "mean_control");
              const meanTest = readNumber(ab, "mean_test");
              const meanDelta = readNumber(ab, "mean_delta");
              const pValue = readNumber(ab, "p_one_tailed");

              return (
                <TableRow key={`${step.key}-${index}`}>
                  <TableCell>
                    <div className="space-y-0.5">
                      <div className="font-mono text-sm text-fg">{step.label || step.key}</div>
                      <div className="flex items-center gap-2">
                        {step.mode === "ab_test" && (
                          <Badge variant="outline" className="border-info/40 bg-info/10 text-info">
                            A/B
                          </Badge>
                        )}
                        <span className="font-mono text-[10px] text-fg-3">
                          {new Date(step.evaluated_at).toLocaleString("it-IT")}
                        </span>
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={cn(
                        step.status === "success" || step.status === "promoted"
                          ? "border-success/40 bg-success/10 text-success"
                          : step.status === "reverted" || step.status === "rejected"
                            ? "border-danger/40 bg-danger/10 text-danger"
                            : "border-warn/40 bg-warn/10 text-warn",
                      )}
                    >
                      {step.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {step.trades_evaluated ?? step.metrics_after?.trades ?? "—"}
                  </TableCell>
                  <TableCell>
                    <div className="space-y-0.5 font-mono text-xs">
                      <div>{fmtPct(step.metrics_before?.win_rate)}</div>
                      <div className="text-fg-3">→ {fmtPct(step.metrics_after?.win_rate)}</div>
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="space-y-0.5 font-mono text-xs">
                      <div>{fmtSigned(step.metrics_before?.mean_pnl ?? step.metrics_before?.realized_pnl)}</div>
                      <div className="text-fg-3">
                        → {fmtSigned(step.metrics_after?.mean_pnl ?? step.metrics_after?.realized_pnl)}
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>
                    {step.mode === "ab_test" ? (
                      <div className="space-y-0.5 font-mono text-xs">
                        <div>C/T {nControl ?? "—"}/{nTest ?? "—"}</div>
                        <div>mean {fmtSigned(meanControl)}/{fmtSigned(meanTest)}</div>
                        <div className={cn(meanDelta != null && meanDelta >= 0 ? "text-success" : "text-danger")}>
                          Δ {fmtSigned(meanDelta)}
                        </div>
                        <div className="text-fg-3">p {pValue != null ? pValue.toFixed(3) : "—"}</div>
                      </div>
                    ) : (
                      <span className="text-xs text-fg-3">—</span>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </Card>
  );
}
