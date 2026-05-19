"use client";

import React from "react";
import { Icon } from "@/components/atoms";
import { Health } from "@/lib/api";

export type Route = "dash" | "signals" | "coin" | "account" | "strategies" | "calibration" | "history" | "alerts" | "settings";
export type TradingMode = "signal" | "paper" | "live";

interface SidebarProps {
  route: Route;
  setRoute: (r: Route) => void;
  counts: { signals: number; alerts: number };
  wsLive: boolean;
  health: Health;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}

export const Sidebar = ({ route, setRoute, counts, wsLive, health, collapsed, onToggleCollapsed }: SidebarProps) => {
  const topItems: { id: Route; label: string; icon: string; badge?: number }[] = [
    { id: "dash",    label: "Dashboard", icon: "dash"    },
    { id: "signals", label: "Segnali",   icon: "signal", badge: counts.signals > 0 ? counts.signals : undefined },
    { id: "account", label: "Account",   icon: "wallet"  },
  ];
  const bottomItems: { id: Route; label: string; icon: string; badge?: number }[] = [
    { id: "strategies", label: "Strategie", icon: "sliders"  },
    { id: "calibration", label: "Calibration", icon: "refresh" },
    { id: "history",    label: "Storico",   icon: "history"  },
    { id: "alerts",     label: "Alert",     icon: "bell", badge: counts.alerts > 0 ? counts.alerts : undefined },
    { id: "settings",   label: "Settings",  icon: "settings" },
  ];


  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-logo-wrap">
          <img src="/csp.png" alt="Trado" className="brand-logo-img" />
        </div>
        <div className="brand-row">
          <div className="brand-name">Trado</div>
          <div className="brand-net">{health.network}</div>
          <button
            className="sidebar-toggle"
            onClick={onToggleCollapsed}
            title={collapsed ? "Espandi menu" : "Comprimi menu"}
            aria-label={collapsed ? "Espandi menu" : "Comprimi menu"}
          >
            <Icon name={collapsed ? "chevron" : "chevron_left"} size={13} />
          </button>
        </div>
      </div>

      <nav className="nav">
        <div className="nav-group">Workspace</div>
        {topItems.map((it) => (
          <div
            key={it.id}
            className={"nav-item " + (route === it.id ? "active" : "")}
            onClick={() => setRoute(it.id)}
          >
            <span className="nav-icon"><Icon name={it.icon} /></span>
            <span className="nav-label">{it.label}</span>
            {it.badge != null && <span className="nav-badge">{it.badge}</span>}
          </div>
        ))}

        <div className="nav-group">Config</div>
        {bottomItems.map((it) => (
          <div
            key={it.id}
            className={"nav-item " + (route === it.id ? "active" : "")}
            onClick={() => setRoute(it.id)}
          >
            <span className="nav-icon"><Icon name={it.icon} /></span>
            <span className="nav-label">{it.label}</span>
            {it.badge != null && <span className="nav-badge">{it.badge}</span>}
          </div>
        ))}
      </nav>

      <div className="sidebar-foot">
        <span className={"dot" + (wsLive ? " green" : " dot-warn")} />
        <span className="sidebar-foot-label">Hyperliquid · {wsLive ? "WS live" : "offline"}</span>
      </div>
    </aside>
  );
};

const ROUTE_LABELS: Record<Route, string> = {
  dash:       "Dashboard",
  signals:    "Segnali live",
  coin:       "Mercati",
  account:    "Account monitor",
  strategies: "Strategie",
  calibration: "Calibration",
  history:    "Storico segnali",
  alerts:     "Alert",
  settings:   "Settings",
};

interface TopbarProps {
  route: Route;
  mode: TradingMode;
  kill: boolean;
  setKill: (k: boolean) => void;
  onToggleTweaks: () => void;
}

export const Topbar = ({ route, mode, kill, setKill, onToggleTweaks }: TopbarProps) => (
  <div className="topbar">
    <div className="crumbs">
      <span>Trado</span>
      <span className="sep">/</span>
      <span className="here">{ROUTE_LABELS[route]}</span>
    </div>
    <div className="topbar-spacer" />

    <div className="mode-selector" aria-label="Trading mode" title="Modalità definita in .env">
      <span
        className={"mode-opt" + (mode === "signal" ? " active" : "")}
        aria-current={mode === "signal" ? "true" : undefined}
      >
        <span className="mode-dot" style={{ background: "var(--info)" }} />
        SIGNAL_ONLY
      </span>
      <span
        className={"mode-opt" + (mode === "paper" ? " active" : "")}
        aria-current={mode === "paper" ? "true" : undefined}
      >
        <span className="mode-dot" style={{ background: "var(--warn)" }} />
        PAPER_TRADING
      </span>
      <span
        className={"mode-opt live" + (mode === "live" ? " active" : "")}
        aria-current={mode === "live" ? "true" : undefined}
      >
        <span className="mode-dot" style={{ background: "var(--short)" }} />
        AUTO_TRADE
      </span>
    </div>

    <button
      className={"kill-switch" + (kill ? " armed" : "")}
      onClick={() => setKill(!kill)}
      title="Kill switch — disabilita istantaneamente l'autotrading"
    >
      <Icon name="power" size={14} />
      <span className="mono">{kill ? "HALTED" : "KILL"}</span>
    </button>

    <button className="icon-btn" onClick={onToggleTweaks} title="Tweaks">
      <Icon name="sliders" size={14} />
    </button>
  </div>
);
