"use client";

import { useEffect, useState } from "react";
import { ArrowRight, Power, RefreshCcw } from "lucide-react";

import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command";
import { useMarkets, useSettings, useSignals } from "@/lib/hooks/queries";
import { useAutoTuner, useKillSwitch } from "@/lib/hooks/mutations";
import { ROUTES, ROUTE_MAP, type Route } from "@/components/shell/routes";
import type { Market } from "@/lib/api";

export interface CommandMenuProps {
  setRoute: (r: Route) => void;
  onOpenMarket: (market: Market, timeframe?: string) => void;
}

export function CommandMenu({ setRoute, onOpenMarket }: CommandMenuProps) {
  const [open, setOpen] = useState(false);

  const { data: markets } = useMarkets();
  const { data: signals } = useSignals("min_score=70&limit=30");
  const { data: settings } = useSettings();
  const { mutate: toggleKill } = useKillSwitch();
  const { mutate: runTuner } = useAutoTuner();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const runCommand = (fn: () => void) => {
    setOpen(false);
    setTimeout(fn, 50);
  };

  const killArmed = Boolean(settings?.kill_switch?.enabled);
  const topMarkets = (markets ?? []).slice(0, 30);
  const topSignals = (signals ?? []).slice(0, 8);

  return (
    <CommandDialog open={open} onOpenChange={setOpen}>
      <CommandInput placeholder="Cerca un mercato, naviga o esegui un'azione..." />
      <CommandList>
        <CommandEmpty>Nessun risultato.</CommandEmpty>

        <CommandGroup heading="Naviga">
          {ROUTES.map((r) => {
            const Icon = r.icon;
            return (
              <CommandItem
                key={r.id}
                value={`naviga ${r.label} ${r.description}`}
                onSelect={() => runCommand(() => setRoute(r.id))}
              >
                <Icon className="text-fg-3" />
                <span>{r.label}</span>
                <span className="ml-2 text-xs text-fg-4">{r.description}</span>
              </CommandItem>
            );
          })}
        </CommandGroup>

        {topSignals.length > 0 && (
          <CommandGroup heading="Segnali recenti">
            {topSignals.map((s) => (
              <CommandItem
                key={s.id}
                value={`segnale ${s.market?.symbol ?? ""} ${s.timeframe} ${s.direction}`}
                onSelect={() =>
                  runCommand(() => {
                    if (s.market) onOpenMarket(s.market, s.timeframe);
                  })
                }
              >
                <ArrowRight className="text-fg-3" />
                <span className="font-mono text-xs">{s.market?.symbol ?? "—"}</span>
                <span className="ml-2 text-xs text-fg-3">{s.timeframe}</span>
                <span className="ml-2 text-xs text-fg-2">{s.direction}</span>
                <CommandShortcut>score {s.score}</CommandShortcut>
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        {topMarkets.length > 0 && (
          <CommandGroup heading="Mercati">
            {topMarkets.map((m) => (
              <CommandItem
                key={`${m.market_type}:${m.symbol}`}
                value={`mercato ${m.symbol} ${m.market_type}`}
                onSelect={() => runCommand(() => onOpenMarket(m))}
              >
                <span className="font-mono text-xs">{m.symbol}</span>
                <span className="ml-auto text-[10px] uppercase text-fg-3">{m.market_type}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        <CommandGroup heading="Azioni">
          <CommandItem
            value={`kill switch ${killArmed ? "disarma" : "arma"}`}
            onSelect={() => runCommand(() => toggleKill(!killArmed))}
          >
            <Power className={killArmed ? "text-success" : "text-danger"} />
            {killArmed ? "Disarma kill switch" : "Arma kill switch"}
          </CommandItem>
          <CommandItem
            value="run auto-tuner step"
            onSelect={() => runCommand(() => runTuner("run"))}
          >
            <RefreshCcw className="text-fg-3" />
            Esegui auto-tuner
          </CommandItem>
          <CommandItem
            value="apri settings"
            onSelect={() => runCommand(() => setRoute("settings"))}
          >
            <ROUTE_MAP.settings.icon className="text-fg-3" />
            Apri Settings
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
