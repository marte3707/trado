"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, ExternalLink, Save } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Switch } from "@/components/ui/switch";
import {
  useCoinGeckoUsage,
  useCoinMarketCapUsage,
} from "@/lib/hooks/queries";
import { useUpdateSettings } from "@/lib/hooks/mutations";
import type { AppSettings } from "@/lib/api";
import { cn } from "@/lib/utils";

function asNumber(v: unknown): number | null {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function flatten(
  obj: Record<string, unknown>,
  prefix = "",
): Array<{ path: string; value: unknown }> {
  const out: Array<{ path: string; value: unknown }> = [];
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (isPlainObject(v)) {
      out.push(...flatten(v, path));
    } else {
      out.push({ path, value: v });
    }
  }
  return out;
}

function valueLabel(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (Array.isArray(v)) return v.length === 0 ? "—" : `${v.length} items`;
  return String(v);
}

export function IntegrationsTab({ settings }: { settings: AppSettings }) {
  const { data: cmc } = useCoinMarketCapUsage();
  const { data: cg } = useCoinGeckoUsage();
  const theBull = (settings.the_bull ?? {}) as Record<string, unknown>;
  const theBullState = (settings.the_bull_state ?? {}) as Record<string, unknown>;
  const lastTheBullCandidate = isPlainObject(theBullState.last_candidate)
    ? theBullState.last_candidate
    : null;
  const lastTheBullChartCandidate = isPlainObject(theBullState.last_chart_candidate)
    ? theBullState.last_chart_candidate
    : null;
  const persisted = {
    enabled: Boolean(theBull.enabled ?? false),
    executionMode: String(theBull.execution_mode ?? "paper"),
    timeframe: String(theBull.timeframe ?? "4h"),
    maxSignalAgeMinutes: asNumber(theBull.max_signal_age_minutes) ?? 60,
    riskPct: (asNumber(theBull.risk_multiplier) ?? 0.5) * 100,
    maxOpenPositions: asNumber(theBull.max_open_positions) ?? 3,
    closeOnProfit: Boolean(theBull.close_on_profit_update ?? true),
    semanticAuditEnabled: Boolean(theBull.semantic_audit_enabled ?? true),
    expandedEntryExecution: Boolean(theBull.expanded_entry_execution ?? true),
    chartAnalysisEnabled: Boolean(theBull.chart_analysis_enabled ?? true),
    chartZoneExecution: Boolean(theBull.chart_zone_execution ?? true),
    chartOcrFallbackExecution: Boolean(theBull.chart_ocr_fallback_execution ?? false),
    spotFallbackEnabled: Boolean(theBull.spot_fallback_enabled ?? false),
  };
  const [local, setLocal] = useState(persisted);
  const { mutate: updateSettings, isPending } = useUpdateSettings();

  useEffect(() => {
    setLocal(persisted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(persisted)]);

  const dirty = JSON.stringify(local) !== JSON.stringify(persisted);

  function saveTheBull() {
    updateSettings({
      the_bull: {
        value: {
          ...theBull,
          enabled: local.enabled,
          channel_username: "thebull_crypto",
          execution_mode: local.executionMode,
          timeframe: local.timeframe,
          max_signal_age_minutes: local.maxSignalAgeMinutes,
          risk_multiplier: local.riskPct / 100,
          max_open_positions: local.maxOpenPositions,
          close_on_profit_update: local.closeOnProfit,
          semantic_audit_enabled: local.semanticAuditEnabled,
          expanded_entry_execution: local.expandedEntryExecution,
          chart_analysis_enabled: local.chartAnalysisEnabled,
          chart_zone_execution: local.chartZoneExecution,
          chart_ocr_fallback_execution: local.chartOcrFallbackExecution,
          spot_fallback_enabled: local.spotFallbackEnabled,
        },
      },
    } as Partial<AppSettings>);
  }

  return (
    <div className="space-y-4">
      <Card className="p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <div className="text-base font-semibold text-fg">The Bull</div>
              <Badge
                variant="outline"
                className={cn(
                  "font-mono uppercase",
                  local.enabled
                    ? "border-success/40 bg-success/10 text-success"
                    : "border-border bg-bg-2 text-fg-3",
                )}
              >
                {local.enabled ? "Monitor attivo" : "Disattivato"}
              </Badge>
            </div>
            <p className="mt-1 max-w-2xl text-xs text-fg-3">
              Acquisisce le call pubbliche del canale. Le call estese e le zone annotate nei grafici
              hanno controlli separati, così da restare tracciabili senza ridurre i guardrail.
            </p>
          </div>
          <Switch
            checked={local.enabled}
            onCheckedChange={(enabled: boolean) => setLocal((value) => ({ ...value, enabled }))}
          />
        </div>

        <div className="mt-5 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-5">
          <div>
            <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Esecuzione
            </Label>
            <select
              value={local.executionMode}
              onChange={(event) =>
                setLocal((value) => ({ ...value, executionMode: event.target.value }))
              }
              className="mt-2 h-9 w-full rounded-md border border-border bg-bg-2 px-3 text-sm text-fg"
            >
              <option value="paper">Solo paper</option>
              <option value="live">Paper + live</option>
            </select>
          </div>
          <div>
            <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Piano rischio
            </Label>
            <select
              value={local.timeframe}
              onChange={(event) =>
                setLocal((value) => ({ ...value, timeframe: event.target.value }))
              }
              className="mt-2 h-9 w-full rounded-md border border-border bg-bg-2 px-3 font-mono text-sm text-fg"
            >
              <option value="1h">1h</option>
              <option value="4h">4h · consigliato</option>
              <option value="1d">1d</option>
            </select>
          </div>
          <div>
            <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Età massima
            </Label>
            <div className="mt-2 flex items-center gap-2">
              <Input
                type="number"
                min={1}
                max={120}
                value={local.maxSignalAgeMinutes}
                onChange={(event) =>
                  setLocal((value) => ({
                    ...value,
                    maxSignalAgeMinutes: Number(event.target.value),
                  }))
                }
                className="font-mono"
              />
              <span className="text-xs text-fg-3">min</span>
            </div>
          </div>
          <div>
            <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Quota rischio globale
            </Label>
            <div className="mt-2 flex items-center gap-2">
              <Input
                type="number"
                min={5}
                max={100}
                step={5}
                value={local.riskPct}
                onChange={(event) =>
                  setLocal((value) => ({ ...value, riskPct: Number(event.target.value) }))
                }
                className="font-mono"
              />
              <span className="text-xs text-fg-3">%</span>
            </div>
          </div>
          <div>
            <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Max posizioni fonte
            </Label>
            <Input
              type="number"
              min={1}
              max={20}
              value={local.maxOpenPositions}
              onChange={(event) =>
                setLocal((value) => ({
                  ...value,
                  maxOpenPositions: Number(event.target.value),
                }))
              }
              className="mt-2 font-mono"
            />
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-bg-2 px-3 py-3">
          <div>
            <div className="text-xs font-medium text-fg">Chiudi sul riepilogo profitto</div>
            <div className="mt-0.5 text-[11px] text-fg-3">
              Un reply “+N% from entry”, stop o chiusura esplicita diventa un’uscita aggiuntiva.
            </div>
          </div>
          <Switch
            checked={local.closeOnProfit}
            onCheckedChange={(closeOnProfit: boolean) =>
              setLocal((value) => ({ ...value, closeOnProfit }))
            }
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-info/30 bg-info/5 px-3 py-3">
          <div>
            <div className="text-xs font-medium text-fg">Rilevamento call estese</div>
            <div className="mt-0.5 text-[11px] text-fg-3">
              Rileva “buy zone”, “buy the dip”, acquisti su supporto, breakout/retest,
              “buying more” e watchlist.
            </div>
          </div>
          <Switch
            checked={local.semanticAuditEnabled}
            onCheckedChange={(semanticAuditEnabled: boolean) =>
              setLocal((value) => ({ ...value, semanticAuditEnabled }))
            }
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-warn/30 bg-warn/5 px-3 py-3">
          <div>
            <div className="text-xs font-medium text-fg">Esegui call estese</div>
            <div className="mt-0.5 text-[11px] text-fg-3">
              Rende eseguibili buy zone, supporto, breakout, double bottom, bullish bias e watchlist.
              Su una posizione live The Bull già aperta, “buying more” e “buy the dip”
              consentono al massimo due aggiunte del 30% della quantità iniziale ciascuna,
              solo se passano minimo d’ordine, rischio e protezione stop. Spot escluso.
            </div>
          </div>
          <Switch
            checked={local.expandedEntryExecution}
            onCheckedChange={(expandedEntryExecution: boolean) =>
              setLocal((value) => ({ ...value, expandedEntryExecution }))
            }
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-info/30 bg-info/5 px-3 py-3">
          <div>
            <div className="text-xs font-medium text-fg">Analisi delle zone nel grafico</div>
            <div className="mt-0.5 text-[11px] text-fg-3">
              Legge immagini pubbliche di Telegram con limiti di prezzo visibili usando Vision di macOS,
              senza API né costi. Le zone con prezzo corrente dentro il range passano al normale piano rischio.
            </div>
          </div>
          <Switch
            checked={local.chartAnalysisEnabled}
            onCheckedChange={(chartAnalysisEnabled: boolean) =>
              setLocal((value) => ({ ...value, chartAnalysisEnabled }))
            }
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-warn/30 bg-warn/5 px-3 py-3">
          <div>
            <div className="text-xs font-medium text-fg">Esegui zone lette dal grafico</div>
            <div className="mt-0.5 text-[11px] text-fg-3">
              Apre solo se il midpoint corrente è dentro la zona riconosciuta; applica comunque età,
              rischio, cap posizioni e tutti i guardrail globali.
            </div>
          </div>
          <Switch
            checked={local.chartZoneExecution}
            onCheckedChange={(chartZoneExecution: boolean) =>
              setLocal((value) => ({ ...value, chartZoneExecution }))
            }
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-info/30 bg-info/5 px-3 py-3">
          <div>
            <div className="text-xs font-medium text-fg">Fallback se il grafico non è leggibile</div>
            <div className="mt-0.5 text-[11px] text-fg-3">
              Se Vision non ricava una zona, una call con ticker nella didascalia usa il prezzo corrente
              entro l’età massima configurata. Non aggira mai il piano rischio, SL/TP o i guardrail live.
            </div>
          </div>
          <Switch
            checked={local.chartOcrFallbackExecution}
            onCheckedChange={(chartOcrFallbackExecution: boolean) =>
              setLocal((value) => ({ ...value, chartOcrFallbackExecution }))
            }
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-warn/30 bg-warn/5 px-3 py-3">
          <div>
            <div className="text-xs font-medium text-fg">Fallback spot Hyperliquid</div>
            <div className="mt-0.5 text-[11px] text-fg-3">
              Se il perpetual non esiste, consente solo acquisti spot quotati in USDC per The Bull:
              niente leva, stop obbligatorio e nessun take profit automatico.
            </div>
          </div>
          <Switch
            checked={local.spotFallbackEnabled}
            onCheckedChange={(spotFallbackEnabled: boolean) =>
              setLocal((value) => ({ ...value, spotFallbackEnabled }))
            }
          />
        </div>

        {local.executionMode === "live" && (
          <div className="mt-4 flex gap-2 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[11px] text-warn">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            Il live richiede comunque AUTO_TRADE, credenziali valide, opt-in mainnet, kill switch disarmato
            e tutti i guardrail Trado verdi.
          </div>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-fg-3">
          <span>
            Ultimo poll: {theBullState.last_polled_at ? new Date(String(theBullState.last_polled_at)).toLocaleString("it-IT") : "—"}
          </span>
          <span className="font-mono">Cursor: {String(theBullState.last_message_id ?? "—")}</span>
          {lastTheBullCandidate && (
            <a
              href={String(lastTheBullCandidate.message_url ?? "https://t.me/thebull_crypto")}
              target="_blank"
              rel="noreferrer"
              className="text-warn hover:underline"
            >
              Ultimo candidato: ${String(lastTheBullCandidate.symbol ?? "—")} · {String(lastTheBullCandidate.intent ?? "review")}
            </a>
          )}
          {lastTheBullChartCandidate && (
            <a
              href={String(lastTheBullChartCandidate.message_url ?? "https://t.me/thebull_crypto")}
              target="_blank"
              rel="noreferrer"
              className="text-info hover:underline"
            >
              Ultima zona grafico: ${String(lastTheBullChartCandidate.symbol ?? "—")} · {String(lastTheBullChartCandidate.confidence ?? "review")}
            </a>
          )}
          {Boolean(theBullState.last_error) && (
            <span className="text-danger">{String(theBullState.last_error)}</span>
          )}
          <a
            href="https://t.me/thebull_crypto"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-info hover:underline"
          >
            Apri canale <ExternalLink className="h-3 w-3" />
          </a>
        </div>

        {dirty && (
          <div className="mt-4 flex justify-end">
            <Button size="sm" onClick={saveTheBull} disabled={isPending}>
              <Save className="mr-1.5 h-3.5 w-3.5" />
              {isPending ? "Salvataggio..." : "Salva integrazione"}
            </Button>
          </div>
        )}
      </Card>

      <Card className="p-6">
        <SectionHeader
          title="CoinMarketCap"
          enabled={cmc?.enabled}
          available={cmc?.available}
          checkedAt={cmc?.checked_at}
          source={cmc?.source}
        />
        {cmc ? (
          <div className="mt-4 space-y-3">
            <UsageRows usage={cmc.usage as Record<string, unknown>} plan={cmc.plan as Record<string, unknown>} />
            {cmc.disabled_reason && (
              <div className="rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-[11px] text-warn">
                {cmc.disabled_reason}{" "}
                {cmc.disabled_until && (
                  <span className="text-fg-3">· fino a {cmc.disabled_until}</span>
                )}
              </div>
            )}
          </div>
        ) : (
          <EmptyHint />
        )}
      </Card>

      <Card className="p-6">
        <SectionHeader
          title="CoinGecko"
          enabled={cg?.enabled}
          checkedAt={cg?.checked_at}
          source={cg?.source}
        />
        {cg ? (
          <div className="mt-4">
            <UsageRows usage={cg.data as Record<string, unknown>} plan={undefined} />
          </div>
        ) : (
          <EmptyHint />
        )}
      </Card>

      <Card className="p-5">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Telegram & Push
        </div>
        <p className="mt-1 text-xs text-fg-3">
          La configurazione canali notifica (Telegram, Web Push, Dashboard) si trova nella schermata{" "}
          <span className="inline-flex items-center gap-1 font-mono text-fg-2">
            <ExternalLink className="h-3 w-3" />
            Alert center
          </span>{" "}
          → pannello sinistro.
        </p>
      </Card>
    </div>
  );
}

function SectionHeader({
  title,
  enabled,
  available,
  checkedAt,
  source,
}: {
  title: string;
  enabled?: boolean;
  available?: boolean;
  checkedAt?: string | null;
  source?: string;
}) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <div className="text-base font-semibold text-fg">{title}</div>
        <div className="mt-0.5 text-xs text-fg-3">
          Source: <span className="font-mono">{source ?? "—"}</span>
          {checkedAt && (
            <span className="ml-2">· check {new Date(checkedAt).toLocaleString("it-IT")}</span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2">
        {enabled != null && (
          <Badge
            variant="outline"
            className={cn(
              "font-mono uppercase",
              enabled
                ? "border-success/40 bg-success/10 text-success"
                : "border-border bg-bg-2 text-fg-3",
            )}
          >
            {enabled ? "Enabled" : "Disabled"}
          </Badge>
        )}
        {available != null && (
          <Badge
            variant="outline"
            className={cn(
              "font-mono uppercase",
              available
                ? "border-success/40 bg-success/10 text-success"
                : "border-danger/40 bg-danger/10 text-danger",
            )}
          >
            {available ? "Available" : "Down"}
          </Badge>
        )}
      </div>
    </div>
  );
}

function UsageRows({
  usage,
  plan,
}: {
  usage: Record<string, unknown> | undefined;
  plan: Record<string, unknown> | undefined;
}) {
  if (!usage || Object.keys(usage).length === 0) {
    return <EmptyHint />;
  }
  // Flatten nested objects in path -> value pairs (es. CoinGecko data ha sub-keys nidificate).
  const entries = flatten(usage);
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      {entries.map(({ path, value }) => {
        const used = asNumber(value);
        const limit = asNumber(plan?.[`${path}_limit`] ?? plan?.[path]);
        const pct = used != null && limit != null && limit > 0 ? (used / limit) * 100 : null;
        return (
          <div key={path} className="rounded-md border border-border bg-bg-2/30 p-3">
            <div className="flex items-center justify-between gap-2">
              <span
                className="truncate font-mono text-xs uppercase text-fg-3"
                title={path}
              >
                {path}
              </span>
              <span className="shrink-0 font-mono text-sm text-fg">
                {used != null ? used.toLocaleString("en-US") : valueLabel(value)}
                {limit != null && <span className="text-fg-3">/{limit}</span>}
              </span>
            </div>
            {pct != null && (
              <Progress
                value={pct}
                className="mt-2"
                indicatorClassName={cn(
                  pct >= 90 && "bg-danger",
                  pct >= 70 && pct < 90 && "bg-warn",
                )}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

function EmptyHint() {
  return (
    <div className="mt-3 text-xs text-fg-3">Nessun dato disponibile per questa integrazione.</div>
  );
}
