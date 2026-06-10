import { describe, expect, it } from "vitest";

import { sideMultiplier, unrealizedPnl } from "@/lib/pnl";

describe("sideMultiplier", () => {
  it("ritorna -1 per short (case-insensitive)", () => {
    expect(sideMultiplier("short")).toBe(-1);
    expect(sideMultiplier("SHORT")).toBe(-1);
  });

  it("ritorna 1 per long", () => {
    expect(sideMultiplier("long")).toBe(1);
  });
});

describe("unrealizedPnl", () => {
  it("calcola un long in profitto", () => {
    expect(unrealizedPnl({ entryPrice: 100, size: 2, markPrice: 110, side: "long" })).toBe(20);
  });

  it("uno short guadagna quando il prezzo scende", () => {
    expect(unrealizedPnl({ entryPrice: 100, size: 2, markPrice: 90, side: "short" })).toBe(20);
  });

  it("usa il valore assoluto della size", () => {
    expect(unrealizedPnl({ entryPrice: 100, size: -2, markPrice: 110, side: "long" })).toBe(20);
  });

  it("accetta stringhe numeriche (valori dal backend)", () => {
    expect(unrealizedPnl({ entryPrice: "100", size: "2", markPrice: "105", side: "long" })).toBe(10);
  });

  it("ritorna null quando un input non è numerico", () => {
    expect(unrealizedPnl({ entryPrice: 100, size: 2, markPrice: null, side: "long" })).toBeNull();
    expect(unrealizedPnl({ entryPrice: "x", size: 2, markPrice: 105, side: "long" })).toBeNull();
  });
});
