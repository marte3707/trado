"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  apiDelete,
  apiPatch,
  apiPost,
  ApiError,
  type AppSettings,
  type LeaderboardAlphaImportResult,
  type LeaderboardAlphaSyncResult,
  type LeaderboardAlphaTrader,
  type LeaderboardCopyLiveEnableResult,
  type LeaderboardCopyPaperRunResult,
  type Strategy,
  type SystemService,
  type SystemServiceAction,
  type SystemServiceResult,
} from "@/lib/api";
import { qk } from "@/lib/hooks/queryKeys";

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail || err.message;
  if (err instanceof Error) return err.message;
  return "Errore sconosciuto";
}

/* ── Kill switch ─────────────────────────────────────────────────────── */

export function useKillSwitch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (enable: boolean) => {
      return apiPost<unknown>(enable ? "/api/kill-switch/enable" : "/api/kill-switch/disable");
    },
    onSuccess: (_, enable) => {
      toast.success(enable ? "Kill switch armato" : "Kill switch disarmato");
      qc.invalidateQueries({ queryKey: qk.settings });
      qc.invalidateQueries({ queryKey: qk.health });
    },
    onError: (err) => toast.error(`Kill switch fallito: ${errorMessage(err)}`),
  });
}

/* ── Settings ────────────────────────────────────────────────────────── */

export function useUpdateSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (patch: Partial<AppSettings>) => apiPatch<AppSettings>("/api/settings", patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.settings });
      qc.invalidateQueries({ queryKey: qk.health });
    },
    onError: (err) => toast.error(`Salvataggio fallito: ${errorMessage(err)}`),
  });
}

export function useUpdateScoreThreshold() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      threshold,
      currentRisk,
    }: {
      threshold: number;
      currentRisk: Record<string, unknown>;
    }) => {
      const nextRisk = { ...currentRisk, min_signal_score: threshold };
      return apiPatch<AppSettings>("/api/settings", { risk: { value: nextRisk } });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.settings });
    },
    onError: (err) => toast.error(`Soglia score non salvata: ${errorMessage(err)}`),
  });
}

/* ── Strategies ──────────────────────────────────────────────────────── */

export function useUpdateStrategy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      patch,
    }: {
      id: number;
      patch: Partial<Pick<Strategy, "enabled" | "weight" | "params">>;
    }) => apiPatch<Strategy>(`/api/strategies/${id}`, patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.strategies });
    },
    onError: (err) => toast.error(`Strategia non aggiornata: ${errorMessage(err)}`),
  });
}

/* ── Positions ───────────────────────────────────────────────────────── */

export function useClosePosition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (positionId: number) => apiPost<unknown>(`/api/positions/${positionId}/close`),
    onSuccess: () => {
      toast.success("Posizione chiusa");
      qc.invalidateQueries({ queryKey: qk.positions });
      qc.invalidateQueries({ queryKey: qk.orders });
    },
    onError: (err) => toast.error(`Chiusura fallita: ${errorMessage(err)}`),
  });
}

export function useDeletePosition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (positionId: number) => apiDelete<unknown>(`/api/positions/${positionId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.positions });
    },
    onError: (err) => toast.error(`Eliminazione fallita: ${errorMessage(err)}`),
  });
}

/* ── Signals ─────────────────────────────────────────────────────────── */

export function useExecuteSignalPaper() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (signalId: number) =>
      apiPost<unknown>(`/api/signals/${signalId}/execute-paper`),
    onSuccess: () => {
      toast.success("Segnale eseguito in paper");
      qc.invalidateQueries({ queryKey: qk.signals() });
      qc.invalidateQueries({ queryKey: qk.positions });
    },
    onError: (err) => toast.error(`Esecuzione paper fallita: ${errorMessage(err)}`),
  });
}

export function useIgnoreSignal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (signalId: number) => apiPost<unknown>(`/api/signals/${signalId}/ignore`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.signals() });
    },
    onError: (err) => toast.error(`Operazione fallita: ${errorMessage(err)}`),
  });
}

export function useReopenSignal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (signalId: number) => apiPost<unknown>(`/api/signals/${signalId}/reopen`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.signals() });
    },
    onError: (err) => toast.error(`Operazione fallita: ${errorMessage(err)}`),
  });
}

export function useDeleteSignal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (signalId: number) => apiDelete<unknown>(`/api/signals/${signalId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.signals() });
    },
    onError: (err) => toast.error(`Eliminazione fallita: ${errorMessage(err)}`),
  });
}

