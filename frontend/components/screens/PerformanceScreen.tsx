"use client";

import { useMemo, useState } from "react";

import { Seg } from "@/components/atoms";
import type { Health } from "@/lib/api";
import { useAccountMonitor, usePositionHistory, useSettings } from "@/lib/hooks/queries";
import { asNumber, DEFAULT_BASE_EQUITY } from "@/lib/performance";
import { EquityCurveCard } from "@/components/performance/EquityCurveCard";
import { DrawdownCard } from "@/components/performance/DrawdownCard";
import { PnlDistributionCard } from "@/components/performance/PnlDistributionCard";
import { TradesPerDayCard } from "@/components/performance/TradesPerDayCard";
import { MonthlyReturnCard } from "@/components/performance/MonthlyReturnCard";

type Mode = "paper" | "live";
type Period = "30d" | "90d" | "all";

const PERIOD_OPTIONS: [string, string][] = [
  ["30d", "30d"],
  ["90d", "90d"],
  ["all", "All"],
];

function defaultMode(health: Health): Mode {
  return health.mode === "AUTO_TRADE" ? "live" : "paper";
}

export function PerformanceScreen({ health }: { health: Health }) {
  const [mode, setMode] = useState<Mode>(() => defaultMode(health));
  const [period, setPeriod] = useState<Period>("all");

  // 90d non è un periodo nativo dell'endpoint: chiediamo "all" e fa fede il client.
  const apiPeriod = period === "90d" ? "all" : period;
  const history = usePositionHistory({ mode, period: apiPeriod, market_type: "all", limit: 5000 });
  const settings = useSettings();
  const account = useAccountMonitor();

  const positions = useMemo(() => {
    const rows = history.data?.positions ?? [];
    if (period !== "90d") return rows;
    const cutoff = Date.now() - 90 * 24 * 60 * 60 * 1000;
    return rows.filter((p) => {
      const ts = p.closed_at ? new Date(p.closed_at).getTime() : NaN;
      return Number.isFinite(ts) && ts >= cutoff;
    });
  }, [history.data, period]);

  // Equity di partenza: paper da settings, live ricostruita dallo snapshot.
  const baseEquity = useMemo(() => {
    if (mode === "paper") {
      const paper = (settings.data?.paper ?? {}) as Record<string, unknown>;
      return asNumber(paper.equity) ?? DEFAULT_BASE_EQUITY;
    }
    const current = asNumber(account.data?.account.effective_account_value);
    if (current == null) return DEFAULT_BASE_EQUITY;
    const totalPnl = positions.reduce((s, p) => s + (asNumber(p.realized_pnl) ?? 0), 0);
    const start = current - totalPnl;
    return start > 0 ? start : DEFAULT_BASE_EQUITY;
  }, [mode, settings.data, account.data, positions]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-fg">Performance</h1>
          <p className="text-xs text-fg-3">
            Equity, drawdown e distribuzione dei trade chiusi · base {mode} ≈{" "}
            <span className="font-mono">{Math.round(baseEquity).toLocaleString("en-US")} USD</span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Seg
            value={mode}
            onChange={(v) => setMode(v as Mode)}
            options={[
              ["paper", "Paper"],
              ["live", "Live"],
            ]}
          />
          <Seg value={period} onChange={(v) => setPeriod(v as Period)} options={PERIOD_OPTIONS} />
        </div>
      </div>

      {history.isError ? (
        <div className="grid h-40 place-items-center rounded-lg border border-border text-xs text-danger">
          Errore nel caricamento dello storico posizioni.
        </div>
      ) : (
        <>
          <EquityCurveCard positions={positions} baseEquity={baseEquity} />
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <DrawdownCard positions={positions} baseEquity={baseEquity} />
            <PnlDistributionCard positions={positions} />
            <TradesPerDayCard positions={positions} />
            <MonthlyReturnCard positions={positions} baseEquity={baseEquity} />
          </div>
        </>
      )}
    </div>
  );
}
