import { asNumber } from "@/lib/performance";

/** Segno del PnL in base al lato: una posizione short guadagna quando il prezzo scende. */
export function sideMultiplier(side: string): 1 | -1 {
  return side.toLowerCase() === "short" ? -1 : 1;
}

/**
 * PnL non realizzato di una posizione dato il prezzo mark corrente.
 *
 * Ritorna `null` quando uno degli input non è un numero valido: spetta al
 * chiamante decidere il fallback (tipicamente il campo `unrealized_pnl` del
 * backend, oppure 0). Single source of truth condivisa tra dashboard e header
 * realtime, prima duplicata in più componenti.
 */
export function unrealizedPnl(params: {
  entryPrice: unknown;
  size: unknown;
  markPrice: unknown;
  side: string;
}): number | null {
  const entry = asNumber(params.entryPrice);
  const size = asNumber(params.size);
  const mark = asNumber(params.markPrice);
  if (entry == null || size == null || mark == null) return null;
  return (mark - entry) * Math.abs(size) * sideMultiplier(params.side);
}
