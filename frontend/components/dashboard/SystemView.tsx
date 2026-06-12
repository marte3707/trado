"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useMemo, useState } from "react";

import { DecisionLog, Strategy, apiPatch } from "@/lib/api";
import {
  INPUT_CLS,
  TF_ORDER,
  formatDate,
  logBorderClass,
  logEventColor,
  relativeTime,
} from "@/components/dashboard/helpers";

interface SystemViewProps {
  strategies: Strategy[];
  onStrategiesChange: (updater: (current: Strategy[]) => Strategy[]) => void;
  logs: DecisionLog[];
  runAction: (label: string, action: () => Promise<void>) => void;
}

export function SystemView({ strategies, onStrategiesChange, logs, runAction }: SystemViewProps) {
  const [collapsedTfs, setCollapsedTfs] = useState<Record<string, boolean>>({});

  function toggleTf(tf: string) {
    setCollapsedTfs((prev) => ({ ...prev, [tf]: !prev[tf] }));
  }

  const strategiesByTf = useMemo(() => {
    const groups: Record<string, Strategy[]> = {};
    for (const s of strategies) {
      if (!groups[s.timeframe]) groups[s.timeframe] = [];
      groups[s.timeframe].push(s);
    }
    const sorted: Record<string, Strategy[]> = {};
    const allTfs = [
      ...TF_ORDER.filter((tf) => tf in groups),
      ...Object.keys(groups).filter((tf) => !TF_ORDER.includes(tf))
    ];
    for (const tf of allTfs) sorted[tf] = groups[tf];
    return sorted;
  }, [strategies]);

  return (
    <div className="grid grid-cols-2 gap-4 mb-5 max-md:grid-cols-1">

      {/* Strategie raggruppate per TF */}
      <div className="bg-surface rounded-2xl shadow-card overflow-hidden">
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-divider">
          <h2 className="text-sm font-semibold text-text">Strategie</h2>
          <span className="text-xs text-muted">
            {strategies.filter((s) => s.enabled).length}/{strategies.length} abilitate
          </span>
        </div>
        <div className="divide-y divide-divider/40">
          {Object.keys(strategiesByTf).length === 0 && (
            <p className="text-sm text-muted text-center py-6">Nessuna strategia configurata</p>
          )}
          {Object.entries(strategiesByTf).map(([tf, group]) => {
            const isCollapsed = collapsedTfs[tf];
            const enabledCount = group.filter((s) => s.enabled).length;
            return (
              <div key={tf}>
                <button
                  className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-white/3 transition-colors text-left"
                  onClick={() => toggleTf(tf)}
                >
                  <div className="flex items-center gap-2">
                    {isCollapsed
                      ? <ChevronRight size={12} className="text-muted/60 shrink-0" />
                      : <ChevronDown size={12} className="text-muted/60 shrink-0" />
                    }
                    <span className="text-xs font-bold text-text tracking-wide">{tf}</span>
                    <span className="text-xs text-muted/60">{group.length} str.</span>
                  </div>
                  <span className={`text-xs font-semibold ${enabledCount > 0 ? "text-green" : "text-muted/50"}`}>
                    {enabledCount}/{group.length}
                  </span>
                </button>
                {!isCollapsed && (
                  <div className="grid gap-1 px-3 pb-3 pt-1">
                    {group.map((strategy) => (
                      <div
                        key={strategy.id}
                        className="grid grid-cols-[1fr_52px] items-center gap-2 px-2.5 py-2 rounded-xl bg-surface-2"
                      >
                        <label className="flex items-center gap-2 min-w-0 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={strategy.enabled}
                            onChange={() =>
                              runAction(`strategy-${strategy.id}`, () =>
                                apiPatch(`/api/strategies/${strategy.id}`, {
                                  enabled: !strategy.enabled
                                }).then(() => undefined)
                              )
                            }
                            className="w-3.5 h-3.5 shrink-0 accent-blue cursor-pointer"
                          />
                          <span
                            className={`text-xs truncate ${strategy.enabled ? "text-text" : "text-muted/60"}`}
                          >
                            {strategy.strategy_name}
                          </span>
                        </label>
                        <input
                          type="number"
                          min="0"
                          max="100"
                          value={strategy.weight}
                          title={`Peso ${strategy.strategy_name}`}
                          aria-label={`Peso ${strategy.strategy_name}`}
                          onChange={(e) =>
                            onStrategiesChange((current) =>
                              current.map((item) =>
                                item.id === strategy.id
                                  ? { ...item, weight: Number(e.target.value) }
                                  : item
                              )
                            )
                          }
                          onBlur={(e) =>
                            runAction(`weight-${strategy.id}`, () =>
                              apiPatch(`/api/strategies/${strategy.id}`, {
                                weight: Number(e.currentTarget.value)
                              }).then(() => undefined)
                            )
                          }
                          className={`w-full text-center ${INPUT_CLS}`}
                        />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Log */}
      <div className="bg-surface rounded-2xl shadow-card overflow-hidden">
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-divider">
          <h2 className="text-sm font-semibold text-text">Log</h2>
          <span className="text-xs text-muted">{logs.length} eventi</span>
        </div>
        <div className="grid gap-1.5 p-3">
          {logs.length === 0 && (
            <p className="text-sm text-muted text-center py-6">Nessun evento registrato</p>
          )}
          {logs.slice(0, 16).map((log) => (
            <div
              key={log.id}
              className={`grid grid-cols-[1fr_auto] gap-x-2 gap-y-0.5 px-2.5 py-2 rounded-lg bg-surface-2 border-l-2 ${logBorderClass(log.event_type)}`}
            >
              <strong className={`text-xs font-semibold truncate ${logEventColor(log.event_type)}`}>
                {log.event_type}
              </strong>
              <span
                className="text-[10px] text-muted text-right cursor-default whitespace-nowrap"
                title={formatDate(log.timestamp)}
              >
                {relativeTime(log.timestamp)}
              </span>
              <p className="col-span-2 text-xs text-muted m-0 leading-relaxed">{log.message}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
