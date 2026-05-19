"use client";

import { createChart, ColorType, HistogramData, IChartApi, ISeriesApi, LineStyle, LogicalRange, SeriesMarker, Time } from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { Health, Market, Signal } from "@/lib/api";

type Candle = {
  open_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
};

type ChartCandle = {
  time: Time;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

type LinePoint = {
  time: Time;
  value: number;
};

type CandlestickPattern = {
  time: Time;
  pattern: string;
  direction: "LONG" | "SHORT";
  confidence: number;
};

type WsCandle = {
  t: number;
  T: number;
  s: string;
  i: string;
  o: number | string;
  c: number | string;
  h: number | string;
  l: number | string;
  v: number | string;
  n: number;
};

type WsMessage =
  | { channel: "candle"; data: WsCandle | WsCandle[] }
  | { channel: "subscriptionResponse"; data: unknown }
  | { channel: string; data: unknown };

export type LiveChartUpdate = {
  price: number;
  baseVolumeDelta: number;
  quoteVolumeDelta: number;
  candleTime: number;
  candle?: Candle;
};

function wsUrl(network: Health["network"]) {
  return network === "mainnet" ? "wss://api.hyperliquid.xyz/ws" : "wss://api.hyperliquid-testnet.xyz/ws";
}

function marketCoin(market: Market | null) {
  const coin = market?.metadata_json?.hl_coin;
  return typeof coin === "string" ? coin : market?.symbol ?? "";
}

function toChartCandle(candle: Candle): ChartCandle {
  return {
    time: Math.floor(new Date(candle.open_time).getTime() / 1000) as Time,
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
    volume: Number(candle.volume)
  };
}

function toApiCandle(candle: ChartCandle): Candle {
  return {
    open_time: new Date(Number(candle.time) * 1000).toISOString(),
    open: String(candle.open),
    high: String(candle.high),
    low: String(candle.low),
    close: String(candle.close),
    volume: String(candle.volume)
  };
}

function toChartCandleFromWs(candle: WsCandle): ChartCandle | null {
  const open = Number(candle.o);
  const high = Number(candle.h);
  const low = Number(candle.l);
  const close = Number(candle.c);
  const volume = Number(candle.v);
  const time = Math.floor(candle.t / 1000);
  if (![time, open, high, low, close, volume].every(Number.isFinite)) return null;
  return { time: time as Time, open, high, low, close, volume };
}

function normalizeCandles(candles: Candle[]): ChartCandle[] {
  const byTime = new Map<number, ChartCandle>();
  for (const candle of candles) {
    const parsed = toChartCandle(candle);
    if (![parsed.open, parsed.high, parsed.low, parsed.close, parsed.volume, Number(parsed.time)].every(Number.isFinite)) continue;
    byTime.set(Number(parsed.time), parsed);
  }
  return [...byTime.values()].sort((a, b) => Number(a.time) - Number(b.time));
}

function mergeRestCandlesWithLive(restCandles: ChartCandle[], existingCandles: ChartCandle[]) {
  if (restCandles.length === 0 || existingCandles.length === 0) return restCandles;
  const byTime = new Map<number, ChartCandle>();
  for (const candle of restCandles) byTime.set(Number(candle.time), candle);
  const latestRestTime = Number(restCandles.at(-1)!.time);

  for (const candle of existingCandles) {
    const time = Number(candle.time);
    const restCandle = byTime.get(time);
    if (time > latestRestTime) {
      byTime.set(time, candle);
    } else if (restCandle && candle.volume > restCandle.volume) {
      byTime.set(time, candle);
    }
  }

  return [...byTime.values()].sort((a, b) => Number(a.time) - Number(b.time));
}

function lastNumber(values: (number | null)[]) {
  for (let index = values.length - 1; index >= 0; index -= 1) {
    if (values[index] !== null) return values[index];
  }
  return null;
}

function lineData(candles: ChartCandle[], values: (number | null)[]): LinePoint[] {
  return candles
    .map((candle, index) => {
      const value = values[index];
      return value === null ? null : { time: candle.time, value };
    })
    .filter((point): point is LinePoint => point !== null);
}

function volumeData(candles: ChartCandle[], longColor: string, shortColor: string): HistogramData[] {
  return candles.map((candle) => ({
    time: candle.time,
    value: candle.volume,
    color: candle.close >= candle.open ? `${longColor}55` : `${shortColor}55`
  }));
}

function macdHistogramData(candles: ChartCandle[], values: (number | null)[], longColor: string, shortColor: string): HistogramData[] {
  const points: HistogramData[] = [];
  candles.forEach((candle, index) => {
    const value = values[index];
    if (value === null) return;
    points.push({
      time: candle.time,
      value,
      color: value >= 0 ? `${longColor}88` : `${shortColor}88`
    });
  });
  return points;
}

function formatLegendValue(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 1000) return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (Math.abs(value) >= 1) return value.toFixed(value >= 100 ? 2 : 3);
  return value.toPrecision(3);
}

function ema(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = Array(values.length).fill(null);
  if (period <= 0 || values.length < period) return result;
  let previous = values.slice(0, period).reduce((sum, value) => sum + value, 0) / period;
  result[period - 1] = previous;
  const multiplier = 2 / (period + 1);
  for (let index = period; index < values.length; index += 1) {
    previous = (values[index] - previous) * multiplier + previous;
    result[index] = previous;
  }
  return result;
}

