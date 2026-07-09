"use client";

import { FormEvent, useMemo, useState } from "react";
import { AlertTriangle, Eye, RefreshCcw, ShieldCheck, TrendingUp } from "lucide-react";

import {
  useCreateLeaderboardTrader,
  useImportLeaderboardTopTraders,
  useRunLeaderboardCopyPaper,
  useSyncLeaderboardTrader,
  useSyncLeaderboardWatchlist,
  useUpdateLeaderboardTrader,
} from "@/lib/hooks/mutations";
import { useLeaderboardAlpha } from "@/lib/hooks/queries";
import type {
  LeaderboardAlphaClassification,
  LeaderboardAlphaFill,
  LeaderboardAlphaSignal,
  LeaderboardAlphaTrader,
  LeaderboardCopyLive,
  LeaderboardCopyLiveDryRunOrder,
  LeaderboardCopyLiveTrader,
  LeaderboardCopyPaperPosition,
  LeaderboardCopyPaperRanking,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const CLASS_LABEL: Record<LeaderboardAlphaClassification, string> = {
  candidate: "Candidate",
  watch: "Watch",
  quarantine: "Quarantine",
  insufficient: "Insufficient",
};

function asNumber(value: string | number | null | undefined): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function fmtUsd(value: string | number | null | undefined): string {
  const n = asNumber(value);
  if (n == null) return "—";
  const digits = Math.abs(n) < 10 ? 2 : 0;
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: digits,
  });
}

