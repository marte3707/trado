"use client";

import { Activity, AlertTriangle, Pause, Play, RotateCcw, Undo2 } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
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
import type { AutoTunerStatus } from "@/lib/api";

interface Props {
  status: AutoTunerStatus | null;
}

function formatStepKey(key: string | null): string {
  if (!key) return "—";
  return key.replace(/_/g, " ");
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
  const progress = status.progress;
  const abProgress = status.ab_progress;
  const tradesDone = progress?.trades_done ?? status.trades_now ?? 0;
  const tradesRequired = isAbTest
    ? (abProgress?.trades_required_per_arm ?? 0) * 2
    : progress?.trades_required ?? status.config.min_trades_per_step ?? 0;
  const pct = tradesRequired > 0 ? Math.min(100, (tradesDone / tradesRequired) * 100) : 0;

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
          <Button
            variant="outline"
            size="sm"
            disabled={isPending}
            onClick={() => runAction("run")}
          >
            <Activity className="mr-1.5 h-3.5 w-3.5" />
            Esegui step
          </Button>
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
          <span>
            Trade accumulati nello step{" "}
            <span className="font-mono text-fg-2">
              {tradesDone}/{tradesRequired || "—"}
            </span>
          </span>
          {tradesRequired > 0 && (
            <span className="font-mono text-fg-2">{pct.toFixed(0)}%</span>
          )}
        </div>
        <Progress value={pct} />
      </div>
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
