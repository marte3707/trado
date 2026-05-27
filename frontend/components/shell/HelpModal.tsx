"use client";

import { useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { NAV_HOTKEY_TO_ROUTE } from "@/lib/hotkeys";
import type { Route } from "@/components/shell/routes";
import { ROUTE_MAP } from "@/components/shell/routes";

interface Props {
  open: boolean;
  onClose: () => void;
}

const NAV_ROWS = Object.entries(NAV_HOTKEY_TO_ROUTE).map(([key, route]) => ({
  keys: ["g", key.toUpperCase()],
  label: ROUTE_MAP[route as Route]?.label ?? route,
}));

const GLOBAL_SHORTCUTS = [
  { keys: ["⌘", "K"], label: "Apri Command Menu" },
  { keys: ["?"], label: "Mostra questo help" },
  { keys: ["Esc"], label: "Chiudi pannelli / drawer" },
];

const ACTION_SHORTCUTS = [
  { keys: ["g", "D"], label: "Dashboard" },
  { keys: ["g", "S"], label: "Segnali" },
  { keys: ["g", "M"], label: "Mercati / Grafico" },
  { keys: ["g", "A"], label: "Account" },
  { keys: ["g", "H"], label: "Storico" },
  { keys: ["g", "C"], label: "Calibration" },
  { keys: ["g", ","], label: "Impostazioni" },
];

export function HelpModal({ open, onClose }: Props) {
  return (
    <Dialog open={open} onOpenChange={(v: boolean) => { if (!v) onClose(); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Scorciatoie da tastiera</DialogTitle>
        </DialogHeader>


        <div className="space-y-5 pt-2">
          <Section title="Globali" rows={GLOBAL_SHORTCUTS} />
          <Section title="Navigazione (g + tasto)" rows={ACTION_SHORTCUTS} />
        </div>

        <p className="mt-4 text-[11px] text-fg-3">
          Le scorciatoie di navigazione non funzionano quando il focus è su un input.
        </p>
      </DialogContent>
    </Dialog>
  );
}

function Section({ title, rows }: { title: string; rows: { keys: string[]; label: string }[] }) {
  return (
    <div>
      <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</div>
      <div className="space-y-1">
        {rows.map((r, i) => (
          <div key={i} className="flex items-center justify-between gap-4">
            <span className="text-sm text-fg-2">{r.label}</span>
            <span className="flex items-center gap-1">
              {r.keys.map((k, j) => (
                <kbd
                  key={j}
                  className="inline-flex min-w-6 items-center justify-center rounded border border-border bg-bg-2 px-1.5 py-0.5 font-mono text-[11px] text-fg-2"
                >
                  {k}
                </kbd>
              ))}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
