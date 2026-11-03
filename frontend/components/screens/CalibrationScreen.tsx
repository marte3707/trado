"use client";

import { FlaskConical, RefreshCw } from "lucide-react";

import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EquitySlopeTab } from "@/components/calibration/EquitySlopeTab";
import { DiagnosticsTab } from "@/components/calibration/DiagnosticsTab";
import { ScoreAlignmentTab } from "@/components/calibration/ScoreAlignmentTab";
import { useExperimentOutcomes, useSignals } from "@/lib/hooks/queries";
import type {
  AppSettings,
  SignalRejections,
  WorkerCycle,
} from "@/lib/api";

interface Props {
  settings: AppSettings;
  workerCycles: WorkerCycle[];
  signalRejections: SignalRejections;
  onRefresh: () => void | Promise<void>;
}

export function CalibrationScreen({
  settings,
  workerCycles,
  signalRejections,
  onRefresh,
}: Props) {
  // Signals servono al Signal Funnel della tab Diagnostics.
  const { data: signals = [] } = useSignals("min_score=0");
  const experiment = settings.research_experiment ?? {};
  const experimentVersion = String(experiment.active_version ?? "baseline-frozen-v1");
  const { data: outcomes } = useExperimentOutcomes(experimentVersion);
  const entriesEnabled = Boolean(settings.live_execution?.new_live_entries_enabled);
  const sourceOutcomes = Object.entries(outcomes?.by_source ?? {});
  const configurationCount = outcomes?.configuration_count ?? 0;
  const openPaperPositions = outcomes?.active_paper.positions ?? 0;

  return (
    <div className="space-y-6 px-7 py-7">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Ricerca</h1>
          <p className="mt-1 text-xs text-fg-3">
            Evidenza forward, robustezza e diagnostica del processo decisionale
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void onRefresh()}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          Aggiorna
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-bg-1 px-4 py-3">
        <FlaskConical className="h-4 w-4 text-success" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-fg">
            Esperimento {experimentVersion}
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            {String(experiment.mode ?? "frozen_observation")} · promozione {String(experiment.promotion_policy ?? "manual")}
          </div>
        </div>
      </div>

      <Tabs defaultValue="overview" className="w-full">
        <TabsList>
          <TabsTrigger value="overview">Sintesi</TabsTrigger>
          <TabsTrigger value="equity-slope">Curva equity</TabsTrigger>
          <TabsTrigger value="diagnostics">Diagnostica</TabsTrigger>
          <TabsTrigger value="score-alignment">Score alignment</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="mt-5 space-y-4">
          <Card className="p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-fg">Evidenza forward attribuita</h2>
                <p className="mt-1 text-xs text-fg-3">
                  Solo trade paper generati da questa versione, costi round-trip stimati inclusi.
                </p>
              </div>
              <span className="rounded-md border border-border bg-bg-2 px-2 py-1 font-mono text-[10px] text-fg-3">
                {outcomes?.latest_closed_at
                  ? `ultimo ${new Date(outcomes.latest_closed_at).toLocaleDateString("it-IT")}`
                  : "raccolta attiva"}
              </span>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <ResearchMetric label="Campione attribuito" value={String(outcomes?.overall.trades ?? 0)} />
              <ResearchMetric
                label="Profit factor netto"
                value={outcomes?.overall.profit_factor_net_r?.toFixed(2) ?? "—"}
                danger={(outcomes?.overall.profit_factor_net_r ?? 1) < 1}
              />
              <ResearchMetric
                label="Expectancy netta"
                value={outcomes?.overall.average_net_r != null ? `${outcomes.overall.average_net_r.toFixed(2)} R` : "—"}
                danger={(outcomes?.overall.average_net_r ?? 0) < 0}
              />
              <ResearchMetric label="Max drawdown" value={`${(outcomes?.overall.max_drawdown_r ?? 0).toFixed(2)} R`} />
            </div>
            <div className="mt-4 flex flex-wrap gap-2 text-[11px] text-fg-3">
              <BarrierBadge label="TP prima" value={outcomes?.overall.barriers.tp_first ?? 0} />
              <BarrierBadge label="Stop prima" value={outcomes?.overall.barriers.stop_first ?? 0} />
              <BarrierBadge label="Ambigui" value={outcomes?.overall.barriers.ambiguous ?? 0} />
              <BarrierBadge label="Nessun touch" value={outcomes?.overall.barriers.no_touch ?? 0} />
            </div>
            {openPaperPositions > 0 ? (
              <p className="mt-4 rounded-md border border-border bg-bg-2 px-3 py-2 text-xs leading-5 text-fg-2">
                {openPaperPositions} {openPaperPositions === 1 ? "posizione paper è ancora aperta" : "posizioni paper sono ancora aperte"}. Il loro P&amp;L è provvisorio e non viene usato per stimare l&apos;edge: il campione si aggiorna solo alla chiusura.
              </p>
            ) : null}
            {configurationCount > 1 ? (
              <p className="mt-4 rounded-md border border-border bg-bg-2 px-3 py-2 text-xs leading-5 text-fg-2">
                Questa versione contiene {configurationCount} configurazioni distinte. I risultati aggregati servono solo come audit: non costituiscono evidenza per una promozione. Ogni configurazione viene validata separatamente.
              </p>
            ) : null}
            <div className="mt-5 border-t border-border pt-4">
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Evidenza per sorgente
              </div>
              <div className="mt-2 grid gap-2 md:grid-cols-2">
                {sourceOutcomes.map(([source, summary]) => (
                  <div key={source} className="rounded-md border border-border bg-bg-2 px-3 py-2 text-xs">
                    <div className="font-medium text-fg">{source === "standard" ? "Motore standard" : source}</div>
                    <div className="mt-1 text-fg-3">
                      {summary.trades} trade · PF {summary.profit_factor_net_r?.toFixed(2) ?? "—"} · {summary.average_net_r != null ? `${summary.average_net_r.toFixed(2)} R` : "—"}
                    </div>
                  </div>
                ))}
              </div>
            </div>
            {(outcomes?.overall.trades ?? 0) === 0 ? (
              <p className="mt-4 text-xs leading-5 text-fg-3">
                {openPaperPositions > 0
                  ? "La prima evidenza sarà disponibile quando queste posizioni paper verranno chiuse."
                  : "Il campione parte da ora: lo storico precedente non viene riattribuito, per evitare contaminazione retrospettiva."}
              </p>
            ) : null}
          </Card>

          <Card className="p-5">
            <h2 className="text-sm font-semibold text-fg">Stato della validazione</h2>
            <p className="mt-2 text-sm leading-6 text-fg-2">
              Il sistema è in osservazione forward con parametri congelati. Le nuove entry live sono {entriesEnabled ? "abilitate" : "sospese"}; i trade paper continuano a costruire il campione della baseline.
            </p>
            <div className="mt-4 grid gap-3 md:grid-cols-3">
              <ResearchStep label="1. Raccogli" detail="Nuovi trade paper senza tuning durante la finestra." />
              <ResearchStep label="2. Verifica" detail="PF netto, expectancy e drawdown su full e recente." />
              <ResearchStep label="3. Promuovi" detail="Solo manualmente, dopo conferma forward e holdout." />
            </div>
          </Card>
        </TabsContent>

        <TabsContent value="equity-slope" className="mt-5">
          <EquitySlopeTab />
        </TabsContent>

        <TabsContent value="score-alignment" className="mt-5">
          <ScoreAlignmentTab />
        </TabsContent>

        <TabsContent value="diagnostics" className="mt-5">
          <DiagnosticsTab
            signals={signals}
            rejections={signalRejections}
            workerCycles={workerCycles}
            settings={settings}
          />
        </TabsContent>

      </Tabs>
    </div>
  );
}

function BarrierBadge({ label, value }: { label: string; value: number }) {
  return (
    <span className="rounded-md border border-border bg-bg-2 px-2 py-1">
      {label}: <span className="font-mono text-fg-2">{value}</span>
    </span>
  );
}

function ResearchMetric({ label, value, danger = false }: { label: string; value: string; danger?: boolean }) {
  return (
    <Card className="p-4">
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
      <div className={`mt-2 font-mono text-xl font-semibold ${danger ? "text-danger" : "text-fg"}`}>{value}</div>
    </Card>
  );
}

function ResearchStep({ label, detail }: { label: string; detail: string }) {
  return (
    <div className="rounded-md border border-border bg-bg-2 p-3">
      <div className="text-xs font-semibold text-fg">{label}</div>
      <div className="mt-1 text-xs leading-5 text-fg-3">{detail}</div>
    </div>
  );
}
