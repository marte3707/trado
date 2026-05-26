"use client";

import { useState } from "react";
import { Power, ShieldOff } from "lucide-react";

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
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSettings } from "@/lib/hooks/queries";
import { useKillSwitch } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";

export function KillSwitchButton() {
  const { data: settings } = useSettings();
  const { mutate: toggleKill, isPending } = useKillSwitch();
  const [open, setOpen] = useState(false);

  const armed = Boolean(settings?.kill_switch?.enabled);

  const handleConfirm = () => {
    toggleKill(!armed, { onSuccess: () => setOpen(false) });
  };

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <Tooltip>
        <TooltipTrigger asChild>
          <AlertDialogTrigger asChild>
            <button
              className={cn(
                "inline-flex h-8 items-center gap-2 rounded-md border px-3 font-mono text-xs transition-colors",
                armed
                  ? "border-danger/40 bg-danger/10 text-danger"
                  : "border-border bg-bg-1 text-fg-2 hover:border-danger/40 hover:text-danger",
              )}
              disabled={isPending}
              aria-label={armed ? "Disarma kill switch" : "Arma kill switch"}
            >
              {armed ? <ShieldOff className="h-3.5 w-3.5" /> : <Power className="h-3.5 w-3.5" />}
              <span>{armed ? "HALTED" : "KILL"}</span>
            </button>
          </AlertDialogTrigger>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {armed
            ? "Kill switch attivo — clicca per disarmare"
            : "Arma il kill switch (blocca live trading)"}
        </TooltipContent>
      </Tooltip>

      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {armed ? "Disarma il kill switch?" : "Armare il kill switch?"}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {armed
              ? "Il live trading tornera' operativo. Tutti i guardrail residui restano attivi (modalita', credenziali, capi)."
              : "Verra' bloccata l'apertura di nuove posizioni live finche' non disarmi. Le posizioni gia' aperte non vengono toccate."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={isPending}>Annulla</AlertDialogCancel>
          <AlertDialogAction
            onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
              e.preventDefault();
              handleConfirm();
            }}
            disabled={isPending}
            className={cn(armed ? "" : "bg-danger text-destructive-foreground hover:brightness-105")}
          >
            {armed ? "Disarma" : "Arma kill switch"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
