"use client";

import { cn } from "@/lib/utils";

export type DotStatus = "ok" | "warn" | "danger" | "info" | "muted";

const STYLES: Record<DotStatus, string> = {
  ok: "bg-success shadow-[0_0_0_3px_rgba(62,207,142,0.18)]",
  warn: "bg-warn shadow-[0_0_0_3px_rgba(224,179,65,0.18)]",
  danger: "bg-danger shadow-[0_0_0_3px_rgba(242,109,109,0.18)]",
  info: "bg-info shadow-[0_0_0_3px_rgba(122,162,247,0.18)]",
  muted: "bg-fg-4",
};

export function StatusDot({
  status,
  size = 8,
  className,
}: {
  status: DotStatus;
  size?: number;
  className?: string;
}) {
  return (
    <span
      className={cn("inline-block shrink-0 rounded-full", STYLES[status], className)}
      style={{ width: size, height: size }}
    />
  );
}