function fmtPct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}%`;
}

function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short" });
}

function shortAddress(address: string | null | undefined): string {
  if (!address) return "—";
  return `${address.slice(0, 6)}…${address.slice(-4)}`;
}

function classTone(classification: string | null | undefined): string {
  if (classification === "candidate") return "border-success/30 bg-success/10 text-success";
  if (classification === "watch") return "border-warn/30 bg-warn/10 text-warn";
  if (classification === "quarantine") return "border-danger/30 bg-danger/10 text-danger";
  return "border-border bg-bg-2 text-fg-3";
}

function directionTone(direction: string | null | undefined): string {
  return direction === "long" ? "text-success" : direction === "short" ? "text-danger" : "text-fg-2";
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function leaderboardMeta(trader: LeaderboardAlphaTrader): Record<string, unknown> | null {
  return recordValue(trader.metadata_json?.leaderboard);
}

function metaNumber(meta: Record<string, unknown> | null, key: string): number | null {
  const value = meta?.[key];
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") return asNumber(value);
  return null;
}

export function LeaderboardAlphaScreen() {
  const { data, isLoading, error, refetch, isFetching } = useLeaderboardAlpha();
  const createTrader = useCreateLeaderboardTrader();
  const importTopTraders = useImportLeaderboardTopTraders();
  const runCopyPaper = useRunLeaderboardCopyPaper();
  const updateTrader = useUpdateLeaderboardTrader();
  const syncTrader = useSyncLeaderboardTrader();
  const syncWatchlist = useSyncLeaderboardWatchlist();
  const [address, setAddress] = useState("");
  const [label, setLabel] = useState("");

  const sortedTraders = useMemo(() => {
    return [...(data?.traders ?? [])].sort((a, b) => {
      const sa = a.latest_snapshot?.quality_score ?? -1;
      const sb = b.latest_snapshot?.quality_score ?? -1;
      return Number(b.enabled) - Number(a.enabled) || sb - sa || a.address.localeCompare(b.address);
    });
  }, [data?.traders]);

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    createTrader.mutate(
      { address: address.trim(), label: label.trim() || undefined },
      {
        onSuccess: () => {
          setAddress("");
          setLabel("");
        },
      },
    );
  };

  if (isLoading) {
    return <div className="p-6 text-sm text-fg-3">Caricamento leaderboard alpha…</div>;
  }
  if (error || !data) {
    return (
      <div className="p-6 text-sm text-danger">
        Errore nel caricamento leaderboard alpha.{" "}
        <button onClick={() => refetch()} className="underline">
          Riprova
        </button>
      </div>
    );
  }

  const syncing =
    syncWatchlist.isPending ||
    syncTrader.isPending ||
    importTopTraders.isPending ||
    runCopyPaper.isPending ||
    isFetching;
  const lastAutoImportAt = typeof data.state.last_auto_import_at === "string" ? data.state.last_auto_import_at : null;
  const lastAutoSyncAt = typeof data.state.last_auto_sync_at === "string" ? data.state.last_auto_sync_at : null;

  return (
    <div className="flex flex-col gap-4 p-6">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-success">
            <Eye className="h-4 w-4" />
            Observe only
          </div>
          <h1 className="mt-1 text-xl font-semibold">Leaderboard Alpha</h1>
          <p className="max-w-3xl text-xs text-fg-3">
            Watchlist separata per address Hyperliquid: sincronizza fill pubblici,
            valuta qualità e genera solo conferme operative. Nessun ordine automatico.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => importTopTraders.mutate()}
            disabled={syncing}
            className="rounded-md border border-border-strong px-3 py-1.5 text-xs hover:bg-bg-hover disabled:opacity-50"
          >
            Import top 20 ROI 7d
          </button>
          <button
            onClick={() => runCopyPaper.mutate()}
            disabled={syncing}
            className="inline-flex items-center gap-1.5 rounded-md border border-success/40 bg-success/10 px-3 py-1.5 text-xs text-success hover:bg-success/15 disabled:opacity-50"
          >
            <TrendingUp className="h-3.5 w-3.5" />
            Run copy-paper
          </button>
          <button
            onClick={() => refetch()}
            disabled={syncing}
            className="rounded-md border border-border-strong px-3 py-1.5 text-xs hover:bg-bg-hover disabled:opacity-50"
          >
            Aggiorna UI
          </button>
          <button
            onClick={() => syncWatchlist.mutate()}
            disabled={syncing || data.traders.length === 0}
            className="inline-flex items-center gap-1.5 rounded-md border border-success/40 bg-success/10 px-3 py-1.5 text-xs text-success hover:bg-success/15 disabled:opacity-50"
          >
            <RefreshCcw className="h-3.5 w-3.5" />
            Sync watchlist
          </button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
        <Kpi label="Trader osservati" value={data.summary.traders} sub={`${data.summary.enabled_traders} attivi`} />
        <Kpi label="Candidate" value={data.summary.candidates} sub={`score min ${data.settings.min_quality_score}`} />
        <Kpi label="Alpha signal" value={data.summary.signals} sub={`ultime ${data.settings.recent_signal_hours}h`} />
        <Kpi label="Lookback" value={`${data.settings.lookback_days}d`} sub="fill pubblici HL" />
        <Kpi
          label="Auto sync"
          value={data.settings.auto_sync_enabled ? `${data.settings.auto_sync_interval_hours}h` : "off"}
          sub={lastAutoSyncAt ? `ultimo ${fmtDate(lastAutoSyncAt)}` : "in attesa"}
          compact
        />
        <Kpi label="Ultimo sync" value={fmtDate(data.summary.last_sync_at)} compact />
      </div>

      <div className="rounded-lg border border-border bg-bg-1 p-3 text-xs text-fg-3">
        Auto-import top {data.settings.auto_import_limit} per ROI {data.settings.auto_import_window} con volume &gt;{" "}
        {fmtUsd(data.settings.auto_import_min_volume_usd)}. Ultimo import: {fmtDate(lastAutoImportAt)}.
        Copy-paper separato: equity iniziale {fmtUsd(data.copy_paper.summary.starting_equity_usd)}, size{" "}
        {fmtUsd(data.copy_paper.summary.position_notional_usd)} per posizione.
      </div>

      <CopyPaperLab
        summary={data.copy_paper.summary}
        rankings={data.copy_paper.trader_rankings}
        openPositions={data.copy_paper.open_positions}
        recentClosed={data.copy_paper.recent_closed_positions}
      />

      <CopyLiveShadowPanel plan={data.copy_live} />

      <div className="rounded-lg border border-border bg-bg-1 p-4">
        <form onSubmit={onSubmit} className="grid gap-3 lg:grid-cols-[minmax(280px,1fr)_220px_auto]">
          <input
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            placeholder="0x… address dalla leaderboard HL"
            className="h-9 rounded-md border border-border bg-bg px-3 font-mono text-sm outline-none focus:border-success"
          />
          <input
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="Label opzionale"
            className="h-9 rounded-md border border-border bg-bg px-3 text-sm outline-none focus:border-success"
          />
          <button
            type="submit"
            disabled={createTrader.isPending || !address.trim()}
            className="h-9 rounded-md border border-border-strong px-3 text-sm hover:bg-bg-hover disabled:opacity-50"
          >
            Aggiungi
          </button>
        </form>
      </div>

      <Card title="Segnali di conferma">
        {data.signals.length === 0 ? (
          <div className="flex items-start gap-2 rounded-md border border-border bg-bg p-3 text-sm text-fg-3">
            <ShieldCheck className="mt-0.5 h-4 w-4 text-fg-4" />
            Nessun fill open recente dai trader abilitati. Il feed resta confirm-only:
            single-trader è debole, cluster multi-trader è conferma più forte.
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {data.signals.map((signal) => (
              <SignalCard key={`${signal.coin}:${signal.direction}`} signal={signal} />
            ))}
          </div>
        )}
      </Card>

      <Card title="Trader osservati">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-fg-3">
                <th className="pb-2">Trader</th>
                <th className="pb-2">Classe</th>
                <th className="pb-2 text-right">Score</th>
                <th className="pb-2 text-right">ROI 7d</th>
                <th className="pb-2 text-right">Vlm 7d</th>
                <th className="pb-2 text-right">Net PnL</th>
                <th className="pb-2 text-right">Win</th>
                <th className="pb-2 text-right">PF</th>
                <th className="pb-2 text-right">Fill</th>
                <th className="pb-2 text-right">Top coin</th>
                <th className="pb-2 text-right">Sync</th>
                <th className="pb-2 text-right">Azioni</th>
              </tr>
            </thead>
            <tbody>
              {sortedTraders.map((trader) => (
                <TraderRow
                  key={trader.id}
                  trader={trader}
                  onToggle={() => updateTrader.mutate({ id: trader.id, patch: { enabled: !trader.enabled } })}
                  onSync={() => syncTrader.mutate(trader.id)}
                  syncing={syncTrader.isPending}
                />
              ))}
              {sortedTraders.length === 0 && (
                <tr>
                  <td className="py-4 text-fg-3" colSpan={12}>
                    Importa i top 20 ROI 7d o aggiungi address dalla leaderboard Hyperliquid.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Fill recenti">
        <RecentFills fills={data.recent_fills} />
      </Card>
    </div>
  );
}

function CopyPaperLab({
  summary,
  rankings,
  openPositions,
  recentClosed,
}: {
  summary: {
    starting_equity_usd: string | null;
    equity_usd: string | null;
    realized_pnl_usdc: string | null;
    return_pct: number | null;
    open_positions: number;
    closed_positions: number;
    total_positions: number;
    win_rate_pct: number | null;
    profit_factor: number | null;
    avg_trade_pnl_usdc: string | null;
    total_fees_usdc: string | null;
    copied_traders: number;
    position_notional_usd: string;
    fee_rate: string;
  };
  rankings: LeaderboardCopyPaperRanking[];
  openPositions: LeaderboardCopyPaperPosition[];
  recentClosed: LeaderboardCopyPaperPosition[];
}) {
  const pnl = asNumber(summary.realized_pnl_usdc);
  return (
    <Card title="Copy-paper separato">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
        <Kpi label="Equity simulata" value={fmtUsd(summary.equity_usd)} sub={`start ${fmtUsd(summary.starting_equity_usd)}`} />
        <Kpi
          label="PnL realizzato"
          value={fmtUsd(summary.realized_pnl_usdc)}
          sub={fmtPct(summary.return_pct)}
          tone={pnl == null ? undefined : pnl >= 0 ? "success" : "danger"}
        />
        <Kpi label="Win rate" value={fmtPct(summary.win_rate_pct)} sub={`${summary.closed_positions} chiuse`} />
        <Kpi label="Profit factor" value={fmtNum(summary.profit_factor)} sub={`avg ${fmtUsd(summary.avg_trade_pnl_usdc)}`} />
        <Kpi label="Aperte" value={summary.open_positions} sub={`${summary.total_positions} totali`} />
        <Kpi label="Trader copiati" value={summary.copied_traders} sub={`fee ${summary.fee_rate}`} />
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[1.1fr_1fr]">
        <div>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-3">Ranking copy-paper</div>
          {rankings.length === 0 ? (
            <div className="rounded-md border border-border bg-bg p-3 text-sm text-fg-3">
              Nessuna posizione simulata ancora. Serve sync dei fill dei trader top.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wider text-fg-3">
                    <th className="pb-2">Trader</th>
                    <th className="pb-2 text-right">PnL</th>
                    <th className="pb-2 text-right">Win</th>
                    <th className="pb-2 text-right">PF</th>
                    <th className="pb-2 text-right">Open</th>
                    <th className="pb-2 text-right">Closed</th>
                  </tr>
                </thead>
                <tbody>
                  {rankings.slice(0, 8).map((row) => (
                    <CopyRankingRow key={row.trader_id} row={row} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-3">Ultime posizioni chiuse</div>
          <CopyPositionsTable positions={recentClosed.slice(0, 8)} empty="Nessuna posizione chiusa." />
        </div>
      </div>

      {openPositions.length > 0 && (
        <div className="mt-4">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-3">Posizioni copy-paper aperte</div>
          <CopyPositionsTable positions={openPositions.slice(0, 8)} empty="Nessuna posizione aperta." />
        </div>
      )}
    </Card>
  );
}

const LIVE_REASON_LABELS: Record<string, string> = {
  trader_disabled: "trader off",
  rank_outside_live_limit: "rank fuori limite",
  missing_snapshot: "snapshot mancante",
  classification_not_allowed: "classe non live",
  quality_below_live_min: "quality bassa",
  copy_paper_sample_too_small: "sample paper piccolo",
  profit_factor_below_live_min: "PF basso",
  win_rate_below_live_min: "win rate basso",
  avg_trade_pnl_below_live_min: "avg PnL basso",
  net_pnl_below_live_min: "net PnL basso",
  recent_sample_too_small: "sample recente piccolo",
  outside_live_trader_cap: "fuori cap trader live",
  promotion_not_ready: "promozione non matura",
  trader_not_selected: "trader non selezionato",
  market_not_found: "market non trovato",
  market_inactive: "market inattivo",
  market_not_perp: "market non perp",
  live_position_already_open_for_market: "live già aperta",
  notional_below_min_order: "notional sotto minimo",
  notional_above_live_cap: "notional sopra cap",
  account_snapshot_missing: "snapshot account mancante",
  account_value_below_live_min: "equity sotto minimo",
  max_total_exposure_exceeded: "exposure cap superato",
  available_margin_unknown: "margine sconosciuto",
  available_margin_insufficient: "margine insufficiente",
  max_live_open_positions_reached: "max live raggiunto",
  depth_not_checked_shadow: "depth non verificata",
  order_not_submitted: "ordine non inviato",
};

function reasonLabel(reason: string): string {
  return LIVE_REASON_LABELS[reason] ?? reason.replaceAll("_", " ");
}

function CopyLiveShadowPanel({ plan }: { plan: LeaderboardCopyLive }) {
  const skipped = Object.entries(plan.summary.skipped_open_positions ?? {})
    .map(([key, value]) => `${reasonLabel(key)}: ${value}`)
    .join(" · ");
  return (
    <Card title="Live shadow shortlist">
      <div className="mb-3 rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
        Modalità live preparatoria: seleziona i trader copiabili e stima le posizioni, ma
        `orders_enabled=false`, quindi non invia ordini Hyperliquid.
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
        <Kpi
          label="Trader live"
          value={`${plan.summary.selected_traders}/${plan.summary.evaluated_traders}`}
          sub={`max ${plan.summary.max_traders}`}
        />
        <Kpi
          label="Promossi"
          value={plan.summary.promoted_traders}
          sub={`min ${plan.selected_traders[0]?.promotion.min_shortlist_days ?? "—"} giorni`}
        />
        <Kpi
          label="Posizioni stimate"
          value={plan.summary.planned_open_positions}
          sub={`max ${plan.summary.max_open_positions}`}
        />
        <Kpi
          label="Notional stimato"
          value={fmtUsd(plan.summary.estimated_open_notional_usd)}
          sub={`${fmtUsd(plan.summary.position_notional_usd)} cad.`}
        />
        <Kpi
          label="Modalità"
          value={plan.mode}
          sub={plan.enabled ? "flag on" : "flag off"}
          compact
        />
        <Kpi
          label="Dry-run"
          value={`${plan.summary.dry_run_would_submit}/${plan.summary.dry_run_would_submit + plan.summary.dry_run_blocked}`}
          sub="would submit"
        />
        <Kpi label="Scartati" value={plan.summary.rejected_traders} sub={skipped || "no skip open"} />
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <div>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-3">
            Trader selezionati
          </div>
          {plan.selected_traders.length === 0 ? (
            <div className="rounded-md border border-border bg-bg p-3 text-sm text-fg-3">
              Nessun trader passa i filtri live-shadow. Serve più storico paper o soglie meno restrittive.
            </div>
          ) : (
            <CopyLiveTraderTable rows={plan.selected_traders} selected />
          )}
        </div>

        <div>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-3">
            Open shadow
          </div>
          {plan.shadow_open_positions.length === 0 ? (
            <div className="rounded-md border border-border bg-bg p-3 text-sm text-fg-3">
              Nessuna posizione aperta idonea dopo cap per trader, cap totale e filtri coin.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wider text-fg-3">
                    <th className="pb-2">Trader</th>
                    <th className="pb-2">Coin</th>
                    <th className="pb-2">Dir</th>
                    <th className="pb-2 text-right">Live</th>
                    <th className="pb-2 text-right">Paper</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.shadow_open_positions.map((position) => (
                    <tr key={position.paper_position_id} className="border-t border-border">
                      <td className="py-1.5 font-mono text-xs">
                        {position.trader_label || shortAddress(position.trader_address)}
                      </td>
                      <td className="py-1.5 font-mono">{position.coin}</td>
                      <td className={cn("py-1.5 font-mono text-xs", directionTone(position.direction))}>
                        {position.direction}
                      </td>
                      <td className="py-1.5 text-right font-mono">{fmtUsd(position.live_notional_usd)}</td>
                      <td className="py-1.5 text-right font-mono">{fmtUsd(position.paper_notional_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {plan.rejected_traders.length > 0 && (
        <div className="mt-4">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-3">
            Scartati principali
          </div>
          <CopyLiveTraderTable rows={plan.rejected_traders.slice(0, 8)} />
        </div>
      )}

      <div className="mt-4 grid gap-4 xl:grid-cols-[1fr_1fr]">
        <div>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-3">
            Promotion table
          </div>
          <CopyLiveTraderTable rows={plan.promotion_table.slice(0, 8)} showPromotion />
        </div>
        <div>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-3">
            Dry-run order plan
          </div>
          <CopyLiveDryRunOrders orders={plan.dry_run_orders} />
        </div>
      </div>
    </Card>
  );
}

function CopyLiveTraderTable({
  rows,
  selected = false,
  showPromotion = false,
}: {
  rows: LeaderboardCopyLiveTrader[];
  selected?: boolean;
  showPromotion?: boolean;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-fg-3">
            <th className="pb-2">Trader</th>
            <th className="pb-2 text-right">Live score</th>
            <th className="pb-2 text-right">Q</th>
            <th className="pb-2 text-right">PnL</th>
            <th className="pb-2 text-right">PF</th>
            <th className="pb-2 text-right">Win</th>
            <th className="pb-2 text-right">Closed</th>
            <th className="pb-2 text-right">{showPromotion ? "Promo" : selected ? "Plan" : "Motivi"}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.trader_id} className="border-t border-border">
              <td className="py-2 pr-3">
                <div className="font-medium">{row.trader_label || shortAddress(row.trader_address)}</div>
                <div className="text-xs text-fg-3">
                  {row.leaderboard_rank != null ? `HL #${row.leaderboard_rank}` : shortAddress(row.trader_address)}
                  {row.classification ? ` · ${row.classification}` : ""}
                </div>
              </td>
              <td className="py-2 text-right font-mono">{fmtNum(row.live_candidate_score, 1)}</td>
              <td className="py-2 text-right font-mono">{row.quality_score ?? "—"}</td>
              <td
                className={cn(
                  "py-2 text-right font-mono",
                  (asNumber(row.net_pnl_usdc) ?? 0) >= 0 ? "text-success" : "text-danger",
                )}
              >
                {fmtUsd(row.net_pnl_usdc)}
              </td>
              <td className="py-2 text-right font-mono">{fmtNum(row.profit_factor)}</td>
              <td className="py-2 text-right font-mono">{fmtPct(row.win_rate_pct)}</td>
              <td className="py-2 text-right font-mono">{row.closed_positions}</td>
              <td className="max-w-[220px] py-2 text-right text-xs text-fg-3">
                {showPromotion
                  ? `${row.promotion.status} · ${row.promotion.shortlist_days}/${row.promotion.min_shortlist_days}d`
                  : selected
                    ? `${row.planned_open_positions}/${row.open_positions} open · ${row.promotion.status}`
                    : row.reasons.map(reasonLabel).join(", ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CopyLiveDryRunOrders({ orders }: { orders: LeaderboardCopyLiveDryRunOrder[] }) {
  if (orders.length === 0) {
    return (
      <div className="rounded-md border border-border bg-bg p-3 text-sm text-fg-3">
        Nessun ordine dry-run: non ci sono posizioni shadow idonee.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-fg-3">
            <th className="pb-2">Coin</th>
            <th className="pb-2">Dir</th>
            <th className="pb-2 text-right">Notional</th>
            <th className="pb-2 text-right">Margin</th>
            <th className="pb-2 text-right">Status</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order) => (
            <tr key={`${order.paper_position_id}:${order.coin}`} className="border-t border-border">
              <td className="py-1.5 font-mono">{order.coin}</td>
              <td className={cn("py-1.5 font-mono text-xs", directionTone(order.direction))}>{order.direction}</td>
              <td className="py-1.5 text-right font-mono">{fmtUsd(order.effective_notional_usd)}</td>
              <td className="py-1.5 text-right font-mono">{fmtUsd(order.required_margin_usd)}</td>
              <td className="max-w-[260px] py-1.5 text-right text-xs">
                <div className={order.status === "would_submit" ? "text-success" : "text-warn"}>
                  {order.status}
                </div>
                <div className="text-fg-3">
                  {(order.reasons.length ? order.reasons : order.warnings).map(reasonLabel).join(", ")}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CopyRankingRow({ row }: { row: LeaderboardCopyPaperRanking }) {
  const pnl = asNumber(row.net_pnl_usdc);
  return (
    <tr className="border-t border-border">
      <td className="py-2 pr-3">
        <div className="font-medium">{row.trader_label || shortAddress(row.trader_address)}</div>
        <div className="text-xs text-fg-3">
          {row.leaderboard_rank != null ? `HL #${row.leaderboard_rank}` : shortAddress(row.trader_address)}
        </div>
      </td>
      <td className={cn("py-2 text-right font-mono", pnl == null ? "" : pnl >= 0 ? "text-success" : "text-danger")}>
        {fmtUsd(row.net_pnl_usdc)}
      </td>
      <td className="py-2 text-right font-mono">{fmtPct(row.win_rate_pct)}</td>
      <td className="py-2 text-right font-mono">{fmtNum(row.profit_factor)}</td>
      <td className="py-2 text-right font-mono">{row.open_positions}</td>
      <td className="py-2 text-right font-mono">{row.closed_positions}</td>
    </tr>
  );
}

function CopyPositionsTable({
  positions,
  empty,
}: {
  positions: LeaderboardCopyPaperPosition[];
  empty: string;
}) {
  if (positions.length === 0) {
    return <div className="rounded-md border border-border bg-bg p-3 text-sm text-fg-3">{empty}</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-fg-3">
            <th className="pb-2">Trader</th>
            <th className="pb-2">Coin</th>
            <th className="pb-2">Dir</th>
            <th className="pb-2 text-right">Entry</th>
            <th className="pb-2 text-right">Exit</th>
            <th className="pb-2 text-right">PnL</th>
            <th className="pb-2 text-right">Quando</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => {
            const pnl = asNumber(position.net_pnl_usdc);
            return (
              <tr key={position.id} className="border-t border-border">
                <td className="py-1.5 font-mono text-xs">
                  {position.trader_label || shortAddress(position.trader_address)}
                </td>
                <td className="py-1.5 font-mono">{position.coin}</td>
                <td className={cn("py-1.5 font-mono text-xs", directionTone(position.direction))}>{position.direction}</td>
                <td className="py-1.5 text-right font-mono">{position.entry_price ?? "—"}</td>
                <td className="py-1.5 text-right font-mono">{position.exit_price ?? "—"}</td>
                <td className={cn("py-1.5 text-right font-mono", pnl == null ? "" : pnl >= 0 ? "text-success" : "text-danger")}>
                  {fmtUsd(position.net_pnl_usdc)}
                </td>
                <td className="py-1.5 text-right text-xs text-fg-3">
                  {fmtDate(position.closed_at || position.opened_at)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Kpi({
  label,
  value,
  sub,
  compact = false,
  tone,
}: {
  label: string;
  value: number | string;
  sub?: string;
  compact?: boolean;
  tone?: "success" | "danger";
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-1 p-4">
      <div className="text-xs uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-1 font-mono",
          compact ? "text-sm" : "text-2xl",
          tone === "success" && "text-success",
          tone === "danger" && "text-danger",
        )}
      >
        {value}
      </div>
      {sub && <div className="text-xs text-fg-3">{sub}</div>}
    </div>
  );
}

function Card({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-1 p-4">
      <div className="mb-3 text-xs font-semibold uppercase tracking-wider text-fg-3">{title}</div>
      {children}
    </div>
  );
}

function SignalCard({ signal }: { signal: LeaderboardAlphaSignal }) {
  return (
    <div className="rounded-lg border border-border bg-bg p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-mono text-lg font-semibold">{signal.coin}</div>
          <div className={cn("text-xs uppercase tracking-wider", directionTone(signal.direction))}>
            {signal.direction}
          </div>
        </div>
        <div className="rounded-md border border-success/30 bg-success/10 px-2 py-1 text-right text-xs text-success">
          {signal.confirmation_level === "cluster" ? "cluster" : "single"} · {signal.trader_count} trader
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <Metric label="Notional" value={fmtUsd(signal.notional_usd)} />
        <Metric label="Fill" value={String(signal.fill_count)} />
        <Metric label="Avg score" value={fmtNum(signal.avg_quality_score, 0)} />
      </div>
      <div className="mt-3 text-xs text-fg-3">
        Ultimo fill {fmtDate(signal.latest_fill_at)} · usa come conferma, non come trigger unico.
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {signal.traders.map((trader) => (
          <span
            key={`${trader.id}:${trader.filled_at}`}
            className="rounded-md border border-border bg-bg-2 px-2 py-1 text-[11px] text-fg-2"
          >
            {trader.label || shortAddress(trader.address)} · q{trader.quality_score ?? "—"}
          </span>
        ))}
      </div>
    </div>
  );
}

function TraderRow({
  trader,
  onToggle,
  onSync,
  syncing,
}: {
  trader: LeaderboardAlphaTrader;
  onToggle: () => void;
  onSync: () => void;
  syncing: boolean;
}) {
  const snap = trader.latest_snapshot;
  const meta = leaderboardMeta(trader);
  const roiPct = metaNumber(meta, "roi_pct");
  const volumeUsd = metaNumber(meta, "volume_usd");
  const rank = metaNumber(meta, "rank");
  const topCoin = typeof snap?.metrics.top_coin === "string" ? snap.metrics.top_coin : null;
  const topCoinShare =
    typeof snap?.metrics.top_coin_share === "number" ? snap.metrics.top_coin_share * 100 : null;
  return (
    <tr className={cn("border-t border-border", !trader.enabled && "opacity-55")}>
      <td className="py-2 pr-3">
        <div className="font-medium">{trader.label || shortAddress(trader.address)}</div>
        <div className="font-mono text-xs text-fg-3">{shortAddress(trader.address)}</div>
        {rank != null && <div className="mt-0.5 text-[11px] text-fg-4">HL ROI 7d #{rank}</div>}
        {trader.last_sync_status === "error" && (
          <div className="mt-1 flex items-center gap-1 text-xs text-danger">
            <AlertTriangle className="h-3 w-3" />
            {trader.last_sync_error || "Sync error"}
          </div>
        )}
        {trader.last_sync_status === "rate_limited" && (
          <div className="mt-1 flex items-center gap-1 text-xs text-warn">
            <AlertTriangle className="h-3 w-3" />
            Budget HL esaurito
          </div>
        )}
      </td>
      <td className="py-2">
        <span className={cn("rounded-md border px-2 py-1 text-xs", classTone(snap?.classification))}>
          {snap ? CLASS_LABEL[snap.classification] : "No data"}
        </span>
      </td>
      <td className="py-2 text-right font-mono">{snap?.quality_score ?? "—"}</td>
      <td className="py-2 text-right font-mono">{fmtPct(roiPct)}</td>
      <td className="py-2 text-right font-mono">{fmtUsd(volumeUsd)}</td>
      <td
        className={cn(
          "py-2 text-right font-mono",
          asNumber(snap?.net_pnl_usdc) != null && (asNumber(snap?.net_pnl_usdc) ?? 0) >= 0
            ? "text-success"
            : "text-danger",
        )}
      >
        {fmtUsd(snap?.net_pnl_usdc)}
      </td>
      <td className="py-2 text-right font-mono">{fmtPct(snap?.win_rate_pct)}</td>
      <td className="py-2 text-right font-mono">{fmtNum(snap?.profit_factor)}</td>
      <td className="py-2 text-right font-mono">{snap?.fills_count ?? "—"}</td>
      <td className="py-2 text-right font-mono text-xs">
        {topCoin ? `${topCoin} ${fmtPct(topCoinShare)}` : "—"}
      </td>
      <td className="py-2 text-right text-xs text-fg-3">{fmtDate(trader.last_sync_at)}</td>
      <td className="py-2 text-right">
        <div className="flex justify-end gap-1.5">
          <button
            onClick={onSync}
            disabled={syncing}
            className="rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-hover disabled:opacity-50"
          >
            Sync
          </button>
          <button
            onClick={onToggle}
            className={cn(
              "rounded-md border px-2 py-1 text-xs hover:bg-bg-hover",
              trader.enabled ? "border-success/40 text-success" : "border-border text-fg-3",
            )}
          >
            {trader.enabled ? "On" : "Off"}
          </button>
        </div>
      </td>
    </tr>
  );
}

function RecentFills({ fills }: { fills: LeaderboardAlphaFill[] }) {
  if (fills.length === 0) {
    return <div className="py-3 text-sm text-fg-3">Nessun fill sincronizzato.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-fg-3">
            <th className="pb-2">Trader</th>
            <th className="pb-2">Coin</th>
            <th className="pb-2">Intent</th>
            <th className="pb-2 text-right">Notional</th>
            <th className="pb-2 text-right">Prezzo</th>
            <th className="pb-2 text-right">Closed PnL</th>
            <th className="pb-2 text-right">Quando</th>
          </tr>
        </thead>
        <tbody>
          {fills.map((fill) => (
            <tr key={fill.id} className="border-t border-border">
              <td className="py-1.5 font-mono text-xs">
                {fill.trader_label || shortAddress(fill.trader_address)}
              </td>
              <td className="py-1.5 font-mono">{fill.coin}</td>
              <td className="py-1.5">
                <span className={cn("font-mono text-xs", directionTone(fill.direction))}>
                  {fill.intent || "—"} {fill.direction || ""}
                </span>
              </td>
              <td className="py-1.5 text-right font-mono">{fmtUsd(fill.notional_usd)}</td>
              <td className="py-1.5 text-right font-mono">{fill.price ?? "—"}</td>
              <td className="py-1.5 text-right font-mono">{fmtUsd(fill.closed_pnl)}</td>
              <td className="py-1.5 text-right text-xs text-fg-3">{fmtDate(fill.filled_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-bg-1 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-fg-4">{label}</div>
      <div className="font-mono text-xs text-fg">{value}</div>
    </div>
  );
}
