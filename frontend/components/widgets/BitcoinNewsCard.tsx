"use client";

import { ExternalLink, Newspaper } from "lucide-react";

import { fmt } from "@/components/atoms";
import { Card } from "@/components/ui/card";
import type { MarketDashboardSnapshot } from "@/lib/api";

export function BitcoinNewsCard({
  snapshot,
}: {
  snapshot?: MarketDashboardSnapshot | null;
}) {
  const news = snapshot?.news ?? [];
  const updated = snapshot?.generated_at ?? null;

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold text-fg">
            <Newspaper className="h-4 w-4 text-fg-3" />
            Ultime notizie Bitcoin
          </div>
          <p className="mt-1 text-xs text-fg-3">
            CoinMarketCap News · aggiornato {fmt.relTime(updated)}
          </p>
        </div>
        <a
          href={snapshot?.source_url ?? "https://coinmarketcap.com/currencies/bitcoin/#News"}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-2 px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-fg-3 transition-colors hover:text-fg"
        >
          Fonte
          <ExternalLink className="h-3 w-3" />
        </a>
      </div>

      {news.length > 0 ? (
        <div className="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-2">
          {news.map((item) => (
            <a
              key={`${item.title}-${item.url}`}
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className="group min-w-0 rounded-md border border-border bg-bg-2/45 p-3 transition-colors hover:border-fg-4 hover:bg-bg-2"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm font-medium leading-snug text-fg group-hover:text-accent">
                    {item.title}
                  </div>
                  {item.excerpt && (
                    <p className="mt-1.5 max-h-10 overflow-hidden text-xs leading-5 text-fg-3">
                      {item.excerpt}
                    </p>
                  )}
                </div>
                <ExternalLink className="mt-0.5 h-3.5 w-3.5 shrink-0 text-fg-4" />
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] uppercase tracking-wider text-fg-4">
                <span>{item.source ?? "CoinMarketCap"}</span>
                {item.published_at && <span>{fmt.relTime(item.published_at)}</span>}
              </div>
            </a>
          ))}
        </div>
      ) : (
        <div className="mt-4 rounded-md border border-border bg-bg-2/45 p-4 text-sm text-fg-3">
          News non disponibili nell&apos;ultimo aggiornamento. Il prossimo refresh riprova entro 15 minuti.
        </div>
      )}
    </Card>
  );
}