/* ── Auto-tuner ──────────────────────────────────────────────────────── */

type TunerAction = "run" | "pause" | "resume" | "rollback" | "reset";

export function useAutoTuner() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (action: TunerAction) =>
      apiPost<unknown>(`/api/auto-tuner/${action}`),
    onSuccess: (_, action) => {
      const labels: Record<TunerAction, string> = {
        run: "Auto-tuner attivato",
        pause: "Auto-tuner in pausa",
        resume: "Auto-tuner ripreso",
        rollback: "Rollback eseguito",
        reset: "Auto-tuner resettato",
      };
      toast.success(labels[action]);
      qc.invalidateQueries({ queryKey: qk.tunerStatus });
      qc.invalidateQueries({ queryKey: qk.tunerSourceTests });
      qc.invalidateQueries({ queryKey: qk.tunerStrategyTests });
    },
    onError: (err) => toast.error(`Azione tuner fallita: ${errorMessage(err)}`),
  });
}

export function useStartSourceTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (source: string) =>
      apiPost<unknown>(`/api/auto-tuner/source-tests/${source}/start`),
    onSuccess: (_, source) => {
      toast.success(`A/B test "${source}" avviato`);
      qc.invalidateQueries({ queryKey: qk.tunerStatus });
      qc.invalidateQueries({ queryKey: qk.tunerSourceTests });
      qc.invalidateQueries({ queryKey: qk.tunerStrategyTests });
    },
    onError: (err) => toast.error(`Avvio test fallito: ${errorMessage(err)}`),
  });
}

export function useStartStrategyTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ timeframe, strategy }: { timeframe: string; strategy: string }) =>
      apiPost<unknown>(`/api/auto-tuner/strategy-tests/${timeframe}/${strategy}/start`),
    onSuccess: (_, payload) => {
      toast.success(`A/B test ${payload.timeframe}/${payload.strategy} avviato`);
      qc.invalidateQueries({ queryKey: qk.tunerStatus });
      qc.invalidateQueries({ queryKey: qk.tunerSourceTests });
      qc.invalidateQueries({ queryKey: qk.tunerStrategyTests });
    },
    onError: (err) => toast.error(`Avvio strategy test fallito: ${errorMessage(err)}`),
  });
}

export function useQueueStrategyTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ timeframe, strategy }: { timeframe: string; strategy: string }) =>
      apiPost<unknown>(`/api/auto-tuner/strategy-tests/${timeframe}/${strategy}/queue`),
    onSuccess: (_, payload) => {
      toast.success(`Test ${payload.timeframe}/${payload.strategy} messo in coda`);
      qc.invalidateQueries({ queryKey: qk.tunerStatus });
      qc.invalidateQueries({ queryKey: qk.tunerStrategyTests });
    },
    onError: (err) => toast.error(`Queue strategy test fallita: ${errorMessage(err)}`),
  });
}

export function useClearQueuedStrategyTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => apiDelete<unknown>("/api/auto-tuner/strategy-tests/queue"),
    onSuccess: () => {
      toast.success("Coda strategy test rimossa");
      qc.invalidateQueries({ queryKey: qk.tunerStatus });
      qc.invalidateQueries({ queryKey: qk.tunerStrategyTests });
    },
    onError: (err) => toast.error(`Rimozione coda fallita: ${errorMessage(err)}`),
  });
}

/* ── Leaderboard alpha ───────────────────────────────────────────────── */

export function useCreateLeaderboardTrader() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { address: string; label?: string; notes?: string }) =>
      apiPost<LeaderboardAlphaTrader>("/api/leaderboard-alpha/traders", payload),
    onSuccess: () => {
      toast.success("Trader aggiunto alla watchlist");
      qc.invalidateQueries({ queryKey: qk.leaderboardAlpha });
    },
    onError: (err) => toast.error(`Trader non aggiunto: ${errorMessage(err)}`),
  });
}

