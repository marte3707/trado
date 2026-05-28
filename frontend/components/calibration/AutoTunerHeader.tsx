"use client";

import { Activity, AlertTriangle, CheckCircle2, Clock, Info, Pause, Play, RotateCcw, Undo2 } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { useAutoTuner } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";
import type { AutoTunerStatus, TunerStepMetrics } from "@/lib/api";

interface Props {
  status: AutoTunerStatus | null;
}

function formatStepKey(key: string | null): string {
  if (!key) return "—";
  return key.replace(/_/g, " ");
}

function fmtPct(v: number | undefined | null): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtPnl(v: number | undefined | null): string {
  if (v == null) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}`;
}

function MetricDelta({
  label,
  before,
  now,
  format,
  tooltip,
}: {
  label: string;
  before: number | null | undefined;
  now: number | null | undefined;
  format: (v: number | undefined | null) => string;
  tooltip?: string;
}) {
  const delta = before != null && now != null ? now - before : null;
  const positive = delta != null ? delta > 0 : null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="cursor-default space-y-0.5">
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-xs text-fg-3">{format(before)}</span>
            <span className="text-[10px] text-fg-4">→</span>
            <span className={cn(
              "font-mono text-sm font-semibold",
              positive === true && "text-success",
              positive === false && "text-danger",
              positive === null && "text-fg-2",
            )}>
              {format(now)}
            </span>
            {delta != null && (
              <span className={cn(
                "font-mono text-[10px]",
                positive === true && "text-success",
                positive === false && "text-danger",
                positive === null && "text-fg-3",
              )}>
                ({delta >= 0 ? "+" : ""}{format(delta)})
              </span>
            )}
          </div>
        </div>
      </TooltipTrigger>
      {tooltip && <TooltipContent className="max-w-60">{tooltip}</TooltipContent>}
    </Tooltip>
  );
}

function StepPerformance({
  before,
  now,
}: {
  before: TunerStepMetrics | null | undefined;
  now: TunerStepMetrics | null | undefined;
}) {
  if (!before && !now) return null;

  return (
    <div className="mt-4 border-t border-border pt-4">
      <div className="mb-3 flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-fg-3">
        Performance step corrente
        <Tooltip>
          <TooltipTrigger asChild>
            <Info className="h-3 w-3 cursor-help" />
          </TooltipTrigger>
          <TooltipContent className="max-w-72">
            Confronto tra le metriche calcolate prima dell&apos;attivazione dello step
            e le metriche correnti sui trade chiusi dopo l&apos;attivazione. Il tuner userà
            questi valori per decidere se promuovere o revertire la modifica.
          </TooltipContent>
        </Tooltip>
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <MetricDelta
          label="Win rate"
          before={before?.win_rate}
          now={now?.win_rate}
          format={fmtPct}
          tooltip="Percentuale di trade chiusi in profitto."
        />
        <MetricDelta
          label="Mean PnL"
          before={before?.mean_pnl}
          now={now?.mean_pnl}
          format={fmtPnl}
          tooltip="PnL medio per trade (USD). Principale metrica usata dal tuner per la promozione."
        />
        <MetricDelta
          label="Equity slope"
          before={before?.equity_slope}
          now={now?.equity_slope}
          format={fmtPnl}
          tooltip="Pendenza della curva equity: USD guadagnati per trade (regressione lineare). Positivo = tendenza ascendente."
        />
        <div className="space-y-0.5">
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">Trade post-step</div>
          <div className="font-mono text-sm font-semibold text-fg">{now?.trades ?? "—"}</div>
        </div>
      </div>
    </div>
  );
}

export function AutoTunerHeader({ status }: Props) {
  const { mutate: runAction, isPending } = useAutoTuner();

  if (!status) {
    return (
      <Card className="p-5">
        <div className="flex items-center gap-2 text-fg-3">
          <AlertTriangle className="h-4 w-4" />
          <span className="text-sm">Auto-tuner non disponibile.</span>
        </div>
      </Card>
    );
  }

  const enabled = status.enabled;
  const isAbTest = (status.current_step_mode ?? status.config.mode) === "ab_test";
  const isSequential = !isAbTest;
  const progress = status.progress;
  const abProgress = status.ab_progress;

  // In AB mode il denominatore è trades_required_per_arm × 2 e il numeratore
  // sono i trade effettivamente assegnati ai bracci (non il totale storico).
  const abArmsDone = (abProgress?.trades_control ?? 0) + (abProgress?.trades_test ?? 0);
  const tradesDone = isAbTest ? abArmsDone : progress?.trades_done ?? status.trades_now ?? 0;
  const tradesRequired = isAbTest
    ? (abProgress?.trades_required_per_arm ?? 0) * 2
    : progress?.trades_required ?? status.config.min_trades_per_step ?? 0;
  const rawPct = tradesRequired > 0 ? (tradesDone / tradesRequired) * 100 : 0;
  const pct = Math.min(100, rawPct);
  const thresholdReached = rawPct >= 100 && status.current_step_key != null;


  return (
    <Card className="p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        {/* Left: stato + step corrente */}
        <div className="space-y-2.5">
          <div className="flex items-center gap-2">
            <div
              className={cn(
                "inline-flex h-2 w-2 rounded-full",
                enabled ? "bg-success shadow-[0_0_0_3px_rgba(62,207,142,0.18)]" : "bg-fg-4",
              )}
            />
            <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Auto-tuner · {enabled ? "attivo" : "in pausa"}
            </span>
            {isAbTest && (
              <Badge variant="outline" className="ml-1 border-info/40 bg-info/10 text-info">
                A/B Test
              </Badge>
            )}
          </div>
          <div className="flex items-baseline gap-3">
            <span className="font-mono text-lg font-semibold text-fg">
              {formatStepKey(status.current_step_key)}
            </span>
            {status.current_step_index != null && (
              <span className="font-mono text-xs text-fg-3">
                step #{status.current_step_index}
              </span>
            )}
          </div>
          {status.next_step_key && (
            <div className="text-xs text-fg-3">
              Prossimo:{" "}
              <span className="font-mono text-fg-2">{formatStepKey(status.next_step_key)}</span>
            </div>
          )}
        </div>

        {/* Right: controlli */}
        <div className="flex flex-wrap items-center gap-2">
          {enabled ? (
            <Button
              variant="outline"
              size="sm"
              disabled={isPending}
              onClick={() => runAction("pause")}
            >
              <Pause className="mr-1.5 h-3.5 w-3.5" />
              Pausa
            </Button>
          ) : (
            <Button size="sm" disabled={isPending} onClick={() => runAction("resume")}>
              <Play className="mr-1.5 h-3.5 w-3.5" />
              Riprendi
            </Button>
          )}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                disabled={isPending}
                onClick={() => runAction("run")}
              >
                <Activity className="mr-1.5 h-3.5 w-3.5" />
                {thresholdReached ? "Valuta ora" : "Avanza ciclo"}
              </Button>
            </TooltipTrigger>
            <TooltipContent className="max-w-64">
              {thresholdReached
                ? "Forza la valutazione immediata dello step senza aspettare il prossimo ciclo del worker (60s)."
                : status.current_step_key
                ? "Esegue manualmente un ciclo del tuner — aggiorna il conteggio trade senza ancora valutare lo step."
                : "Avvia il prossimo step della sequenza."}
            </TooltipContent>
          </Tooltip>
          <ConfirmAction
            label="Rollback"
            icon={<Undo2 className="mr-1.5 h-3.5 w-3.5" />}
            description="Torna allo step precedente, ripristinando i parametri prima dell'ultimo cambio applicato."
            confirmLabel="Esegui rollback"
            onConfirm={() => runAction("rollback")}
            disabled={isPending}
          />
          <ConfirmAction
            label="Reset"
            icon={<RotateCcw className="mr-1.5 h-3.5 w-3.5" />}
            description="Riporta l'auto-tuner allo stato iniziale: pulisce step_history e factor_history."
            confirmLabel="Conferma reset"
            destructive
            onConfirm={() => runAction("reset")}
            disabled={isPending}
          />
        </div>
      </div>

      {/* Progress bar */}
      <div className="mt-5 space-y-2">
        <div className="flex items-center justify-between text-[11px] text-fg-3">
          <div className="flex items-center gap-1.5">
            <span>
              {isAbTest ? "Trade nei bracci A/B" : "Trade accumulati nello step"}{" "}
              <span className="font-mono text-fg-2">
                {tradesDone}/{tradesRequired || "—"}
              </span>
            </span>
            <Tooltip>
              <TooltipTrigger asChild>
                <Info className="h-3 w-3 shrink-0 cursor-help" />
              </TooltipTrigger>
              <TooltipContent className="max-w-72">
                {isAbTest
                  ? `Trade assegnati ai bracci control e test dopo l'attivazione. Ogni braccio richiede almeno ${abProgress?.trades_required_per_arm ?? "—"} trade prima che il test statistico venga eseguito.`
                  : "L'auto-tuner accumula trade chiusi nello step corrente prima di valutare la modifica al parametro. Raggiunta la soglia minima, confronta win rate, equity slope e PnL medio prima/dopo e decide se promuovere o revertire la modifica."}
              </TooltipContent>
            </Tooltip>
          </div>
          {thresholdReached ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <div className="flex cursor-help items-center gap-1 text-warn">
                  <Clock className="h-3 w-3" />
                  <span className="font-mono">In attesa di valutazione</span>
                </div>
              </TooltipTrigger>
              <TooltipContent className="max-w-64">
                Soglia raggiunta ({tradesDone} trade vs {tradesRequired} richiesti).
                {enabled
                  ? " La valutazione avverrà automaticamente al prossimo ciclo del worker."
                  : " Il tuner è in pausa: riprendi per valutare lo step."}
              </TooltipContent>
            </Tooltip>
          ) : (
            tradesRequired > 0 && (
              <span className="font-mono text-fg-2">{pct.toFixed(0)}%</span>
            )
          )}
        </div>

        {thresholdReached ? (
          <div className="flex items-center gap-2">
            <Progress value={100} className="flex-1" />
            <CheckCircle2 className="h-4 w-4 shrink-0 text-warn" />
          </div>
        ) : (
          <Progress value={pct} />
        )}

        {/* Per-arm breakdown in AB mode */}
        {isAbTest && abProgress && (
          <div className="grid grid-cols-2 gap-2 pt-1">
            {(
              [
                { label: "Control", done: abProgress.trades_control, required: abProgress.trades_required_per_arm },
                { label: "Test", done: abProgress.trades_test, required: abProgress.trades_required_per_arm },
              ] as const
            ).map((arm) => {
              const armPct = Math.min(100, (arm.done / arm.required) * 100);
              const ok = arm.done >= arm.required;
              return (
                <div key={arm.label} className="space-y-1">
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-fg-3">{arm.label}</span>
                    <span className={cn("font-mono", ok ? "text-success" : "text-fg-3")}>
                      {arm.done}/{arm.required}
                      {ok && " ✓"}
                    </span>
                  </div>
                  <Progress
                    value={armPct}
                    className={cn("h-1", !ok && arm.done === Math.min(abProgress.trades_control, abProgress.trades_test) && "opacity-70")}
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Performance comparison (sequential mode only) */}
      {isSequential && status.current_step_key && (
        <StepPerformance before={status.metrics_before} now={status.metrics_now} />
      )}
    </Card>
  );
}

function ConfirmAction({
  label,
  icon,
  description,
  confirmLabel,
  onConfirm,
  destructive,
  disabled,
}: {
  label: string;
  icon: React.ReactNode;
  description: string;
  confirmLabel: string;
  onConfirm: () => void;
  destructive?: boolean;
  disabled?: boolean;
}) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="ghost" size="sm" disabled={disabled}>
          {icon}
          {label}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{label} auto-tuner?</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Annulla</AlertDialogCancel>
          <AlertDialogAction
            onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
              e.preventDefault();
              onConfirm();
            }}
            className={cn(destructive && "bg-danger text-destructive-foreground hover:brightness-105")}
          >
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