function macdCalc(closes: number[], fast = 12, slow = 26, signalPeriod = 9) {
  const fastEma = ema(closes, fast);
  const slowEma = ema(closes, slow);
  const line = closes.map((_, index) => (
    fastEma[index] !== null && slowEma[index] !== null
      ? (fastEma[index] as number) - (slowEma[index] as number)
      : null
  ));
  const compactLine = line.filter((value): value is number => value !== null);
  const compactSignal = ema(compactLine, signalPeriod);
  const signal: (number | null)[] = Array(closes.length).fill(null);
  let compactIndex = 0;
  for (let index = 0; index < line.length; index += 1) {
    if (line[index] === null) continue;
    signal[index] = compactSignal[compactIndex] ?? null;
    compactIndex += 1;
  }
  const histogram = line.map((value, index) => (
    value !== null && signal[index] !== null ? value - (signal[index] as number) : null
  ));
  return { line, signal, histogram };
}

function sma(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = Array(values.length).fill(null);
  if (period <= 0) return result;
  for (let index = period - 1; index < values.length; index += 1) {
    result[index] = values.slice(index - period + 1, index + 1).reduce((sum, value) => sum + value, 0) / period;
  }
  return result;
}

function atr(candles: ChartCandle[], period: number): (number | null)[] {
  const trueRanges = candles.map((candle, index) => {
    if (index === 0) return candle.high - candle.low;
    const previousClose = candles[index - 1].close;
    return Math.max(
      candle.high - candle.low,
      Math.abs(candle.high - previousClose),
      Math.abs(candle.low - previousClose)
    );
  });
  return ema(trueRanges, period);
}

function bollinger(candles: ChartCandle[], period = 20, stddev = 2) {
  const closes = candles.map((candle) => candle.close);
  const middle = sma(closes, period);
  const upper: (number | null)[] = Array(closes.length).fill(null);
  const lower: (number | null)[] = Array(closes.length).fill(null);
  for (let index = period - 1; index < closes.length; index += 1) {
    const mean = middle[index];
    if (mean === null) continue;
    const window = closes.slice(index - period + 1, index + 1);
    const variance = window.reduce((sum, value) => sum + (value - mean) ** 2, 0) / period;
    const deviation = Math.sqrt(variance);
    upper[index] = mean + stddev * deviation;
    lower[index] = mean - stddev * deviation;
  }
  return { middle, upper, lower };
}

function vwap(candles: ChartCandle[], volumes: number[], period = 48): (number | null)[] {
  const result: (number | null)[] = Array(candles.length).fill(null);
  for (let index = 0; index < candles.length; index += 1) {
    const start = Math.max(0, index - period + 1);
    if (index - start + 1 < period) continue;
    let pv = 0;
    let volume = 0;
    for (let cursor = start; cursor <= index; cursor += 1) {
      const typical = (candles[cursor].high + candles[cursor].low + candles[cursor].close) / 3;
      pv += typical * volumes[cursor];
      volume += volumes[cursor];
    }
    result[index] = volume > 0 ? pv / volume : null;
  }
  return result;
}

function supertrend(candles: ChartCandle[], period = 10, multiplier = 3): (number | null)[] {
  const atrValues = atr(candles, period);
  const upper: (number | null)[] = Array(candles.length).fill(null);
  const lower: (number | null)[] = Array(candles.length).fill(null);
  const trend: (number | null)[] = Array(candles.length).fill(null);
  const direction: ("up" | "down" | null)[] = Array(candles.length).fill(null);
  for (let index = 0; index < candles.length; index += 1) {
    const value = atrValues[index];
    if (value === null) continue;
    const hl2 = (candles[index].high + candles[index].low) / 2;
    const basicUpper = hl2 + multiplier * value;
    const basicLower = hl2 - multiplier * value;
    if (index === 0 || upper[index - 1] === null || lower[index - 1] === null) {
      upper[index] = basicUpper;
      lower[index] = basicLower;
      trend[index] = basicLower;
      direction[index] = "up";
      continue;
    }
    const previousUpper = upper[index - 1] as number;
    const previousLower = lower[index - 1] as number;
    const previousClose = candles[index - 1].close;
    upper[index] = basicUpper < previousUpper || previousClose > previousUpper ? basicUpper : previousUpper;
    lower[index] = basicLower > previousLower || previousClose < previousLower ? basicLower : previousLower;
    if (direction[index - 1] === "down") {
      direction[index] = candles[index].close > upper[index]! ? "up" : "down";
    } else {
      direction[index] = candles[index].close < lower[index]! ? "down" : "up";
    }
    trend[index] = direction[index] === "up" ? lower[index] : upper[index];
  }
  return trend;
}

