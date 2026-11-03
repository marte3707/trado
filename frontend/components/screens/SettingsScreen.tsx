"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { OperationTab } from "@/components/settings/OperationTab";
import { RiskTab } from "@/components/settings/RiskTab";
import { PaperTab } from "@/components/settings/PaperTab";
import { PositionTab } from "@/components/settings/PositionTab";
import { LiveTab } from "@/components/settings/LiveTab";
import { ServicesTab } from "@/components/settings/ServicesTab";
import { IntegrationsTab } from "@/components/settings/IntegrationsTab";
import { Separator } from "@/components/ui/separator";
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
          <h1 className="text-xl font-semibold tracking-tight text-fg">Impostazioni</h1>
          <p className="mt-1 text-xs text-fg-3">
            Controlli operativi, rischio e accesso live
          </p>
        </div>
      </div>

      <Tabs defaultValue="operation" className="w-full">
        <TabsList className="h-auto flex-wrap">
          <TabsTrigger value="operation">Operatività</TabsTrigger>
          <TabsTrigger value="risk">Rischio</TabsTrigger>
          <TabsTrigger value="live">Live</TabsTrigger>
        </TabsList>

        <details className="mt-3 rounded-md border border-border bg-bg-1 px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-fg-3">
            Configurazione avanzata
          </summary>
          <TabsList className="mt-3 h-auto flex-wrap">
            <TabsTrigger value="paper">Paper</TabsTrigger>
            <TabsTrigger value="position">Gestione posizioni</TabsTrigger>
            <TabsTrigger value="services">Servizi e integrazioni</TabsTrigger>
          </TabsList>
        </details>

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
        <TabsContent value="services" className="mt-5 space-y-6">
          <ServicesTab health={health} />
          <Separator />
          <IntegrationsTab settings={settings} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
