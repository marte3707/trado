"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { OperationTab } from "@/components/settings/OperationTab";
import { RiskTab } from "@/components/settings/RiskTab";
import { PaperTab } from "@/components/settings/PaperTab";
import { PositionTab } from "@/components/settings/PositionTab";
import { LiveTab } from "@/components/settings/LiveTab";
import { CalibrationTab } from "@/components/settings/CalibrationTab";
import { ServicesTab } from "@/components/settings/ServicesTab";
import { IntegrationsTab } from "@/components/settings/IntegrationsTab";
import type { AppSettings, Health } from "@/lib/api";

interface Props {
  settings: AppSettings;
  health: Health;
  onRefresh: () => void | Promise<void>;
}

export function SettingsScreen({ settings, health }: Props) {
  return (
    <div className="space-y-6 px-7 py-7">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Settings</h1>
          <p className="mt-1 text-xs text-fg-3">
            Operation · Risk · Paper · Position mgmt · Live · Calibration · Services · Integrations
          </p>
        </div>
      </div>

      <Tabs defaultValue="operation" className="w-full">
        <TabsList className="flex-wrap h-auto">
          <TabsTrigger value="operation">Operation</TabsTrigger>
          <TabsTrigger value="risk">Risk</TabsTrigger>
          <TabsTrigger value="paper">Paper</TabsTrigger>
          <TabsTrigger value="position">Position</TabsTrigger>
          <TabsTrigger value="live">Live</TabsTrigger>
          <TabsTrigger value="calibration">Calibration</TabsTrigger>
          <TabsTrigger value="services">Services</TabsTrigger>
          <TabsTrigger value="integrations">Integrations</TabsTrigger>
        </TabsList>

        <TabsContent value="operation" className="mt-5">
          <OperationTab settings={settings} />
        </TabsContent>
        <TabsContent value="risk" className="mt-5">
          <RiskTab settings={settings} />
        </TabsContent>
        <TabsContent value="paper" className="mt-5">
          <PaperTab settings={settings} />
        </TabsContent>
        <TabsContent value="position" className="mt-5">
          <PositionTab settings={settings} />
        </TabsContent>
        <TabsContent value="live" className="mt-5">
          <LiveTab health={health} settings={settings} />
        </TabsContent>
        <TabsContent value="calibration" className="mt-5">
          <CalibrationTab settings={settings} />
        </TabsContent>
        <TabsContent value="services" className="mt-5">
          <ServicesTab health={health} />
        </TabsContent>
        <TabsContent value="integrations" className="mt-5">
          <IntegrationsTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
