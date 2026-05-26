"use client";

import { useEffect, useState } from "react";
import { BellRing, Globe, MessageCircle, MonitorPlay } from "lucide-react";
import { toast } from "sonner";

import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Slider } from "@/components/ui/slider";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Badge } from "@/components/ui/badge";
import { useUpdateSettings } from "@/lib/hooks/mutations";
import { isPushSupported, subscribePush, unsubscribePush } from "@/lib/push";
import { cn } from "@/lib/utils";
import type { AppSettings } from "@/lib/api";

interface Props {
  settings: AppSettings;
}

interface AlertSettings {
  dashboard_enabled: boolean;
  telegram_enabled: boolean;
  browser_push_enabled: boolean;
  min_score: number;
  only_strong: boolean;
  position_state_alerts: boolean;
  risk_warnings: boolean;
  mute_during_auto_trade: boolean;
  quiet_from: string;
  quiet_to: string;
}

const DEFAULTS: AlertSettings = {
  dashboard_enabled: true,
  telegram_enabled: false,
  browser_push_enabled: false,
  min_score: 60,
  only_strong: false,
  position_state_alerts: true,
  risk_warnings: true,
  mute_during_auto_trade: false,
  quiet_from: "",
  quiet_to: "",
};

function readAlertSettings(settings: AppSettings): AlertSettings {
  const raw = (settings.alerts ?? {}) as Record<string, unknown>;
  return {
    dashboard_enabled: Boolean(raw.dashboard_enabled ?? DEFAULTS.dashboard_enabled),
    telegram_enabled: Boolean(raw.telegram_enabled ?? DEFAULTS.telegram_enabled),
    browser_push_enabled: Boolean(raw.browser_push_enabled ?? DEFAULTS.browser_push_enabled),
    min_score: Number(raw.min_score ?? DEFAULTS.min_score),
    only_strong: Boolean(raw.only_strong ?? DEFAULTS.only_strong),
    position_state_alerts: Boolean(raw.position_state_alerts ?? DEFAULTS.position_state_alerts),
    risk_warnings: Boolean(raw.risk_warnings ?? DEFAULTS.risk_warnings),
    mute_during_auto_trade: Boolean(raw.mute_during_auto_trade ?? DEFAULTS.mute_during_auto_trade),
    quiet_from: String(raw.quiet_from ?? ""),
    quiet_to: String(raw.quiet_to ?? ""),
  };
}

