"use client";

import { useMemo } from "react";

import { useBotFunnel } from "@/lib/hooks/queries";

const CAPITAL_LABEL: Record<string, string> = {
  under_1k: "< $1.000",
  "1k_to_10k": "$1.000 – $10.000",
  over_10k: "> $10.000",
  skipped: "Non dichiarato",
};

const STAGE_LABEL: Record<string, string> = {
  started: "Started",
  language_set: "Lingua scelta",
  capital_set: "Capitale dichiarato",
  completed: "Completato (invito inviato)",
  declined: "Disclaimer rifiutato",
  ack_no_link: "Accept · link fallito",
};

const STAGE_ORDER = ["started", "language_set", "capital_set", "completed", "declined", "ack_no_link"];

function pct(n: number, total: number): string {
  if (total <= 0) return "—";
  return `${((n / total) * 100).toFixed(1)}%`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short" });
}

export function FunnelScreen() {
  const { data, isLoading, error, refetch, isFetching } = useBotFunnel();

  const stageRows = useMemo(() => {
    if (!data) return [];
    const known = new Set(STAGE_ORDER);
    const extra = Object.keys(data.by_stage).filter((k) => !known.has(k));
    return [...STAGE_ORDER, ...extra]
      .filter((k) => (data.by_stage[k] ?? 0) > 0)
      .map((k) => ({ key: k, label: STAGE_LABEL[k] ?? k, count: data.by_stage[k] ?? 0 }));
  }, [data]);

  const sourceRows = useMemo(() => {
    if (!data) return [];
    return Object.entries(data.source_funnel)
      .sort((a, b) => b[1].started - a[1].started)
      .map(([source, v]) => ({
        source,
        started: v.started,
        completed: v.completed,
        rate: v.started > 0 ? v.completed / v.started : 0,
      }));
  }, [data]);

  const capitalRows = useMemo(() => {
    if (!data) return [];
    return Object.entries(data.by_capital)
      .sort((a, b) => b[1] - a[1])
      .map(([key, count]) => ({ key, label: CAPITAL_LABEL[key] ?? key, count }));
  }, [data]);

  if (isLoading) {
    return <div className="p-6 text-sm text-fg-3">Caricamento funnel…</div>;
  }
  if (error || !data) {
    return (
      <div className="p-6 text-sm text-danger">
        Errore nel caricamento del funnel.{" "}
        <button onClick={() => refetch()} className="underline">
          Riprova
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Funnel Telegram</h1>
          <p className="text-xs text-fg-3">
            Iscritti, conversione e provenienza dal bot di onboarding.
          </p>
        </div>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="rounded-md border border-border-strong px-3 py-1.5 text-xs hover:bg-bg-hover disabled:opacity-50"
        >
          {isFetching ? "Aggiornamento…" : "Aggiorna"}
        </button>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Kpi label="Utenti totali" value={data.total} />
        <Kpi label="Completati" value={data.completed} sub={pct(data.completed, data.total)} />
        <Kpi
          label="Completati ultime 24h"
          value={data.completed_last_24h}
          sub={`${data.completed_last_7d} negli ultimi 7gg`}
        />
        <Kpi
          label="Rifiutati / sospesi"
          value={data.declined}
          sub={`${data.paused} in pause`}
          tone={data.declined > 0 ? "warn" : "neutral"}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Stage funnel */}
        <Card title="Stage onboarding">
          <table className="w-full text-sm">
            <tbody>
              {stageRows.map((row) => (
                <tr key={row.key} className="border-t border-border first:border-0">
                  <td className="py-1.5 text-fg-2">{row.label}</td>
                  <td className="py-1.5 text-right font-mono">{row.count}</td>
                  <td className="py-1.5 pl-3 text-right text-xs text-fg-3">
                    {pct(row.count, data.total)}
                  </td>
                </tr>
              ))}
              {stageRows.length === 0 && (
                <tr>
                  <td className="py-3 text-fg-3">Nessun utente ancora.</td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>

        {/* Capital distribution */}
        <Card title="Capitale dichiarato">
          <table className="w-full text-sm">
            <tbody>
              {capitalRows.map((row) => (
                <tr key={row.key} className="border-t border-border first:border-0">
                  <td className="py-1.5 text-fg-2">{row.label}</td>
                  <td className="py-1.5 text-right font-mono">{row.count}</td>
                  <td className="py-1.5 pl-3 text-right text-xs text-fg-3">
                    {pct(row.count, data.total)}
                  </td>
                </tr>
              ))}
              {capitalRows.length === 0 && (
                <tr>
                  <td className="py-3 text-fg-3">Nessun dato.</td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>

        {/* Sources */}
        <Card title="Provenienza" className="lg:col-span-2">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-fg-3">
                <th className="pb-2">Source</th>
                <th className="pb-2 text-right">Started</th>
                <th className="pb-2 text-right">Completati</th>
                <th className="pb-2 text-right">Conversion</th>
              </tr>
            </thead>
            <tbody>
              {sourceRows.map((row) => (
                <tr key={row.source} className="border-t border-border">
                  <td className="py-1.5 font-mono text-fg-2">{row.source}</td>
                  <td className="py-1.5 text-right font-mono">{row.started}</td>
                  <td className="py-1.5 text-right font-mono">{row.completed}</td>
                  <td className="py-1.5 text-right font-mono text-fg-3">
                    {pct(row.completed, row.started)}
                  </td>
                </tr>
              ))}
              {sourceRows.length === 0 && (
                <tr>
                  <td className="py-3 text-fg-3" colSpan={4}>
                    Nessuna source registrata.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>

        {/* Recent users */}
        <Card title="Ultimi 20 iscritti" className="lg:col-span-2">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-fg-3">
                  <th className="pb-2">Username</th>
                  <th className="pb-2">Lang</th>
                  <th className="pb-2">Capitale</th>
                  <th className="pb-2">Source</th>
                  <th className="pb-2">Stage</th>
                  <th className="pb-2 text-right">Iscritto</th>
                </tr>
              </thead>
              <tbody>
                {data.recent.map((u, i) => (
                  <tr key={i} className="border-t border-border">
                    <td className="py-1.5 font-mono">
                      {u.telegram_username ? `@${u.telegram_username}` : u.first_name || "—"}
                    </td>
                    <td className="py-1.5 text-fg-3">{u.language}</td>
                    <td className="py-1.5 text-fg-3">
                      {u.trade_capital ? CAPITAL_LABEL[u.trade_capital] ?? u.trade_capital : "—"}
                    </td>
                    <td className="py-1.5 font-mono text-fg-3">{u.source ?? "—"}</td>
                    <td className="py-1.5 text-fg-3">{STAGE_LABEL[u.onboarding_stage] ?? u.onboarding_stage}</td>
                    <td className="py-1.5 text-right text-xs text-fg-3">
                      {fmtDate(u.created_at)}
                    </td>
                  </tr>
                ))}
                {data.recent.length === 0 && (
                  <tr>
                    <td className="py-3 text-fg-3" colSpan={6}>
                      Nessun iscritto ancora.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  );
}

function Kpi({
  label,
  value,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: number | string;
  sub?: string;
  tone?: "neutral" | "warn";
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-1 p-4">
      <div className="text-xs uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={`mt-1 font-mono text-2xl ${tone === "warn" ? "text-warn" : "text-fg"}`}
      >
        {value}
      </div>
      {sub && <div className="text-xs text-fg-3">{sub}</div>}
    </div>
  );
}

function Card({
  title,
  className,
  children,
}: {
  title: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`rounded-lg border border-border bg-bg-1 p-4 ${className ?? ""}`}>
      <div className="mb-3 text-xs font-semibold uppercase tracking-wider text-fg-3">
        {title}
      </div>
      {children}
    </div>
  );
}
