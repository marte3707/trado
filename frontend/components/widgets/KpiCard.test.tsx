import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KpiCard } from "@/components/widgets/KpiCard";

// `getByText` lancia se l'elemento non esiste: di per sé è già un'asserzione.
describe("KpiCard", () => {
  it("rende label e value", () => {
    render(<KpiCard label="Open PnL" value="+$123.45" />);
    expect(screen.getByText("Open PnL")).toBeTruthy();
    expect(screen.getByText("+$123.45")).toBeTruthy();
  });

  it("mostra il badge delta quando fornito", () => {
    render(<KpiCard label="Equity" value="$10,000" delta={{ value: "+2.5%", tone: "up" }} />);
    expect(screen.getByText("+2.5%")).toBeTruthy();
  });

  it("mostra l'hint opzionale", () => {
    render(<KpiCard label="Win rate" value="55%" hint="ultimi 30 giorni" />);
    expect(screen.getByText("ultimi 30 giorni")).toBeTruthy();
  });
});