function rsiCalc(closes: number[], period = 14): (number | null)[] {
  const result: (number | null)[] = Array(closes.length).fill(null);
  if (closes.length < period + 1) return result;
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff > 0) avgGain += diff; else avgLoss -= diff;
  }
  avgGain /= period;
  avgLoss /= period;
  result[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  for (let i = period + 1; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    const gain = diff > 0 ? diff : 0;
    const loss = diff < 0 ? -diff : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    result[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return result;
}

function fibonacciLevels(candles: ChartCandle[], lookback = 120) {
  const window = candles.slice(Math.max(0, candles.length - lookback));
  if (window.length === 0) return [];
  const high = Math.max(...window.map((candle) => candle.high));
  const low = Math.min(...window.map((candle) => candle.low));
  const highIndex = window.findIndex((candle) => candle.high === high);
  const lowIndex = window.findIndex((candle) => candle.low === low);
  const diff = high - low;
  if (diff <= 0) return [];
  const ratios = [0.236, 0.382, 0.5, 0.618, 0.786];
  return ratios.map((ratio) => ({
    title: `Fib ${(ratio * 100).toFixed(1)}%`,
    price: lowIndex < highIndex ? high - diff * ratio : low + diff * ratio
  }));
}

function latestPatternForWindow(candles: ChartCandle[]): CandlestickPattern | null {
  if (candles.length < 2) return null;
  const index = candles.length - 1;
  const current = candles[index];
  const previous = candles[index - 1];
  const body = Math.abs(current.close - current.open);
  const candleRange = current.high - current.low;
  const upperWick = current.high - Math.max(current.open, current.close);
  const lowerWick = Math.min(current.open, current.close) - current.low;
  const previousBearish = previous.close < previous.open;
  const previousBullish = previous.close > previous.open;
  const currentBullish = current.close > current.open;
  const currentBearish = current.close < current.open;

  if (previousBearish && currentBullish && current.open <= previous.close && current.close >= previous.open) {
    return { time: current.time, pattern: "bullish_engulfing", direction: "LONG", confidence: 72 };
  }
  if (previousBullish && currentBearish && current.open >= previous.close && current.close <= previous.open) {
    return { time: current.time, pattern: "bearish_engulfing", direction: "SHORT", confidence: 72 };
  }
  const hasBody = body > candleRange * 0.05;
  if (hasBody && lowerWick >= body * 2 && upperWick <= body * 0.4) {
    return { time: current.time, pattern: "hammer", direction: "LONG", confidence: 62 };
  }
  if (hasBody && upperWick >= body * 2 && lowerWick <= body * 0.4) {
    return { time: current.time, pattern: "shooting_star", direction: "SHORT", confidence: 62 };
  }
  if (candles.length >= 3) {
    const first = candles[index - 2];
    const firstBody = Math.abs(first.close - first.open);
    const middleBody = Math.abs(previous.close - previous.open);
    if (
      first.close < first.open &&
      middleBody < firstBody * 0.6 &&
      currentBullish &&
      current.close > (first.open + first.close) / 2
    ) {
      return { time: current.time, pattern: "morning_star", direction: "LONG", confidence: 78 };
    }
    if (
      first.close > first.open &&
      middleBody < firstBody * 0.6 &&
      currentBearish &&
      current.close < (first.open + first.close) / 2
    ) {
      return { time: current.time, pattern: "evening_star", direction: "SHORT", confidence: 78 };
    }
  }
  return null;
}

function candlestickPatterns(candles: ChartCandle[], lookback = 160) {
  const start = Math.max(0, candles.length - lookback);
  const patterns: CandlestickPattern[] = [];
  for (let index = Math.max(start + 1, 1); index < candles.length; index += 1) {
    const pattern = latestPatternForWindow(candles.slice(0, index + 1));
    if (pattern) patterns.push(pattern);
  }
  return patterns;
}

function candlestickPatternMarkers(candles: ChartCandle[], longColor: string, shortColor: string): SeriesMarker<Time>[] {
  return candlestickPatterns(candles).map((pattern) => {
    const isLong = pattern.direction === "LONG";
    return {
      time: pattern.time,
      position: isLong ? "belowBar" : "aboveBar",
      color: isLong ? longColor : shortColor,
      shape: "circle",
      text: `${pattern.pattern.replaceAll("_", " ")} ${pattern.confidence}`
    } satisfies SeriesMarker<Time>;
  });
}

function supportResistance(candles: ChartCandle[], lookback = 120, pivotWindow = 3, tolerancePct = 0.0025) {
  const start = Math.max(0, candles.length - lookback);
  const supports: number[] = [];
  const resistances: number[] = [];
  for (let index = Math.max(pivotWindow, start); index < candles.length - pivotWindow; index += 1) {
    const slice = candles.slice(index - pivotWindow, index + pivotWindow + 1);
    if (candles[index].low === Math.min(...slice.map((candle) => candle.low))) supports.push(candles[index].low);
    if (candles[index].high === Math.max(...slice.map((candle) => candle.high))) resistances.push(candles[index].high);
  }
  const window = candles.slice(start);
  if (window.length > 0) {
    supports.push(Math.min(...window.map((candle) => candle.low)));
    resistances.push(Math.max(...window.map((candle) => candle.high)));
  }
  return {
    supports: nearestLevels(clusterLevels(supports, tolerancePct), candles.at(-1)?.close ?? 0, "below", 3),
    resistances: nearestLevels(clusterLevels(resistances, tolerancePct), candles.at(-1)?.close ?? 0, "above", 3)
  };
}

function clusterLevels(levels: number[], tolerancePct: number) {
  const groups: number[][] = [];
  for (const level of [...levels].sort((a, b) => a - b)) {
    const latest = groups.at(-1);
    if (!latest) {
      groups.push([level]);
      continue;
    }
    const average = latest.reduce((sum, value) => sum + value, 0) / latest.length;
    if (Math.abs(level - average) <= Math.max(Math.abs(average) * tolerancePct, 1e-12)) latest.push(level);
    else groups.push([level]);
  }
  return groups.map((group) => group.reduce((sum, value) => sum + value, 0) / group.length);
}

function nearestLevels(levels: number[], price: number, side: "below" | "above", limit: number) {
  return levels
    .filter((level) => (side === "below" ? level <= price : level >= price))
    .sort((a, b) => Math.abs(a - price) - Math.abs(b - price))
    .slice(0, limit);
}

function signalMarkers(signals: Signal[], candles: ChartCandle[], market: Market | null, timeframe: string): SeriesMarker<Time>[] {
  if (!market || candles.length === 0) return [];
  return signals
    .filter((signal) => signal.market_id === market.id && signal.timeframe === timeframe)
    .map((signal) => {
      const createdAt = Math.floor(new Date(signal.created_at).getTime() / 1000);
      const candle = [...candles].reverse().find((item) => Number(item.time) <= createdAt) ?? candles.at(-1)!;
      const isLong = signal.direction.includes("LONG");
      return {
        time: candle.time,
        position: isLong ? "belowBar" : "aboveBar",
        color: isLong ? "#2fbf71" : "#f05a5a",
        shape: isLong ? "arrowUp" : "arrowDown",
        text: `${signal.direction.replace("STRONG_", "")} ${signal.score}`
      } satisfies SeriesMarker<Time>;
    })
    .sort((a, b) => Number(a.time) - Number(b.time));
}

function chartMarkers(
  signals: Signal[],
  candles: ChartCandle[],
  market: Market | null,
  timeframe: string,
  includeSignals: boolean,
  includeCandlestickPatterns: boolean,
  longColor: string,
  shortColor: string
): SeriesMarker<Time>[] {
  const patternMkrs = includeCandlestickPatterns ? candlestickPatternMarkers(candles, longColor, shortColor) : [];
  const sigMkrs = includeSignals ? signalMarkers(signals, candles, market, timeframe) : [];
  // lightweight-charts requires unique times; signal markers take priority over pattern markers
  const byTime = new Map<number, SeriesMarker<Time>>();
  for (const m of patternMkrs) byTime.set(Number(m.time), m);
  for (const m of sigMkrs) byTime.set(Number(m.time), m);
  return [...byTime.values()].sort((a, b) => Number(a.time) - Number(b.time));
}

export type ChartOverlays = {
  ema: boolean;
  bollinger: boolean;
  vwap: boolean;
  supertrend: boolean;
  levels: boolean;
  fibonacci: boolean;
  signals: boolean;
  rsi: boolean;
  macd: boolean;
  candlestick_patterns: boolean;
};

export function ChartPanel({
  candles,
  signals,
  market,
  timeframe,
  network,
  overlays,
  onLiveUpdate
}: {
  candles: Candle[];
  signals: Signal[];
  market: Market | null;
  timeframe: string;
  network: Health["network"];
  overlays: ChartOverlays;
  onLiveUpdate?: (update: LiveChartUpdate) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const liveCandleRef = useRef<ChartCandle | null>(null);
  const chartDataRef = useRef<ChartCandle[]>([]);
  const latestDataKeyRef = useRef("");
  const candleFeedSeenRef = useRef(false);
  const onLiveUpdateRef = useRef(onLiveUpdate);
  const colorsRef = useRef({ long: "#1edb88", short: "#ff5a4d" });
  const lastVisibleDataKeyRef = useRef("");
  const lastVisibleLogicalRangeRef = useRef<LogicalRange | null>(null);
  const candlesRef = useRef(candles);
  const marketRef = useRef(market);
  const signalsRef = useRef(signals);
  const rsiRef = useRef<HTMLDivElement | null>(null);
  const rsiChartRef = useRef<IChartApi | null>(null);
  const rsiSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdRef = useRef<HTMLDivElement | null>(null);
  const macdChartRef = useRef<IChartApi | null>(null);
  const macdLineSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdSignalSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdHistogramSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const [status, setStatus] = useState<"idle" | "connecting" | "live" | "closed" | "error">("idle");
  const [lastPrice, setLastPrice] = useState<number | null>(null);
  const coin = useMemo(() => marketCoin(market), [market]);
  const dataKey = `${coin}:${timeframe}`;
  const hasCandles = candles.length > 0;
  const legend = useMemo(() => {
    void lastPrice;
    const data = chartDataRef.current.length > 0 ? chartDataRef.current : normalizeCandles(candles);
    if (data.length === 0) return [];
    const closes = data.map((candle) => candle.close);
    const volumes = data.map((candle) => candle.volume);
    const items: { label: string; value: string; color: string; dashed?: boolean }[] = [];
    if (overlays.ema) {
      items.push(
        { label: "EMA 9", value: formatLegendValue(lastNumber(ema(closes, 9))), color: "var(--info)" },
        { label: "EMA 21", value: formatLegendValue(lastNumber(ema(closes, 21))), color: "var(--warn)" },
        { label: "EMA 200", value: formatLegendValue(lastNumber(ema(closes, 200))), color: "var(--fg-3)", dashed: true }
      );
    }
    if (overlays.bollinger) {
      const bands = bollinger(data);
      items.push({ label: "BB", value: formatLegendValue(lastNumber(bands.middle)), color: "var(--info)" });
    }
    if (overlays.vwap) {
      items.push({ label: "VWAP", value: formatLegendValue(lastNumber(vwap(data, volumes))), color: "var(--accent)" });
    }
    if (overlays.supertrend) {
      items.push({ label: "Supertrend", value: formatLegendValue(lastNumber(supertrend(data))), color: "var(--accent)", dashed: true });
    }
    items.push({ label: "Volume", value: formatLegendValue(data.at(-1)?.volume), color: "var(--fg-3)" });
    if (overlays.levels) items.push({ label: "S/R", value: "", color: "var(--fg-3)", dashed: true });
    if (overlays.fibonacci) items.push({ label: "Fibonacci", value: "", color: "var(--warn)", dashed: true });
    if (overlays.signals) items.push({ label: "Segnali", value: String(signals.filter((signal) => signal.timeframe === timeframe).length), color: "var(--long)" });
    if (overlays.rsi) items.push({ label: "RSI 14", value: formatLegendValue(lastNumber(rsiCalc(closes))), color: "var(--info)" });
    if (overlays.macd) {
      const macdValues = macdCalc(closes);
      items.push({ label: "MACD", value: formatLegendValue(lastNumber(macdValues.histogram)), color: "var(--accent)" });
    }
    if (overlays.candlestick_patterns) {
      items.push({ label: "Pattern", value: String(candlestickPatterns(data).length), color: "var(--warn)" });
    }
    return items;
  }, [candles, lastPrice, overlays, signals, timeframe]);

  useEffect(() => {
    onLiveUpdateRef.current = onLiveUpdate;
  }, [onLiveUpdate]);

  useEffect(() => {
    candlesRef.current = candles;
    marketRef.current = market;
    signalsRef.current = signals;
  }, [candles, market, signals]);

  function mergeLiveCandle(nextCandle: ChartCandle) {
    const current = chartDataRef.current;
    const nextTime = Number(nextCandle.time);
    const existingIndex = current.findIndex((candle) => Number(candle.time) === nextTime);
    const previousVolume = existingIndex >= 0 ? current[existingIndex].volume : 0;
    if (existingIndex >= 0) {
      current[existingIndex] = nextCandle;
    } else if (!current.at(-1) || Number(current.at(-1)!.time) < nextTime) {
      current.push(nextCandle);
    } else {
      current.push(nextCandle);
      current.sort((a, b) => Number(a.time) - Number(b.time));
    }
    return Math.max(0, nextCandle.volume - previousVolume);
  }

  function updateLiveCandle(nextCandle: ChartCandle, baseVolumeDelta: number) {
    const chart = chartRef.current;
    const series = seriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    const visibleLogicalRange = chart?.timeScale().getVisibleLogicalRange() ?? null;
    const shouldPreserveRange = (chart?.timeScale().scrollPosition() ?? 0) > 1;
    const { long, short } = colorsRef.current;
    const nextTime = Number(nextCandle.time);
    const latestTime = Number(chartDataRef.current.at(-1)?.time ?? nextTime);
    const updatesHistoricalCandle = nextTime < latestTime;

    liveCandleRef.current = nextCandle;
    if (updatesHistoricalCandle) {
      series?.setData(chartDataRef.current);
      volumeSeries?.setData(volumeData(chartDataRef.current, long, short));
    } else {
      series?.update(nextCandle);
      volumeSeries?.update({
        time: nextCandle.time,
        value: nextCandle.volume,
        color: nextCandle.close >= nextCandle.open ? `${long}55` : `${short}55`
      });
    }
    setLastPrice(nextCandle.close);

    if (shouldPreserveRange && visibleLogicalRange) {
      requestAnimationFrame(() => {
        chartRef.current?.timeScale().setVisibleLogicalRange(visibleLogicalRange);
      });
    }

    if (Number.isFinite(nextCandle.close)) {
      onLiveUpdateRef.current?.({
        price: nextCandle.close,
        baseVolumeDelta,
        quoteVolumeDelta: nextCandle.close * baseVolumeDelta,
        candleTime: Number(nextCandle.time),
        candle: toApiCandle(nextCandle)
      });
    }
  }

  useEffect(() => {
    if (!ref.current || !hasCandles) return;
    const initialData = latestDataKeyRef.current === dataKey && chartDataRef.current.length > 0
      ? chartDataRef.current
      : normalizeCandles(candlesRef.current);
    if (initialData.length === 0) return;
    const volumes = initialData.map((candle) => candle.volume);
    const closes = initialData.map((candle) => candle.close);
    chartDataRef.current = initialData;
    liveCandleRef.current = initialData.at(-1) ?? null;

    const cv = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const bgColor   = cv("--bg-1")    || "#0d0d10";
    const gridColor = cv("--border")  || "#1c1c22";
    const textClr   = cv("--fg-3")    || "#6a6a72";
    const scaleClr  = cv("--border")  || "#1c1c22";
    const longClr   = cv("--long")    || "#1edb88";
    const shortClr  = cv("--short")   || "#ff5a4d";
    const infoClr   = cv("--info")    || "#5b8def";
    const warnClr   = cv("--warn")    || "#f0b341";
    const accentClr = cv("--accent")  || "#1edb88";
    colorsRef.current = { long: longClr, short: shortClr };

    const chart = createChart(ref.current, {
      height: 440,
      layout: {
        background: { type: ColorType.Solid, color: bgColor },
        textColor: textClr
      },
      grid: {
        vertLines: { color: `${gridColor}66` },
        horzLines: { color: `${gridColor}66` }
      },
      rightPriceScale: { borderColor: scaleClr, scaleMargins: { top: 0.08, bottom: 0.24 } },
      timeScale: { borderColor: scaleClr, rightOffset: 6, barSpacing: 7 }
    });
    chartRef.current = chart;
    const series = chart.addCandlestickSeries({
      upColor: longClr,
      downColor: shortClr,
      borderVisible: false,
      wickUpColor: longClr,
      wickDownColor: shortClr,
      priceLineVisible: false
    });
    seriesRef.current = series;
    series.setData(initialData);
    const volumeSeries = chart.addHistogramSeries({
      color: `${textClr}44`,
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      priceLineVisible: false,
      lastValueVisible: false
    });
    volumeSeriesRef.current = volumeSeries;
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 }
    });
    volumeSeries.setData(volumeData(initialData, longClr, shortClr));
    series.setMarkers(chartMarkers(
      signalsRef.current,
      initialData,
      marketRef.current,
      timeframe,
      overlays.signals,
      overlays.candlestick_patterns,
      longClr,
      shortClr
    ));

    if (overlays.ema) {
      const ema9 = chart.addLineSeries({ color: infoClr, lineWidth: 1, title: "", priceLineVisible: false, lastValueVisible: false });
      ema9.setData(lineData(initialData, ema(closes, 9)));
      const ema21 = chart.addLineSeries({ color: warnClr, lineWidth: 1, title: "", priceLineVisible: false, lastValueVisible: false });
      ema21.setData(lineData(initialData, ema(closes, 21)));
      const ema200 = chart.addLineSeries({
        color: textClr,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        title: "",
        priceLineVisible: false,
        lastValueVisible: false
      });
      ema200.setData(lineData(initialData, ema(closes, 200)));
    }
    if (overlays.bollinger) {
      const bands = bollinger(initialData);
      const upper = chart.addLineSeries({
        color: infoClr,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        title: "",
        priceLineVisible: false,
        lastValueVisible: false
      });
      upper.setData(lineData(initialData, bands.upper));
      const middle = chart.addLineSeries({
        color: `${infoClr}99`,
        lineWidth: 1,
        title: "",
        priceLineVisible: false,
        lastValueVisible: false
      });
      middle.setData(lineData(initialData, bands.middle));
      const lower = chart.addLineSeries({
        color: infoClr,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        title: "",
        priceLineVisible: false,
        lastValueVisible: false
      });
      lower.setData(lineData(initialData, bands.lower));
    }
    if (overlays.vwap) {
      const vwapSeries = chart.addLineSeries({
        color: accentClr,
        lineWidth: 2,
        title: "",
        priceLineVisible: false,
        lastValueVisible: false
      });
      vwapSeries.setData(lineData(initialData, vwap(initialData, volumes)));
    }
    if (overlays.supertrend) {
      const supertrendSeries = chart.addLineSeries({
        color: accentClr,
        lineWidth: 2,
        lineStyle: LineStyle.LargeDashed,
        title: "",
        priceLineVisible: false,
        lastValueVisible: false
      });
      supertrendSeries.setData(lineData(initialData, supertrend(initialData)));
    }
    if (overlays.fibonacci) {
      for (const level of fibonacciLevels(initialData)) {
        series.createPriceLine({
          price: level.price,
          color: warnClr,
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          axisLabelVisible: false,
          title: ""
        });
      }
    }
    if (overlays.levels) {
      const levels = supportResistance(initialData);
      for (const price of levels.supports) {
        series.createPriceLine({
          price,
          color: longClr,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: false,
          title: ""
        });
      }
      for (const price of levels.resistances) {
        series.createPriceLine({
          price,
          color: shortClr,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: false,
          title: ""
        });
      }
    }
    const lastIndex = initialData.length - 1;
    const preservedRange = lastVisibleDataKeyRef.current === dataKey ? lastVisibleLogicalRangeRef.current : null;
    chart.timeScale().setVisibleLogicalRange(preservedRange ?? {
      from: Math.max(0, lastIndex - 130),
      to: lastIndex + 8
    });
    const handleResize = () => chart.applyOptions({ width: ref.current?.clientWidth ?? 800 });
    handleResize();
    const resizeObserver = typeof ResizeObserver !== "undefined" && ref.current
      ? new ResizeObserver(handleResize)
      : null;
    if (resizeObserver && ref.current) resizeObserver.observe(ref.current);
    window.addEventListener("resize", handleResize);
    return () => {
      lastVisibleDataKeyRef.current = dataKey;
      lastVisibleLogicalRangeRef.current = chart.timeScale().getVisibleLogicalRange();
      window.removeEventListener("resize", handleResize);
      resizeObserver?.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      volumeSeriesRef.current = null;
    };
  }, [
    coin,
    dataKey,
    hasCandles,
    overlays.bollinger,
    overlays.candlestick_patterns,
    overlays.ema,
    overlays.fibonacci,
    overlays.levels,
    overlays.signals,
    overlays.supertrend,
    overlays.vwap,
    timeframe
  ]);

  useEffect(() => {
    if (!hasCandles) {
      chartDataRef.current = [];
      liveCandleRef.current = null;
      latestDataKeyRef.current = dataKey;
      return;
    }

    const previousKey = latestDataKeyRef.current;
    const shouldResetRange = previousKey !== dataKey || chartDataRef.current.length === 0;
    const normalizedData = normalizeCandles(candles);
    const nextData = previousKey === dataKey
      ? mergeRestCandlesWithLive(normalizedData, chartDataRef.current)
      : normalizedData;
    chartDataRef.current = nextData;
    liveCandleRef.current = nextData.at(-1) ?? null;
    latestDataKeyRef.current = dataKey;
    if (previousKey !== dataKey) candleFeedSeenRef.current = false;
    setLastPrice(nextData.at(-1)?.close ?? null);

    const { long, short } = colorsRef.current;
    seriesRef.current?.setData(nextData);
    volumeSeriesRef.current?.setData(volumeData(nextData, long, short));
    if (rsiSeriesRef.current && nextData.length > 0) {
      rsiSeriesRef.current.setData(lineData(nextData, rsiCalc(nextData.map((c) => c.close))));
    }
    if (macdLineSeriesRef.current && macdSignalSeriesRef.current && macdHistogramSeriesRef.current && nextData.length > 0) {
      const macdValues = macdCalc(nextData.map((c) => c.close));
      macdLineSeriesRef.current.setData(lineData(nextData, macdValues.line));
      macdSignalSeriesRef.current.setData(lineData(nextData, macdValues.signal));
      macdHistogramSeriesRef.current.setData(macdHistogramData(nextData, macdValues.histogram, long, short));
    }

    if (shouldResetRange && nextData.length > 0) {
      const lastIndex = nextData.length - 1;
      chartRef.current?.timeScale().setVisibleLogicalRange({
        from: Math.max(0, lastIndex - 130),
        to: lastIndex + 8
      });
    }
  }, [candles, dataKey, hasCandles]);

  useEffect(() => {
    if (!seriesRef.current) return;
    const { long, short } = colorsRef.current;
    seriesRef.current.setMarkers(chartMarkers(
      signals,
      chartDataRef.current,
      market,
      timeframe,
      overlays.signals,
      overlays.candlestick_patterns,
      long,
      short
    ));
  }, [market, overlays.candlestick_patterns, overlays.signals, signals, timeframe]);

  useEffect(() => {
    const container = rsiRef.current;
    if (!container || !overlays.rsi) {
      if (rsiChartRef.current) {
        rsiChartRef.current.remove();
        rsiChartRef.current = null;
        rsiSeriesRef.current = null;
      }
      return;
    }
    const data = chartDataRef.current.length > 0 ? chartDataRef.current : normalizeCandles(candlesRef.current);
    if (data.length === 0) return;

    const cv = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const bgColor  = cv("--bg-1")   || "#0d0d10";
    const gridClr  = cv("--border") || "#1c1c22";
    const textClr  = cv("--fg-3")   || "#6a6a72";
    const infoClr  = cv("--info")   || "#5b8def";
    const longClr  = cv("--long")   || "#1edb88";
    const shortClr = cv("--short")  || "#ff5a4d";

    const chart = createChart(container, {
      height: 100,
      layout: { background: { type: ColorType.Solid, color: bgColor }, textColor: textClr },
      grid: { vertLines: { color: `${gridClr}33` }, horzLines: { color: `${gridClr}33` } },
      rightPriceScale: { borderColor: gridClr, scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: { borderColor: gridClr, visible: false },
      crosshair: { horzLine: { visible: true, labelVisible: false } },
      handleScroll: false,
      handleScale: false,
    });
    rsiChartRef.current = chart;

    const series = chart.addLineSeries({ color: infoClr, lineWidth: 1, priceLineVisible: false, lastValueVisible: true });
    rsiSeriesRef.current = series;
    series.setData(lineData(data, rsiCalc(data.map((c) => c.close))));
    series.createPriceLine({ price: 70, color: `${shortClr}99`, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "" });
    series.createPriceLine({ price: 50, color: `${textClr}55`, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
    series.createPriceLine({ price: 30, color: `${longClr}99`,  lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "" });

    const syncRange = (range: LogicalRange | null) => { if (range) chart.timeScale().setVisibleLogicalRange(range); };
    chartRef.current?.timeScale().subscribeVisibleLogicalRangeChange(syncRange);
    const current = chartRef.current?.timeScale().getVisibleLogicalRange();
    if (current) chart.timeScale().setVisibleLogicalRange(current);

    const handleResize = () => chart.applyOptions({ width: container.clientWidth });
    handleResize();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(handleResize) : null;
    ro?.observe(container);
    window.addEventListener("resize", handleResize);

    return () => {
      chartRef.current?.timeScale().unsubscribeVisibleLogicalRangeChange(syncRange);
      ro?.disconnect();
      window.removeEventListener("resize", handleResize);
      chart.remove();
      rsiChartRef.current = null;
      rsiSeriesRef.current = null;
    };
  }, [overlays.rsi, coin, dataKey, hasCandles]);

  useEffect(() => {
    const container = macdRef.current;
    if (!container || !overlays.macd) {
      if (macdChartRef.current) {
        macdChartRef.current.remove();
        macdChartRef.current = null;
        macdLineSeriesRef.current = null;
        macdSignalSeriesRef.current = null;
        macdHistogramSeriesRef.current = null;
      }
      return;
    }
    const data = chartDataRef.current.length > 0 ? chartDataRef.current : normalizeCandles(candlesRef.current);
    if (data.length === 0) return;

    const cv = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const bgColor   = cv("--bg-1")   || "#0d0d10";
    const gridClr   = cv("--border") || "#1c1c22";
    const textClr   = cv("--fg-3")   || "#6a6a72";
    const infoClr   = cv("--info")   || "#5b8def";
    const warnClr   = cv("--warn")   || "#f0b341";
    const longClr   = cv("--long")   || "#1edb88";
    const shortClr  = cv("--short")  || "#ff5a4d";
    const macdData = macdCalc(data.map((c) => c.close));

    const chart = createChart(container, {
      height: 120,
      layout: { background: { type: ColorType.Solid, color: bgColor }, textColor: textClr },
      grid: { vertLines: { color: `${gridClr}33` }, horzLines: { color: `${gridClr}33` } },
      rightPriceScale: { borderColor: gridClr, scaleMargins: { top: 0.16, bottom: 0.16 } },
      timeScale: { borderColor: gridClr, visible: false },
      crosshair: { horzLine: { visible: true, labelVisible: false } },
      handleScroll: false,
      handleScale: false,
    });
    macdChartRef.current = chart;

    const histogramSeries = chart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
    macdHistogramSeriesRef.current = histogramSeries;
    histogramSeries.setData(macdHistogramData(data, macdData.histogram, longClr, shortClr));

    const lineSeries = chart.addLineSeries({ color: infoClr, lineWidth: 1, priceLineVisible: false, lastValueVisible: true });
    macdLineSeriesRef.current = lineSeries;
    lineSeries.setData(lineData(data, macdData.line));

    const signalSeries = chart.addLineSeries({ color: warnClr, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    macdSignalSeriesRef.current = signalSeries;
    signalSeries.setData(lineData(data, macdData.signal));

    lineSeries.createPriceLine({ price: 0, color: `${textClr}55`, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });

    const syncRange = (range: LogicalRange | null) => { if (range) chart.timeScale().setVisibleLogicalRange(range); };
    chartRef.current?.timeScale().subscribeVisibleLogicalRangeChange(syncRange);
    const current = chartRef.current?.timeScale().getVisibleLogicalRange();
    if (current) chart.timeScale().setVisibleLogicalRange(current);

    const handleResize = () => chart.applyOptions({ width: container.clientWidth });
    handleResize();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(handleResize) : null;
    ro?.observe(container);
    window.addEventListener("resize", handleResize);

    return () => {
      chartRef.current?.timeScale().unsubscribeVisibleLogicalRangeChange(syncRange);
      ro?.disconnect();
      window.removeEventListener("resize", handleResize);
      chart.remove();
      macdChartRef.current = null;
      macdLineSeriesRef.current = null;
      macdSignalSeriesRef.current = null;
      macdHistogramSeriesRef.current = null;
    };
  }, [overlays.macd, coin, dataKey, hasCandles]);

  useEffect(() => {
    if (!coin || !hasCandles) {
      setStatus("idle");
      return;
    }

    setStatus("connecting");
    const socket = new WebSocket(wsUrl(network));
    const candleSubscription = { type: "candle", coin, interval: timeframe };
    let active = true;

    socket.addEventListener("open", () => {
      if (!active) return;
      socket.send(JSON.stringify({ method: "subscribe", subscription: candleSubscription }));
      setStatus("live");
    });

    socket.addEventListener("message", (event) => {
      if (!active) return;
      let message: WsMessage;
      try {
        message = JSON.parse(event.data as string) as WsMessage;
      } catch {
        return;
      }

      if (message.channel === "candle") {
        const payload = Array.isArray(message.data) ? message.data : [message.data];
        for (const item of payload) {
          if (item.s !== coin || item.i !== timeframe) continue;
          const nextCandle = toChartCandleFromWs(item);
          if (!nextCandle) continue;
          candleFeedSeenRef.current = true;
          const baseVolumeDelta = mergeLiveCandle(nextCandle);
          updateLiveCandle(nextCandle, baseVolumeDelta);
        }
        return;
      }
    });

    socket.addEventListener("close", () => {
      if (active) setStatus("closed");
    });
    socket.addEventListener("error", () => {
      if (active) setStatus("error");
    });

    return () => {
      active = false;
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ method: "unsubscribe", subscription: candleSubscription }));
      }
      socket.close();
    };
  }, [coin, hasCandles, network, timeframe]);

  if (candles.length === 0) {
    return (
      <div className="w-full min-h-95 flex flex-col items-center justify-center gap-3 p-8 text-center">
        <strong className="text-lg font-semibold text-text">Nessuna candela disponibile</strong>
        <span className="text-sm text-muted max-w-xs">
          Snapshot Hyperliquid non disponibile per questo mercato/timeframe.
        </span>
      </div>
    );
  }

  return (
    <div className="relative">
      <div style={{ position: "absolute", zIndex: 20, top: 10, left: 12, display: "flex", alignItems: "center", gap: 6, pointerEvents: "none" }}>
        <span className={"dot " + (status === "live" ? "green" : status === "connecting" ? "amber" : "red")}
          style={{ animation: status === "live" ? "pulse 2s infinite" : undefined }} />
        <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>
          {status === "live" ? "Live" : status}
        </span>
        {lastPrice !== null && (
          <strong className="mono fg" style={{ fontSize: "var(--fz-xs)" }}>
            {lastPrice >= 1000
              ? lastPrice.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
              : lastPrice.toFixed(4)}
          </strong>
        )}
      </div>
      <div style={{
        position: "absolute",
        zIndex: 20,
        top: 10,
        right: 12,
        display: "flex",
        flexWrap: "wrap",
        justifyContent: "flex-end",
        gap: "6px 10px",
        maxWidth: "68%",
        pointerEvents: "none"
      }}>
        {legend.map((item) => (
          <span key={item.label} className="mono muted" style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: "var(--fz-xs)", background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: 4, padding: "2px 6px" }}>
            <span style={{ width: 10, borderTop: `2px ${item.dashed ? "dashed" : "solid"} ${item.color}`, display: "inline-block" }} />
            <span>{item.label}</span>
            {item.value && <strong className="fg" style={{ fontWeight: 500 }}>{item.value}</strong>}
          </span>
        ))}
      </div>
      <div className="w-full min-h-95" ref={ref} />
      {overlays.macd && (
        <div style={{ borderTop: "1px solid var(--border)" }}>
          <div style={{ padding: "2px 10px", fontSize: 9, fontFamily: "monospace", color: "var(--fg-3)", letterSpacing: "0.06em" }}>MACD 12 26 9</div>
          <div ref={macdRef} className="w-full" />
        </div>
      )}
      {overlays.rsi && (
        <div style={{ borderTop: "1px solid var(--border)" }}>
          <div style={{ padding: "2px 10px", fontSize: 9, fontFamily: "monospace", color: "var(--fg-3)", letterSpacing: "0.06em" }}>RSI 14</div>
          <div ref={rsiRef} className="w-full" />
        </div>
      )}
    </div>
  );
}
