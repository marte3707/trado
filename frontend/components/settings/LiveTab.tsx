"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, Check, Lock, Save, ShieldCheck, Unlock, X } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useUpdateSettings } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";
import type { AppSettings, Health } from "@/lib/api";

interface Props {
  health: Health;
  settings: AppSettings;
}

function toNum(v: unknown, fallback: number) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}
function toBool(v: unknown, fallback = false) {
  if (v == null) return fallback;
  return Boolean(v);
}

export function LiveTab({ health, settings }: Props) {
  const liveAllowed = Boolean(health.live_trading_allowed);
  const readiness = health.live_readiness;
  const network = health.network ?? "—";
  const isMainnet = network === "mainnet";

  const operation = (settings.operation_mode ?? {}) as Record<string, unknown>;
  const execution = (settings.live_execution ?? {}) as Record<string, unknown>;
  const killSwitch = (settings.kill_switch ?? {}) as Record<string, unknown>;

  const persisted = {
    enableLive: toBool(operation.enable_live_trading, health.enable_live_trading),
    enableMainnet: toBool(execution.enable_mainnet_live_trading, false),
    killEnabled: toBool(killSwitch.enabled, false),
    minAccount: toNum(execution.min_account_value_usd, 100),
    notionalCap: toNum(execution.order_notional_cap_usd, 250),
    minNotional: toNum(execution.min_order_notional_usd, 10),
    bumpToMin: toBool(execution.bump_to_min_notional, false),
    maxSlippage: toNum(execution.max_slippage_pct, 0.5),
    orderTtl: toNum(execution.order_expires_after_seconds, 30),
  };

  const [local, setLocal] = useState(persisted);
  const { mutate: updateSettings, isPending } = useUpdateSettings();

  useEffect(() => {
    setLocal(persisted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    persisted.enableLive,
    persisted.enableMainnet,
    persisted.killEnabled,
    persisted.minAccount,
    persisted.notionalCap,
    persisted.minNotional,
    persisted.bumpToMin,
    persisted.maxSlippage,
    persisted.orderTtl,
  ]);

  const dirty = Object.keys(persisted).some(
    (k) => local[k as keyof typeof local] !== persisted[k as keyof typeof persisted],
  );

  function handleSave() {
    updateSettings({
      operation_mode: {
        value: { ...operation, enable_live_trading: local.enableLive },
      },
      live_execution: {
        value: {
          ...execution,
          enable_mainnet_live_trading: local.enableMainnet,
          min_account_value_usd: local.minAccount,
          order_notional_cap_usd: local.notionalCap,
          min_order_notional_usd: local.minNotional,
          bump_to_min_notional: local.bumpToMin,
          max_slippage_pct: local.maxSlippage,
          order_expires_after_seconds: local.orderTtl,
        },
      },
      kill_switch: {
        value: { ...killSwitch, enabled: local.killEnabled },
      },
    } as Partial<AppSettings>);
  }

  return (
    <div className="space-y-5">
      {/* Hero status */}
      <Card
        className={cn(
          "p-6",
          liveAllowed ? "border-success/40 bg-success/5" : "border-warn/40 bg-warn/5",
        )}
      >
        <div className="flex items-start gap-4">
          {liveAllowed ? (
            <Unlock className="h-8 w-8 shrink-0 text-success" />
          ) : (
            <Lock className="h-8 w-8 shrink-0 text-warn" />
          )}
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="text-base font-semibold text-fg">
                Live trading {liveAllowed ? "sbloccato" : "bloccato"}
              </span>
              <Badge
                variant="outline"
                className={cn(
                  "font-mono uppercase",
                  isMainnet
                    ? "border-danger/40 bg-danger/10 text-danger"
                    : "border-info/30 bg-info/10 text-info",
                )}
              >
                {network}
              </Badge>
            </div>
            <p className="mt-1 text-xs text-fg-3">
              {liveAllowed
                ? "Tutti i guardrail sono verdi. L'engine puo' inviare ordini live a Hyperliquid."
                : "Almeno un check guardrail e' rosso. L'engine non invia ordini live."}
            </p>
          </div>
          <ShieldCheck className={cn("h-5 w-5", liveAllowed ? "text-success" : "text-fg-3")} />
        </div>
      </Card>

      {/* Master switches */}
      <Card className="p-6">
        <SectionHeader
          title="Master switches"
          hint="Flag binari che decidono se l'engine puo' o non puo' operare live"
        />
        <div className="mt-4 space-y-3">
          <ToggleRow
            label="enable_live_trading"
            hint="Master flag per consentire live trading. Persistito in DB e prevale su .env"
            checked={local.enableLive}
            onChange={(v) => setLocal((s) => ({ ...s, enableLive: v }))}
          />
          <ToggleRow
            label="enable_mainnet_live_trading"
            hint="Flag aggiuntivo richiesto SOLO su mainnet (cintura di sicurezza)"
            checked={local.enableMainnet}
            onChange={(v) => setLocal((s) => ({ ...s, enableMainnet: v }))}
            danger={local.enableMainnet && isMainnet}
          />
          <ToggleRow
            label="kill_switch"
            hint="Quando armato, blocca tutti gli ordini live (non chiude le posizioni aperte)"
            checked={local.killEnabled}
            onChange={(v) => setLocal((s) => ({ ...s, killEnabled: v }))}
            danger={local.killEnabled}
          />
          <ToggleRow
            label="bump_to_min_notional"
            hint="Se size calcolata e' sotto il minimo exchange, bumpa al min_order_notional_usd"
            checked={local.bumpToMin}
            onChange={(v) => setLocal((s) => ({ ...s, bumpToMin: v }))}
          />
        </div>
      </Card>

      {/* Execution caps */}
      <Card className="p-6">
        <SectionHeader
          title="Execution caps"
          hint="Limiti hard sull'esecuzione live (sicurezza capitale e slippage)"
        />
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          <Field
            label="Min account value"
            hint="Equity minima del conto live perche' l'engine possa operare"
            value={local.minAccount}
            step={50}
            suffix="$"
            onChange={(v) => setLocal((s) => ({ ...s, minAccount: v }))}
          />
          <Field
            label="Order notional cap"
            hint="Notional max per singolo ordine (in USD)"
            value={local.notionalCap}
            step={25}
            suffix="$"
            onChange={(v) => setLocal((s) => ({ ...s, notionalCap: v }))}
          />
          <Field
            label="Min order notional"
            hint="Notional minimo richiesto da Hyperliquid per perp"
            value={local.minNotional}
            step={1}
            suffix="$"
            onChange={(v) => setLocal((s) => ({ ...s, minNotional: v }))}
          />
          <Field
            label="Max slippage"
            hint="Slippage massimo tollerato sul fill (rifiuta oltre)"
            value={local.maxSlippage}
            step={0.05}
            suffix="%"
            onChange={(v) => setLocal((s) => ({ ...s, maxSlippage: v }))}
          />
          <Field
            label="Order TTL"
            hint="Tempo dopo il quale l'ordine pendente viene cancellato"
            value={local.orderTtl}
            step={5}
            suffix="s"
            onChange={(v) => setLocal((s) => ({ ...s, orderTtl: v }))}
          />
        </div>
      </Card>

      {/* Readiness checklist */}
      <Card className="p-6">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Readiness checklist
            </div>
            <div className="mt-0.5 text-xs text-fg-3">
              Tutti i prerequisiti che l&apos;engine valuta per consentire live trading
            </div>
          </div>
          {readiness?.checks && readiness.checks.length > 0 && (
            <Badge variant="outline" className="border-warn/40 bg-warn/10 font-mono text-warn">
              {readiness.checks.length} aperti
            </Badge>
          )}
        </div>

        <Separator className="my-4" />

        <div className="space-y-2">
          <ReadinessRow
            label="Account address configurato"
            ok={Boolean(readiness?.account_address_configured)}
          />
          <ReadinessRow
            label="Private key configurato"
            ok={Boolean(readiness?.private_key_configured)}
            hint="Caricato come variabile d'ambiente / secret"
          />
          <ReadinessRow
            label="ENABLE_LIVE_TRADING attivo"
            ok={Boolean(health.enable_live_trading)}
            hint="Flag .env / settings.operation_mode.enable_live_trading"
          />
          <ReadinessRow
            label="Operation mode = AUTO_TRADE"
            ok={health.mode === "AUTO_TRADE"}
            hint="Solo AUTO_TRADE consente live execution"
          />
          {isMainnet && (
            <ReadinessRow
              label="ENABLE_MAINNET_LIVE_TRADING attivo"
              ok={Boolean(readiness?.mainnet_live_enabled)}
              hint="Flag aggiuntivo richiesto SOLO su mainnet"
            />
          )}
        </div>

        {readiness?.checks && readiness.checks.length > 0 && (
          <>
            <Separator className="my-4" />
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Check aperti
            </div>
            <ul className="mt-2 space-y-1.5">
              {readiness.checks.map((c) => (
                <li
                  key={c}
                  className="flex items-start gap-2 rounded-md border border-warn/30 bg-warn/10 p-2.5 text-xs text-warn"
                >
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>{c}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>

      {isMainnet && (
        <Card className="border-danger/40 bg-danger/5 p-5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-danger" />
            <div>
              <div className="text-sm font-medium text-danger">Avviso mainnet</div>
              <div className="mt-1 text-xs text-fg-2">
                Stai per operare con capitale reale. Verifica i risk control, la presenza di kill
                switch attivo per emergenze, e l&apos;allineamento delle credenziali. Per spegnere il
                live trading senza riavviare i servizi, usa il pulsante <span className="font-mono">KILL</span> in topbar.
              </div>
            </div>
          </div>
        </Card>
      )}

      {dirty && (
        <Card className="sticky bottom-4 z-10 flex items-center gap-3 border-warn/40 bg-warn/10 px-5 py-3">
          <span className="text-sm text-warn">Modifiche non salvate</span>
          <div className="flex-1" />
          <Button size="sm" onClick={handleSave} disabled={isPending}>
            <Save className="mr-1.5 h-3.5 w-3.5" />
            {isPending ? "Salvataggio..." : "Salva live config"}
          </Button>
        </Card>
      )}
    </div>
  );
}

function ReadinessRow({
  label,
  ok,
  hint,
}: {
  label: string;
  ok: boolean;
  hint?: string;
}) {
  return (
    <div className="flex items-start gap-3 rounded-md border border-border bg-bg-2/40 p-3">
      <div
        className={cn(
          "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full",
          ok ? "bg-success/15 text-success" : "bg-danger/15 text-danger",
        )}
      >
        {ok ? <Check className="h-3 w-3" /> : <X className="h-3 w-3" />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-sm text-fg">{label}</div>
        {hint && <div className="mt-0.5 text-[11px] text-fg-3">{hint}</div>}
      </div>
    </div>
  );
}

function ToggleRow({
  label,
  hint,
  checked,
  onChange,
  danger,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  danger?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-start justify-between gap-3 rounded-md border border-border bg-bg-2/40 p-3",
        danger && "border-danger/40 bg-danger/5",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className={cn("font-mono text-sm font-medium", danger ? "text-danger" : "text-fg")}>
          {label}
        </div>
        {hint && <div className="mt-0.5 text-[11px] text-fg-3">{hint}</div>}
      </div>
      <Switch checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

function Field({
  label,
  hint,
  value,
  step,
  onChange,
  suffix,
}: {
  label: string;
  hint: string;
  value: number;
  step: number;
  onChange: (v: number) => void;
  suffix?: string;
}) {
  return (
    <div>
      <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</Label>
      <div className="mt-2 flex items-center gap-2">
        <Input
          type="number"
          value={value}
          step={step}
          onChange={(e) => onChange(Number(e.target.value))}
          className="font-mono"
        />
        {suffix && <span className="font-mono text-sm text-fg-3">{suffix}</span>}
      </div>
      <div className="mt-1.5 text-[11px] text-fg-3">{hint}</div>
    </div>
  );
}

function SectionHeader({ title, hint }: { title: string; hint?: string }) {
  return (
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</div>
      {hint && <div className="mt-0.5 text-xs text-fg-3">{hint}</div>}
    </div>
  );
}