export function AlertConfigPanel({ settings }: Props) {
  const persisted = readAlertSettings(settings);
  const [local, setLocal] = useState<AlertSettings>(persisted);
  const [pushBusy, setPushBusy] = useState(false);
  const { mutate: updateSettings, isPending } = useUpdateSettings();

  useEffect(() => {
    setLocal(persisted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    persisted.dashboard_enabled,
    persisted.telegram_enabled,
    persisted.browser_push_enabled,
    persisted.min_score,
    persisted.only_strong,
    persisted.position_state_alerts,
    persisted.risk_warnings,
    persisted.mute_during_auto_trade,
    persisted.quiet_from,
    persisted.quiet_to,
  ]);

  function commit(next: Partial<AlertSettings>) {
    const merged = { ...local, ...next };
    setLocal(merged);
    updateSettings({ alerts: { value: { ...persisted, ...merged } } } as Partial<AppSettings>);
  }

  async function togglePush(on: boolean) {
    if (!isPushSupported()) {
      toast.error("Push non supportato in questo browser.");
      return;
    }
    setPushBusy(true);
    try {
      if (on) {
        const ok = await subscribePush();
        if (!ok) {
          toast.error("Sottoscrizione push fallita o permesso negato.");
          return;
        }
        toast.success("Push browser attivate.");
      } else {
        await unsubscribePush();
        toast.success("Push browser disattivate.");
      }
      commit({ browser_push_enabled: on });
    } finally {
      setPushBusy(false);
    }
  }

  return (
    <Card className="flex h-full flex-col overflow-hidden">
      <div className="border-b border-border px-6 py-4">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Configurazione alerts
        </div>
        <div className="mt-0.5 text-xs text-fg-3">
          Channel, rules, quiet hours · {isPending ? "salvataggio..." : "salvataggio automatico"}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-5">
        {/* Channels */}
        <SectionHeader title="Channels" />
        <ChannelRow
          icon={<MonitorPlay className="h-4 w-4" />}
          label="Dashboard"
          hint="Feed in-app live."
          checked={local.dashboard_enabled}
          onChange={(v) => commit({ dashboard_enabled: v })}
        />
        <ChannelRow
          icon={<MessageCircle className="h-4 w-4" />}
          label="Telegram"
          hint="Configurato dal backend (TELEGRAM_BOT_TOKEN)."
          checked={local.telegram_enabled}
          onChange={(v) => commit({ telegram_enabled: v })}
        />
        <ChannelRow
          icon={<Globe className="h-4 w-4" />}
          label="Push browser"
          hint={
            isPushSupported()
              ? "Web Push notifications (richiede permesso)."
              : "Non supportato in questo browser."
          }
          checked={local.browser_push_enabled}
          disabled={pushBusy || !isPushSupported()}
          onChange={togglePush}
        />

        <Separator className="my-5" />

        {/* Rules */}
        <SectionHeader title="Rules" />

        <div className="space-y-2">
          <div className="flex items-center justify-between text-xs">
            <span className="text-fg-2">Score minimo</span>
            <span className="font-mono text-fg">{local.min_score}</span>
          </div>
          <Slider
            value={[local.min_score]}
            min={0}
            max={100}
            step={5}
            onValueChange={(v: number[]) => setLocal((s) => ({ ...s, min_score: v[0] }))}
            onValueCommit={() => commit({ min_score: local.min_score })}
          />
        </div>

        <RuleRow
          label="Solo STRONG (score ≥ 90)"
          checked={local.only_strong}
          onChange={(v) => commit({ only_strong: v })}
        />
        <RuleRow
          label="Alert per cambi di stato posizione"
          checked={local.position_state_alerts}
          onChange={(v) => commit({ position_state_alerts: v })}
        />
        <RuleRow
          label="Risk warning (DD, exposure)"
          checked={local.risk_warnings}
          onChange={(v) => commit({ risk_warnings: v })}
        />
        <RuleRow
          label="Mute durante AUTO_TRADE"
          hint="Sospende alert non-critici quando live trading e' attivo"
          checked={local.mute_during_auto_trade}
          onChange={(v) => commit({ mute_during_auto_trade: v })}
        />

        <Separator className="my-5" />

        {/* Quiet hours */}
        <SectionHeader title="Quiet hours" />
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Da
            </label>
            <Input
              type="time"
              value={local.quiet_from}
              onChange={(e) => setLocal((s) => ({ ...s, quiet_from: e.target.value }))}
              onBlur={() => commit({ quiet_from: local.quiet_from })}
              className="mt-1.5"
            />
          </div>
          <div>
            <label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">A</label>
            <Input
              type="time"
              value={local.quiet_to}
              onChange={(e) => setLocal((s) => ({ ...s, quiet_to: e.target.value }))}
              onBlur={() => commit({ quiet_to: local.quiet_to })}
              className="mt-1.5"
            />
          </div>
        </div>
        {(local.quiet_from || local.quiet_to) && (
          <div className="mt-3 flex items-center gap-2 rounded-md border border-warn/30 bg-warn/10 p-2 text-[11px] text-warn">
            <BellRing className="h-3.5 w-3.5 shrink-0" />
            <span>
              Alert silenziati tra{" "}
              <span className="font-mono">{local.quiet_from || "—"}</span> e{" "}
              <span className="font-mono">{local.quiet_to || "—"}</span>
            </span>
          </div>
        )}
      </div>
    </Card>
  );
}

function SectionHeader({ title, badge }: { title: string; badge?: string }) {
  return (
    <div className="mb-3 flex items-center gap-2">
      <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</span>
      {badge && (
        <Badge variant="outline" className="font-mono text-[10px]">
          {badge}
        </Badge>
      )}
    </div>
  );
}

function ChannelRow({
  icon,
  label,
  hint,
  checked,
  onChange,
  disabled,
}: {
  icon: React.ReactNode;
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div
      className={cn(
        "mb-2 flex items-start gap-3 rounded-md border border-border bg-bg-2/40 p-3",
        disabled && "opacity-60",
      )}
    >
      <div className="text-fg-2">{icon}</div>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-fg">{label}</div>
        <div className="mt-0.5 text-[11px] text-fg-3">{hint}</div>
      </div>
      <Switch checked={checked} onCheckedChange={onChange} disabled={disabled} />
    </div>
  );
}

function RuleRow({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="mt-3 flex items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <div className="text-sm text-fg-2">{label}</div>
        {hint && <div className="mt-0.5 text-[11px] text-fg-3">{hint}</div>}
      </div>
      <Switch checked={checked} onCheckedChange={onChange} />
    </div>
  );
}
