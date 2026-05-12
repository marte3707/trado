import { Market } from "@/lib/api";

export function MarketList({ markets }: { markets: Market[] }) {
  const spot = markets.filter((market) => market.market_type === "spot").length;
  const perp = markets.filter((market) => market.market_type === "perp").length;
  return (
    <section className="bg-surface rounded-2xl shadow-card overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-divider">
        <h2 className="text-base font-semibold text-text">Watchlist</h2>
        <span className="text-xs text-muted">{spot} spot / {perp} perp</span>
      </div>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-2 p-4">
        {markets.slice(0, 36).map((market) => (
          <div
            key={`${market.market_type}-${market.symbol}`}
            className="flex items-center justify-between gap-2 px-3 py-2.5 rounded-xl bg-surface-2"
          >
            <strong className="text-sm font-semibold text-text">{market.symbol}</strong>
            <span className="text-xs text-muted">{market.market_type}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
