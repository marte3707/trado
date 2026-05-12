"use client";

import React from "react";

export type Accent = "green" | "blue" | "amber" | "violet";
export type Density = "compact" | "regular" | "comfy";

export interface TweakState {
  accent: Accent;
  density: Density;
  scoreThreshold: number;
  showFearGreed: boolean;
  showNews: boolean;
  showActivity: boolean;
}

export const TWEAK_DEFAULTS: TweakState = {
  accent: "green",
  density: "regular",
  scoreThreshold: 60,
  showFearGreed: true,
  showNews: true,
  showActivity: true,
};

const ACCENT_COLORS: Record<Accent, string> = {
  green:  "#1edb88",
  blue:   "#3b82f6",
  amber:  "#f59e0b",
  violet: "#8b5cf6",
};

interface TweaksPanelProps {
  open: boolean;
  onClose: () => void;
  tweaks: TweakState;
  onChange: <K extends keyof TweakState>(key: K, value: TweakState[K]) => void;
}

export const TweaksPanel = ({ open, onClose, tweaks, onChange }: TweaksPanelProps) => {
  if (!open) return null;

  return (
    <div className="twk-panel">
      <div className="twk-hd">
        <b>Tweaks</b>
        <button className="twk-x" aria-label="Chiudi tweaks" onClick={onClose}>✕</button>
      </div>
      <div className="twk-body">

        {/* Accent */}
        <div className="twk-sect">Colore accento</div>
        <div style={{ display: "flex", gap: 8 }}>
          {(Object.keys(ACCENT_COLORS) as Accent[]).map((a) => (
            <button
              key={a}
              title={a}
              onClick={() => onChange("accent", a)}
              style={{
                width: 24,
                height: 24,
                borderRadius: "50%",
                background: ACCENT_COLORS[a],
                border: tweaks.accent === a ? "2px solid var(--fg)" : "2px solid transparent",
                cursor: "pointer",
                flexShrink: 0,
              }}
            />
          ))}
        </div>

        {/* Density */}
        <div className="twk-sect">Densità</div>
        <div className="twk-seg" style={{ display: "flex", background: "var(--bg-2)", borderRadius: 8, padding: 2 }}>
          {(["compact", "regular", "comfy"] as Density[]).map((d) => (
            <button
              key={d}
              onClick={() => onChange("density", d)}
              style={{
                flex: 1,
                padding: "4px 0",
                border: "none",
                background: tweaks.density === d ? "var(--bg-3)" : "transparent",
                color: tweaks.density === d ? "var(--fg)" : "var(--fg-3)",
                borderRadius: 6,
                cursor: "pointer",
                fontSize: 11,
                fontFamily: "var(--font-mono)",
              }}
            >
              {d}
            </button>
          ))}
        </div>

        {/* Score threshold */}
        <div className="twk-sect">Soglia score</div>
        <div className="twk-row">
          <div className="twk-lbl">
            <span>Score minimo segnali</span>
            <span className="twk-val">{tweaks.scoreThreshold}</span>
          </div>
          <input
            type="range"
            className="twk-slider"
            min={0}
            max={100}
            step={5}
            value={tweaks.scoreThreshold}
            onChange={(e) => onChange("scoreThreshold", Number(e.target.value))}
          />
        </div>

        {/* Sections */}
        <div className="twk-sect">Sezioni dashboard</div>

        {([
          ["showFearGreed", "Fear & Greed"],
          ["showNews",      "News crypto"],
          ["showActivity",  "Log attività"],
        ] as [keyof TweakState, string][]).map(([key, label]) => (
          <div key={key} className="twk-row" style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ fontSize: 12 }}>{label}</span>
            <button
              type="button"
              className="twk-toggle"
              data-on={tweaks[key] ? "1" : "0"}
              role="switch"
              aria-checked={!!tweaks[key]}
              onClick={() => onChange(key, !tweaks[key] as TweakState[typeof key])}
            >
              <i />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
};
