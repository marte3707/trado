import {
  Activity,
  Bell,
  LayoutDashboard,
  LineChart,
  Radar,
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
  | "calibration"
  | "performance"
  | "leaderboard"
  | "funnel"
  | "alerts"
  | "settings";

export type RouteGroup = "operations" | "research" | "system" | "advanced";

export interface RouteMeta {
  id: Route;
  label: string;
  shortLabel?: string;
  group: RouteGroup;
  icon: LucideIcon;
  description: string;
}

export const ROUTES: RouteMeta[] = [
  { id: "dash",        label: "Panoramica",   group: "operations", icon: LayoutDashboard,   description: "Stato operativo e posizioni" },
  { id: "signals",     label: "Segnali",      group: "operations", icon: Activity,          description: "Candidati e decisioni recenti" },
  { id: "account",     label: "Portafoglio",  group: "operations", icon: Wallet,            description: "Posizioni, ordini e storico" },
  { id: "calibration", label: "Ricerca",      group: "research",   icon: RefreshCcw,        description: "Diagnostica e validazione forward" },
  { id: "alerts",      label: "Eventi",       group: "system",     icon: Bell,              description: "Errori, warning e notifiche" },
  { id: "settings",    label: "Impostazioni", group: "system",     icon: Settings,          description: "Trading, rischio e servizi" },

  { id: "coin",        label: "Mercati",      group: "advanced",   icon: LineChart,         description: "Chart e indicatori per mercato" },
  { id: "performance", label: "Performance",  group: "advanced",   icon: TrendingUp,        description: "Equity, drawdown e distribuzione P&L" },
  { id: "strategies",  label: "Strategie",    group: "advanced",   icon: SlidersHorizontal, description: "Pesi, parametri e strategy lab" },
  { id: "leaderboard", label: "Alpha",        group: "advanced",   icon: Radar,             description: "Watchlist leaderboard Hyperliquid" },
  { id: "funnel",      label: "Bot funnel",   group: "advanced",   icon: Users,             description: "Iscritti e conversione Telegram" },
];

export const ROUTE_GROUPS: { id: RouteGroup; label: string }[] = [
  { id: "operations", label: "Operatività" },
  { id: "research", label: "Evidenza" },
  { id: "system", label: "Sistema" },
  { id: "advanced", label: "Strumenti avanzati" },
];

export const ROUTE_MAP: Record<Route, RouteMeta> = Object.fromEntries(
  ROUTES.map((r) => [r.id, r]),
) as Record<Route, RouteMeta>;
