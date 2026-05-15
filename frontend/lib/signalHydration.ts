import { apiGet, Position, Signal } from "@/lib/api";

function asSignalId(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
}

function openPositionSourceSignalIds(positions: Position[]): number[] {
  const ids = new Set<number>();
  for (const position of positions) {
    if (position.status.toLowerCase() !== "open") continue;
    const signalId = asSignalId(position.metadata_json?.source_signal_id);
    if (signalId !== null) ids.add(signalId);
  }
  return [...ids];
}

function sortSignals(a: Signal, b: Signal): number {
  const aTime = new Date(a.created_at).getTime();
  const bTime = new Date(b.created_at).getTime();
  const diff = (Number.isFinite(bTime) ? bTime : 0) - (Number.isFinite(aTime) ? aTime : 0);
  return diff !== 0 ? diff : b.id - a.id;
}

export async function hydrateOpenPositionSourceSignals(signals: Signal[], positions: Position[]): Promise<Signal[]> {
  const existingIds = new Set(signals.map((signal) => signal.id));
  const missingIds = openPositionSourceSignalIds(positions).filter((id) => !existingIds.has(id));
  if (missingIds.length === 0) return signals;

  const linkedSignals = await Promise.all(
    missingIds.map((id) => apiGet<Signal>(`/api/signals/${id}`).catch(() => null))
  );
  const merged = [...signals];
  const mergedIds = new Set(existingIds);
  for (const signal of linkedSignals) {
    if (!signal || mergedIds.has(signal.id)) continue;
    merged.push(signal);
    mergedIds.add(signal.id);
  }
  return merged.sort(sortSignals);
}