export function useImportLeaderboardTopTraders() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => apiPost<LeaderboardAlphaImportResult>("/api/leaderboard-alpha/import-top"),
    onSuccess: (data) => {
      toast.success(`Top ROI importati: ${data.inserted} nuovi, ${data.updated} aggiornati`);
      qc.invalidateQueries({ queryKey: qk.leaderboardAlpha });
    },
    onError: (err) => toast.error(`Import top trader fallito: ${errorMessage(err)}`),
  });
}

export function useUpdateLeaderboardTrader() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      patch,
    }: {
      id: number;
      patch: Partial<Pick<LeaderboardAlphaTrader, "label" | "enabled" | "notes">>;
    }) => apiPatch<LeaderboardAlphaTrader>(`/api/leaderboard-alpha/traders/${id}`, patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.leaderboardAlpha });
    },
    onError: (err) => toast.error(`Trader non aggiornato: ${errorMessage(err)}`),
  });
}

export function useSyncLeaderboardTrader() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) =>
      apiPost<LeaderboardAlphaSyncResult>(`/api/leaderboard-alpha/traders/${id}/sync`),
    onSuccess: (data) => {
      if (data.status === "rate_limited") {
        toast.warning("Sync rimandata: budget Hyperliquid userFillsByTime esaurito");
      } else {
        toast.success(`Sync completata: ${data.inserted ?? 0} fill nuovi`);
      }
      qc.invalidateQueries({ queryKey: qk.leaderboardAlpha });
    },
    onError: (err) => toast.error(`Sync trader fallita: ${errorMessage(err)}`),
  });
}

export function useSyncLeaderboardWatchlist() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => apiPost<LeaderboardAlphaSyncResult>("/api/leaderboard-alpha/sync"),
    onSuccess: (data) => {
      if ((data.rate_limited ?? 0) > 0) {
        toast.warning(`Sync parziale: ${data.synced ?? 0} trader, budget HL esaurito`);
      } else {
        toast.success(`Watchlist sincronizzata: ${data.synced ?? 0} trader`);
      }
      qc.invalidateQueries({ queryKey: qk.leaderboardAlpha });
    },
    onError: (err) => toast.error(`Sync watchlist fallita: ${errorMessage(err)}`),
  });
}

export function useRunLeaderboardCopyPaper() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => apiPost<LeaderboardCopyPaperRunResult>("/api/leaderboard-alpha/copy-paper/run"),
    onSuccess: (data) => {
      toast.success(
        `Copy-paper aggiornato: ${data.opened ?? 0} aperte, ${data.closed ?? 0} chiuse, ${
          data.reconciled_closed ?? 0
        } riconciliate`,
      );
      qc.invalidateQueries({ queryKey: qk.leaderboardAlpha });
    },
    onError: (err) => toast.error(`Copy-paper fallito: ${errorMessage(err)}`),
  });
}

export function useEnableLeaderboardCopyLive() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      apiPost<LeaderboardCopyLiveEnableResult>("/api/leaderboard-alpha/copy-live/enable"),
    onSuccess: () => {
      toast.success("Copy-live impostato in modalità live");
      qc.invalidateQueries({ queryKey: qk.leaderboardAlpha });
      qc.invalidateQueries({ queryKey: qk.settings });
      qc.invalidateQueries({ queryKey: qk.health });
    },
    onError: (err) => toast.error(`Copy-live non attivato: ${errorMessage(err)}`),
  });
}

/* ── System services ─────────────────────────────────────────────────── */

export function useServiceAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      service,
      action,
    }: {
      service: SystemService;
      action: SystemServiceAction;
    }) => apiPost<SystemServiceResult>(`/api/system/services/${service}/${action}`),
    onSuccess: (data) => {
      toast.success(`${data.label}: ${data.action}`);
      qc.invalidateQueries({ queryKey: qk.health });
    },
    onError: (err) => toast.error(`Azione servizio fallita: ${errorMessage(err)}`),
  });
}
