import {
  Activity,
  Bell,
  Beaker,
  History,
  LayoutDashboard,
  LineChart,
  RefreshCcw,
  Settings,
  SlidersHorizontal,
  TrendingUp,
  Users,
  Wallet,
  type LucideIcon,
} from "lucide-react";

export type Route =
  | "dash"
  | "signals"
  | "coin"
  | "account"
  | "strategies"
  | "strategy-lab"
  | "calibration"
  | "performance"
  | "history"
  | "funnel"
  | "alerts"
  | "settings";

export type RouteGroup = "workspace" | "analytics" | "config";

export interface RouteMeta {
  id: Route;
  label: string;
  shortLabel?: string;
  group: RouteGroup;
  icon: LucideIcon;
  description: string;
}

export const ROUTES: RouteMeta[] = [
  { id: "dash",        label: "Dashboard",  group: "workspace", icon: LayoutDashboard,   description: "Panoramica operativa" },
  { id: "signals",     label: "Segnali",    group: "workspace", icon: Activity,          description: "Feed segnali live" },
  { id: "coin",        label: "Dettaglio",    group: "workspace", icon: LineChart,         description: "Chart e indicatori per mercato" },
  { id: "account",     label: "Account",    group: "workspace", icon: Wallet,            description: "Posizioni e ordini Hyperliquid" },

  { id: "strategy-lab",label: "Analisi",    group: "analytics", icon: Beaker,            description: "Strategy lab: ranking e combinazioni" },
  { id: "calibration", label: "Calibration",group: "analytics", icon: RefreshCcw,        description: "Auto-tuner e stability" },
  { id: "performance", label: "Performance",group: "analytics", icon: TrendingUp,        description: "Equity, drawdown e distribuzione P&L" },
  { id: "history",     label: "Storico",    group: "analytics", icon: History,           description: "Posizioni chiuse e exit orders" },
  { id: "funnel",      label: "Funnel",     group: "analytics", icon: Users,             description: "Bot Telegram: iscritti, conversione, source" },

  { id: "strategies",  label: "Strategie",  group: "config",    icon: SlidersHorizontal, description: "Pesi e parametri per timeframe" },
  { id: "alerts",      label: "Alert",      group: "config",    icon: Bell,              description: "Feed alert, push, telegram" },
  { id: "settings",    label: "Settings",   group: "config",    icon: Settings,          description: "Operation, risk, services" },
];

export const ROUTE_GROUPS: { id: RouteGroup; label: string }[] = [
  { id: "workspace", label: "Workspace" },
  { id: "analytics", label: "Analytics" },
  { id: "config",    label: "Config" },
];

export const ROUTE_MAP: Record<Route, RouteMeta> = Object.fromEntries(
  ROUTES.map((r) => [r.id, r]),
) as Record<Route, RouteMeta>;
