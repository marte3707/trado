"use client";

import { useEffect, useState } from "react";
import { Save } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useUpdateSettings } from "@/lib/hooks/mutations";
import type { AppSettings } from "@/lib/api";

interface Props {
  settings: AppSettings;
}

function toNum(v: unknown, fallback: number) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export function PaperTab({ settings }: Props) {
  const paper = (settings.paper ?? {}) as Record<string, unknown>;

  const persisted = {
    equity: toNum(paper.equity, 10_000),
    leverage: toNum(paper.leverage, 1),
    slippagePct: toNum(paper.slippage_pct, 0.05),
  };

  const [local, setLocal] = useState(persisted);
  const { mutate: updateSettings, isPending } = useUpdateSettings();

  useEffect(() => {
    setLocal(persisted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persisted.equity, persisted.leverage, persisted.slippagePct]);

  const dirty =
    local.equity !== persisted.equity ||
    local.leverage !== persisted.leverage ||
    local.slippagePct !== persisted.slippagePct;

  function handleSave() {
    updateSettings({
      paper: {
        value: {
          ...paper,
          equity: local.equity,
          leverage: local.leverage,
          slippage_pct: local.slippagePct,
        },
      },
    } as Partial<AppSettings>);
  }

  return (
    <div className="space-y-5">
      <Card className="p-6">
        <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Equity virtuale
        </Label>
        <div className="mt-2 flex items-center gap-2">
          <Input
            type="number"
            value={local.equity}
            step={100}
            onChange={(e) => setLocal((s) => ({ ...s, equity: Number(e.target.value) }))}
            className="font-mono"
          />
          <span className="font-mono text-sm text-fg-3">USDC</span>
        </div>
        <div className="mt-1.5 text-[11px] text-fg-3">
          Capitale base usato dalle posizioni paper per calcolare size e PnL.
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card className="p-5">
          <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Leverage default
          </Label>
          <div className="mt-2 flex items-center gap-2">
            <Input
              type="number"
              value={local.leverage}
              step={0.5}
              min={1}
              onChange={(e) => setLocal((s) => ({ ...s, leverage: Number(e.target.value) }))}
              className="font-mono"
            />
            <span className="font-mono text-sm text-fg-3">x</span>
          </div>
          <div className="mt-1.5 text-[11px] text-fg-3">
            Leva applicata di default alle posizioni paper.
          </div>
        </Card>

        <Card className="p-5">
          <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Slippage simulato
          </Label>
          <div className="mt-2 flex items-center gap-2">
            <Input
              type="number"
              value={local.slippagePct}
              step={0.01}
              onChange={(e) => setLocal((s) => ({ ...s, slippagePct: Number(e.target.value) }))}
              className="font-mono"
            />
            <span className="font-mono text-sm text-fg-3">%</span>
          </div>
          <div className="mt-1.5 text-[11px] text-fg-3">
            Slippage % stimato applicato all&apos;entry/exit nelle simulazioni.
          </div>
        </Card>
      </div>

      {dirty && (
        <Card className="sticky bottom-4 z-10 flex items-center gap-3 border-warn/40 bg-warn/10 px-5 py-3">
          <span className="text-sm text-warn">Modifiche non salvate</span>
          <div className="flex-1" />
          <Button size="sm" onClick={handleSave} disabled={isPending}>
            <Save className="mr-1.5 h-3.5 w-3.5" />
            {isPending ? "Salvataggio..." : "Salva paper config"}
          </Button>
        </Card>
      )}
    </div>
  );
}
