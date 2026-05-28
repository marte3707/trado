"use client";

import { ChevronLeft, ChevronRight, Hexagon } from "lucide-react";

import { useHealth } from "@/lib/hooks/queries";
import { ROUTE_GROUPS, ROUTES, type Route } from "@/components/shell/routes";
import { StatusDot } from "@/components/shell/StatusDot";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export interface SidebarProps {
  route: Route;
  setRoute: (r: Route) => void;
  counts: { signals: number; alerts: number };
  collapsed: boolean;
  onToggleCollapsed: () => void;
}

export function Sidebar({ route, setRoute, counts, collapsed, onToggleCollapsed }: SidebarProps) {
  const { data: health } = useHealth();
  const wsLive = health?.status === "ok";

  return (
    <aside
      className={cn(
        "grid-area-side flex h-full flex-col border-r border-border bg-bg",
        "transition-[width] duration-200 ease-out",
      )}
      style={{ gridArea: "side" }}
    >
      {/* Brand */}
      <div
        className={cn(
          "flex h-14 shrink-0 items-center gap-2 border-b border-border",
          collapsed ? "justify-center px-0" : "px-3",
        )}
      >
        {!collapsed && (
          <>
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-bg-1">
              <Hexagon className="h-4 w-4 text-success" strokeWidth={2.2} />
            </div>
            <div className="flex min-w-0 flex-col leading-tight">
              <span className="text-sm font-semibold tracking-tight text-fg">Trado</span>
              <span className="font-mono text-[10px] uppercase tracking-wide text-fg-3">
                {health?.network ?? "—"}
              </span>
            </div>
            <button
              className="ml-auto inline-flex h-6 w-6 items-center justify-center rounded-md border border-border text-fg-3 transition-colors hover:bg-bg-2 hover:text-fg"
              onClick={onToggleCollapsed}
              aria-label="Comprimi menu"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
          </>
        )}
        {collapsed && (
          <button
            className="inline-flex h-6 w-6 items-center justify-center rounded-md border border-border text-fg-3 transition-colors hover:bg-bg-2 hover:text-fg"
            onClick={onToggleCollapsed}
            aria-label="Espandi menu"
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {/* Nav groups */}
      <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-2 py-3">
        {ROUTE_GROUPS.map((group) => {
          const items = ROUTES.filter((r) => r.group === group.id);
          return (
            <div key={group.id} className="flex flex-col gap-0.5">
              {!collapsed && (
                <div className="px-2 pb-1 pt-3 text-[10px] font-medium uppercase tracking-wider text-fg-4">
                  {group.label}
                </div>
              )}
              {collapsed && group.id !== "workspace" && (
                <div className="my-1 h-px bg-border" />
              )}
              {items.map((item) => {
                const Icon = item.icon;
                const active = route === item.id;
                const badge =
                  item.id === "signals" && counts.signals > 0
                    ? counts.signals
                    : item.id === "alerts" && counts.alerts > 0
                      ? counts.alerts
                      : null;

                const button = (
                  <button
                    key={item.id}
                    onClick={() => setRoute(item.id)}
                    aria-current={active}
                    className={cn(
                      "group relative flex items-center gap-2.5 rounded-md text-sm transition-colors",
                      collapsed ? "h-9 w-9 justify-center" : "h-8 px-2",
                      active
                        ? "bg-bg-2 text-fg"
                        : "text-fg-2 hover:bg-bg-2 hover:text-fg",
                    )}
                  >
                    {active && (
                      <span className="absolute -left-1 top-1.5 bottom-1.5 w-[2px] rounded-full bg-success" />
                    )}
                    <Icon
                      className={cn(
                        "h-4 w-4 shrink-0",
                        active ? "text-success" : "text-fg-3 group-hover:text-fg-2",
                      )}
                    />
                    {!collapsed && (
                      <>
                        <span className="truncate">{item.label}</span>
                        {badge != null && (
                          <span
                            className={cn(
                              "ml-auto rounded-md border px-1.5 py-0.5 font-mono text-[10px]",
                              active
                                ? "border-success/30 bg-success/10 text-success"
                                : "border-border bg-bg-2 text-fg-3",
                            )}
                          >
                            {badge}
                          </span>
                        )}
                      </>
                    )}
                    {collapsed && badge != null && (
                      <span className="absolute -right-0.5 -top-0.5 inline-flex h-3.5 min-w-3.5 items-center justify-center rounded-full border border-bg bg-success px-1 font-mono text-[9px] text-primary-foreground">
                        {badge > 9 ? "9+" : badge}
                      </span>
                    )}
                  </button>
                );

                return collapsed ? (
                  <Tooltip key={item.id}>
                    <TooltipTrigger asChild>{button}</TooltipTrigger>
                    <TooltipContent side="right">
                      <div className="font-medium">{item.label}</div>
                      <div className="text-fg-3">{item.description}</div>
                    </TooltipContent>
                  </Tooltip>
                ) : (
                  button
                );
              })}
            </div>
          );
        })}
      </nav>

      {/* Footer */}
      <div
        className={cn(
          "flex shrink-0 items-center border-t border-border px-3 py-2.5 text-xs text-fg-3",
          collapsed && "justify-center px-0",
        )}
      >
        <StatusDot status={wsLive ? "ok" : "danger"} />
        {!collapsed && (
          <span className="ml-2 truncate">
            Hyperliquid · {wsLive ? "WS live" : "offline"}
          </span>
        )}
      </div>
    </aside>
  );
}
