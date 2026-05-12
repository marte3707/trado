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

type WsTrade = {
  coin: string;
  px: string;
  sz: string;
  time: number;
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
  | { channel: "trades"; data: WsTrade[] }
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

const INTERVAL_SECONDS: Record<string, number> = {
  "1m": 60,
  "3m": 180,
  "5m": 300,
  "15m": 900,
  "30m": 1800,
  "1h": 3600,
  "2h": 7200,
  "4h": 14400,
  "8h": 28800,
  "12h": 43200,
  "1d": 86400
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

function applyTrade(candle: ChartCandle | null, trade: WsTrade, timeframe: string): ChartCandle {
  const interval = INTERVAL_SECONDS[timeframe] ?? 300;
  const tradeSeconds = Math.floor(trade.time / 1000);
  const bucket = Math.floor(tradeSeconds / interval) * interval;
  const price = Number(trade.px);
  const size = Number(trade.sz);
  const volumeDelta = Number.isFinite(size) ? size : 0;

  if (!candle || Number(candle.time) < bucket) {
    return { time: bucket as Time, open: price, high: price, low: price, close: price, volume: volumeDelta };
  }

  return {
    ...candle,
    high: Math.max(candle.high, price),
    low: Math.min(candle.low, price),
    close: price,
    volume: candle.volume + volumeDelta
  };
}

export type ChartOverlays = {
  ema: boolean;
  bollinger: boolean;
  vwap: boolean;
  supertrend: boolean;
  levels: boolean;
  signals: boolean;
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
    if (overlays.levels) items.push({ label: "S/R + Fib", value: "", color: "var(--warn)", dashed: true });
    if (overlays.signals) items.push({ label: "Segnali", value: String(signals.filter((signal) => signal.timeframe === timeframe).length), color: "var(--long)" });
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

    liveCandleRef.current = nextCandle;
    series?.update(nextCandle);
    volumeSeries?.update({
      time: nextCandle.time,
      value: nextCandle.volume,
      color: nextCandle.close >= nextCandle.open ? `${long}55` : `${short}55`
    });
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
    if (overlays.signals) {
      series.setMarkers(signalMarkers(signalsRef.current, initialData, marketRef.current, timeframe));
    }

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
    if (overlays.levels) {
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
    overlays.ema,
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
    const nextData = normalizeCandles(candles);
    chartDataRef.current = nextData;
    liveCandleRef.current = nextData.at(-1) ?? null;
    latestDataKeyRef.current = dataKey;
    if (previousKey !== dataKey) candleFeedSeenRef.current = false;
    setLastPrice(nextData.at(-1)?.close ?? null);

    const { long, short } = colorsRef.current;
    seriesRef.current?.setData(nextData);
    volumeSeriesRef.current?.setData(volumeData(nextData, long, short));

    if (shouldResetRange && nextData.length > 0) {
      const lastIndex = nextData.length - 1;
      chartRef.current?.timeScale().setVisibleLogicalRange({
        from: Math.max(0, lastIndex - 130),
        to: lastIndex + 8
      });
    }
  }, [candles, dataKey, hasCandles]);

  useEffect(() => {
    if (!seriesRef.current || !overlays.signals) return;
    seriesRef.current.setMarkers(signalMarkers(signals, chartDataRef.current, market, timeframe));
  }, [market, overlays.signals, signals, timeframe]);

  useEffect(() => {
    if (!coin || !hasCandles) {
      setStatus("idle");
      return;
    }

    setStatus("connecting");
    const socket = new WebSocket(wsUrl(network));
    const candleSubscription = { type: "candle", coin, interval: timeframe };
    const tradesSubscription = { type: "trades", coin };
    let active = true;

    socket.addEventListener("open", () => {
      if (!active) return;
      socket.send(JSON.stringify({ method: "subscribe", subscription: candleSubscription }));
      socket.send(JSON.stringify({ method: "subscribe", subscription: tradesSubscription }));
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

      if (message.channel !== "trades" || !Array.isArray(message.data)) return;
      for (const trade of message.data) {
        const price = Number(trade.px);
        const baseVolumeDelta = Number(trade.sz);
        if (Number.isFinite(price)) setLastPrice(price);

        if (!candleFeedSeenRef.current) {
          const nextCandle = applyTrade(liveCandleRef.current, trade, timeframe);
          mergeLiveCandle(nextCandle);
          updateLiveCandle(nextCandle, Number.isFinite(baseVolumeDelta) ? baseVolumeDelta : 0);
        }
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
        socket.send(JSON.stringify({ method: "unsubscribe", subscription: tradesSubscription }));
      }
      socket.close();
    };
  }, [coin, hasCandles, network, timeframe]);

  if (candles.length === 0) {
    return (
      <div className="w-full min-h-95 flex flex-col items-center justify-center gap-3 p-8 text-center">
        <strong className="text-lg font-semibold text-text">Nessuna candela disponibile</strong>
        <span className="text-sm text-muted max-w-xs">
          Avvia il worker e attendi almeno un ciclo di sincronizzazione.
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
    </div>
  );
}
