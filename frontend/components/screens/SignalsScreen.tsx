"use client";

import { useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowDown, ArrowUp, ChevronsUpDown, Download, RefreshCw, Search } from "lucide-react";

import {
  DirBadge,
  MarketBadge,
  ScoreChip,
  StatusBadge,
  StratList,
  TfChip,
  fmt,
} from "@/components/atoms";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  ToggleGroup,
  ToggleGroupItem,
} from "@/components/ui/toggle-group";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Slider } from "@/components/ui/slider";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { SignalAnatomyDrawer } from "@/components/signals/SignalAnatomyDrawer";
import { usePositions } from "@/lib/hooks/queries";
import { cn } from "@/lib/utils";
import type { Health, Market, Signal, Strategy } from "@/lib/api";

interface Props {
  signals: Signal[];
  strategies: Strategy[];
  markets: Market[];
  health: Health;
  threshold: number;
  onRefresh: () => void;
  onOpenChart: (signal: Signal) => void;
}

type SortKey = "score" | "createdAt" | "rr" | "symbol" | "timeframe";
type SortDir = "asc" | "desc";

function asNumber(v: string | number | null | undefined): number | null {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function getSortValue(s: Signal, key: SortKey): number | string {
  switch (key) {
    case "score":
      return s.score;
    case "createdAt":
      return new Date(s.created_at).getTime();
    case "rr":
      return asNumber(s.risk_reward) ?? -Infinity;
    case "symbol":
      return s.market?.symbol ?? "";
    case "timeframe":
      return s.timeframe;
  }
}

export function SignalsScreen({
  signals,
  strategies,
  threshold,
  onRefresh,
  onOpenChart,
}: Props) {
  const searchParams = useSearchParams();
  const router = useRouter();

  function updateUrl(updates: Record<string, string | null>) {
    const params = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(updates)) {
      if (value === null || value === "") params.delete(key);
      else params.set(key, value);
    }
    router.replace(`?${params.toString()}`, { scroll: false } as Parameters<typeof router.replace>[1]);
  }

  const [search, setSearchRaw] = useState(() => searchParams.get("q") ?? "");
  const [marketType, setMarketTypeRaw] = useState<"all" | "spot" | "perp">(
    () => (searchParams.get("mt") as "all" | "spot" | "perp") ?? "all",
  );
  const [dir, setDirRaw] = useState<"all" | "LONG" | "SHORT">(
    () => (searchParams.get("dir") as "all" | "LONG" | "SHORT") ?? "all",
  );
  const [tfFilter, setTfFilterRaw] = useState<string>(() => searchParams.get("tf") ?? "all");
  const [statusFilter, setStatusFilterRaw] = useState<"all" | "new" | "executed" | "ignored">(
    () => (searchParams.get("status") as "all" | "new" | "executed" | "ignored") ?? "all",
  );
  const [strategyFilter, setStrategyFilterRaw] = useState<string>(
    () => searchParams.get("strat") ?? "all",
  );
  const [scoreMin, setScoreMinRaw] = useState<number>(() => {
    const v = searchParams.get("score");
    return v !== null ? Number(v) : threshold;
  });
  const [sortKey, setSortKeyRaw] = useState<SortKey>(
    () => (searchParams.get("sort") as SortKey) ?? "createdAt",
  );
  const [sortDir, setSortDirRaw] = useState<SortDir>(
    () => (searchParams.get("sortDir") as SortDir) ?? "desc",
  );

  function setSearch(v: string) { setSearchRaw(v); updateUrl({ q: v || null }); }
  function setMarketType(v: "all" | "spot" | "perp") { setMarketTypeRaw(v); updateUrl({ mt: v === "all" ? null : v }); }
  function setDir(v: "all" | "LONG" | "SHORT") { setDirRaw(v); updateUrl({ dir: v === "all" ? null : v }); }
  function setTfFilter(v: string) { setTfFilterRaw(v); updateUrl({ tf: v === "all" ? null : v }); }
  function setStatusFilter(v: "all" | "new" | "executed" | "ignored") { setStatusFilterRaw(v); updateUrl({ status: v === "all" ? null : v }); }
  function setStrategyFilter(v: string) { setStrategyFilterRaw(v); updateUrl({ strat: v === "all" ? null : v }); }
  function setScoreMin(v: number) { setScoreMinRaw(v); updateUrl({ score: v === threshold ? null : String(v) }); }

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const { data: allPositions = [] } = usePositions();

  const timeframes = useMemo(
    () => Array.from(new Set(signals.map((s) => s.timeframe))).sort(),
    [signals],
  );

  const strategyNames = useMemo(() => {
    const set = new Set<string>();
    strategies.forEach((s) => set.add(s.strategy_name));
    signals.forEach((s) => Object.keys(s.strategy_breakdown ?? {}).forEach((k) => set.add(k)));
    return Array.from(set).sort();
  }, [strategies, signals]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return signals.filter((s) => {
      if (q) {
        const haystack = `${s.market?.symbol ?? ""} ${s.market?.base ?? ""} ${s.market?.quote ?? ""} ${s.timeframe} ${s.direction} ${s.reason ?? ""}`.toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      if (marketType !== "all" && s.market?.market_type !== marketType) return false;
      if (dir !== "all") {
        const normalized = s.direction.includes("LONG") ? "LONG" : s.direction.includes("SHORT") ? "SHORT" : "NEUTRAL";
        if (normalized !== dir) return false;
      }
      if (tfFilter !== "all" && s.timeframe !== tfFilter) return false;
      if (statusFilter !== "all" && s.status !== statusFilter) return false;
      if (s.score < scoreMin) return false;
      if (strategyFilter !== "all") {
        if (!Object.keys(s.strategy_breakdown ?? {}).includes(strategyFilter)) return false;
      }
      return true;
    });
  }, [signals, search, marketType, dir, tfFilter, statusFilter, scoreMin, strategyFilter]);

  const sorted = useMemo(() => {
    const arr = [...filtered];
    arr.sort((a, b) => {
      const av = getSortValue(a, sortKey);
      const bv = getSortValue(b, sortKey);
      let cmp = 0;
      if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
      else cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
      if (cmp === 0) return b.id - a.id;
      return sortDir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [filtered, sortKey, sortDir]);

  const selected = selectedId != null ? signals.find((s) => s.id === selectedId) ?? null : null;

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      const next: SortDir = sortDir === "asc" ? "desc" : "asc";
      setSortDirRaw(next);
      updateUrl({ sort: key === "createdAt" ? null : key, sortDir: next === "desc" ? null : next });
    } else {
      const next: SortDir = key === "symbol" || key === "timeframe" ? "asc" : "desc";
      setSortKeyRaw(key);
      setSortDirRaw(next);
      updateUrl({ sort: key === "createdAt" ? null : key, sortDir: next === "desc" ? null : next });
    }
  };

  function handleExportCsv() {
    const header = ["Data","Symbol","Timeframe","Direzione","Score","Status","Entry","SL","TP1","TP2","TP3","R/R"];
    const csvRows = sorted.map(s => [
      new Date(s.created_at).toISOString(),
      s.market?.symbol ?? "",
      s.timeframe,
      s.direction,
      s.score,
      s.status,
      s.entry_price ?? "",
      s.stop_loss ?? "",
      s.take_profit_1 ?? "",
      s.take_profit_2 ?? "",
      s.take_profit_3 ?? "",
      s.risk_reward ?? "",
    ]);
    const content = [header, ...csvRows].map(row => row.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join(",")).join("\n");
    const blob = new Blob(["﻿" + content], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `trado_signals_${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-4 p-6">
      {/* Header */}
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Segnali live</h1>
          <p className="mt-1 text-xs text-fg-3">
            {sorted.length} di {signals.length} segnali · soglia globale ≥ {threshold}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={handleExportCsv} title="Esporta segnali filtrati come CSV">
            <Download className="mr-1.5 h-3.5 w-3.5" />
            Export CSV
          </Button>
          <Button variant="outline" size="sm" onClick={onRefresh}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            Aggiorna
          </Button>
        </div>
      </div>

      {/* Filtri */}
      <Card className="flex flex-wrap items-center gap-3 p-3">
        <div className="relative min-w-[220px] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-3" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Cerca symbol, direction, reason..."
            className="pl-8"
          />
        </div>

        <ToggleGroup
          type="single"
          value={marketType}
          onValueChange={(v: string) => v && setMarketType(v as "all" | "spot" | "perp")}
          size="sm"
        >
          <ToggleGroupItem value="all">Tutti</ToggleGroupItem>
          <ToggleGroupItem value="spot">Spot</ToggleGroupItem>
          <ToggleGroupItem value="perp">Perp</ToggleGroupItem>
        </ToggleGroup>

        <ToggleGroup
          type="single"
          value={dir}
          onValueChange={(v: string) => v && setDir(v as "all" | "LONG" | "SHORT")}
          size="sm"
        >
          <ToggleGroupItem value="all">All dir</ToggleGroupItem>
          <ToggleGroupItem value="LONG">Long</ToggleGroupItem>
          <ToggleGroupItem value="SHORT">Short</ToggleGroupItem>
        </ToggleGroup>

        <Popover>
          <PopoverTrigger asChild>
            <Button variant="outline" size="sm">
              Score ≥ {scoreMin}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-64">
            <div className="space-y-3">
              <div className="text-xs font-medium text-fg-2">Score minimo</div>
              <Slider
                value={[scoreMin]}
                onValueChange={(v: number[]) => setScoreMin(v[0])}
                min={0}
                max={100}
                step={5}
              />
              <div className="flex justify-between font-mono text-[10px] text-fg-4">
                <span>0</span>
                <span>{scoreMin}</span>
                <span>100</span>
              </div>
            </div>
          </PopoverContent>
        </Popover>

        <select
          value={tfFilter}
          onChange={(e) => setTfFilter(e.target.value)}
          className="h-8 rounded-md border border-border bg-bg-1 px-2 text-xs text-fg-2"
        >
          <option value="all">Tutti i TF</option>
          {timeframes.map((tf) => (
            <option key={tf} value={tf}>
              {tf}
            </option>
          ))}
        </select>

        <select
          value={strategyFilter}
          onChange={(e) => setStrategyFilter(e.target.value)}
          className="h-8 rounded-md border border-border bg-bg-1 px-2 text-xs text-fg-2"
        >
          <option value="all">Tutte le strategie</option>
          {strategyNames.map((sn) => (
            <option key={sn} value={sn}>
              {sn}
            </option>
          ))}
        </select>

        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as "all" | "new" | "executed" | "ignored")}
          className="h-8 rounded-md border border-border bg-bg-1 px-2 text-xs text-fg-2"
        >
          <option value="all">Tutti gli stati</option>
          <option value="new">New</option>
          <option value="executed">Executed</option>
          <option value="ignored">Ignored</option>
        </select>
      </Card>

      {/* Tabella */}
      <Card className="flex h-[calc(100vh-240px)] flex-col overflow-hidden">
        <ScrollArea className="flex-1">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-bg-1">
              <TableRow>
                <Th sortKey="symbol" current={sortKey} dir={sortDir} onClick={toggleSort}>
                  Symbol
                </Th>
                <Th sortKey="timeframe" current={sortKey} dir={sortDir} onClick={toggleSort}>
                  TF
                </Th>
                <TableHead>Dir</TableHead>
                <Th sortKey="score" current={sortKey} dir={sortDir} onClick={toggleSort} className="text-right">
                  Score
                </Th>
                <TableHead className="text-right">Entry</TableHead>
                <TableHead className="text-right">SL</TableHead>
                <TableHead className="text-right">TP1</TableHead>
                <Th sortKey="rr" current={sortKey} dir={sortDir} onClick={toggleSort} className="text-right">
                  R:R
                </Th>
                <TableHead>Strategie</TableHead>
                <Th sortKey="createdAt" current={sortKey} dir={sortDir} onClick={toggleSort}>
                  Età
                </Th>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={11} className="py-12 text-center text-sm text-fg-3">
                    Nessun segnale corrisponde ai filtri.
                  </TableCell>
                </TableRow>
              ) : (
                sorted.map((s) => {
                  const isSelected = s.id === selectedId;
                  return (
                    <TableRow
                      key={s.id}
                      data-state={isSelected ? "selected" : undefined}
                      className={cn(
                        "cursor-pointer",
                        isSelected && "border-l-2 border-l-success bg-bg-2",
                      )}
                      onClick={() => setSelectedId(s.id)}
                    >
                      <TableCell className="font-mono font-medium text-fg">
                        <div className="flex items-center gap-1.5">
                          {s.market?.symbol ?? "—"}
                          {s.market && <MarketBadge market={s.market.market_type} />}
                        </div>
                      </TableCell>
                      <TableCell>
                        <TfChip tf={s.timeframe} />
                      </TableCell>
                      <TableCell>
                        <DirBadge dir={s.direction} />
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="inline-flex">
                          <ScoreChip score={s.score} />
                        </div>
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {fmt.price(s.entry_price)}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums text-danger">
                        {fmt.price(s.stop_loss)}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums text-success">
                        {fmt.price(s.take_profit_1)}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {asNumber(s.risk_reward)?.toFixed(2) ?? "—"}
                      </TableCell>
                      <TableCell>
                        <StratList strats={Object.keys(s.strategy_breakdown ?? {})} max={2} />
                      </TableCell>
                      <TableCell className="font-mono text-xs text-fg-3">
                        {fmt.relTime(s.created_at)}
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={s.status} />
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
        </ScrollArea>
        <div className="flex shrink-0 items-center justify-between border-t border-border px-4 py-2 text-xs text-fg-3">
          <span>
            <Badge variant="outline" className="mr-2 font-mono">
              {sorted.length}
            </Badge>
            segnali filtrati · click su una riga per Signal Anatomy
          </span>
          <span className="font-mono">
            Sort: {sortKey} {sortDir}
          </span>
        </div>
      </Card>

      <SignalAnatomyDrawer
        signal={selected}
        positions={allPositions}
        onClose={() => setSelectedId(null)}
        onOpenChart={onOpenChart}
      />
    </div>
  );
}

function Th({
  sortKey,
  current,
  dir,
  onClick,
  children,
  className,
}: {
  sortKey: SortKey;
  current: SortKey;
  dir: SortDir;
  onClick: (k: SortKey) => void;
  children: React.ReactNode;
  className?: string;
}) {
  const active = current === sortKey;
  return (
    <TableHead className={className}>
      <button
        onClick={() => onClick(sortKey)}
        className={cn(
          "inline-flex items-center gap-1 transition-colors hover:text-fg",
          active && "text-fg",
        )}
      >
        {children}
        {active ? (
          dir === "asc" ? (
            <ArrowUp className="h-3 w-3" />
          ) : (
            <ArrowDown className="h-3 w-3" />
          )
        ) : (
          <ChevronsUpDown className="h-3 w-3 opacity-40" />
        )}
      </button>
    </TableHead>
  );
}
