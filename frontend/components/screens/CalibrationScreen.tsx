"use client";

import { RefreshCw } from "lucide-react";

import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { AutoTunerTab } from "@/components/calibration/AutoTunerTab";
import { StabilityTab } from "@/components/calibration/StabilityTab";
import { EquitySlopeTab } from "@/components/calibration/EquitySlopeTab";
import { DiagnosticsTab } from "@/components/calibration/DiagnosticsTab";
import { useSignals } from "@/lib/hooks/queries";
import type {
  AppSettings,
  AutoTunerStatus,
  Calibration,
  CalibrationHistoryEntry,
  SignalRejections,
  WorkerCycle,
} from "@/lib/api";

interface Props {
  calibration: Calibration;
  history: CalibrationHistoryEntry[];
  settings: AppSettings;
  tunerStatus: AutoTunerStatus | null;
  workerCycles: WorkerCycle[];
  signalRejections: SignalRejections;
  onRefresh: () => void | Promise<void>;
}

export function CalibrationScreen({
  calibration,
  history,
  tunerStatus,
  workerCycles,
  signalRejections,
  onRefresh,
}: Props) {
  // Signals servono al Signal Funnel della tab Diagnostics.
  const { data: signals = [] } = useSignals("min_score=0");

  return (
    <div className="space-y-6 px-7 py-7">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Calibration</h1>
          <p className="mt-1 text-xs text-fg-3">
            Auto-tuner A/B · Stability matrix · Signal funnel diagnostics
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void onRefresh()}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          Aggiorna
        </Button>
      </div>

      <Tabs defaultValue="tuner" className="w-full">
        <TabsList>
          <TabsTrigger value="tuner">Auto-tuner</TabsTrigger>
          <TabsTrigger value="stability">Stability</TabsTrigger>
          <TabsTrigger value="equity-slope">Equity slope</TabsTrigger>
          <TabsTrigger value="diagnostics">Diagnostics</TabsTrigger>
        </TabsList>

        <TabsContent value="tuner" className="mt-5">
          <AutoTunerTab initialStatus={tunerStatus} />
        </TabsContent>

        <TabsContent value="stability" className="mt-5">
          <StabilityTab />
        </TabsContent>

        <TabsContent value="equity-slope" className="mt-5">
          <EquitySlopeTab />
        </TabsContent>

        <TabsContent value="diagnostics" className="mt-5">
          <DiagnosticsTab
            calibration={calibration}
            history={history}
            signals={signals}
            rejections={signalRejections}
            workerCycles={workerCycles}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
