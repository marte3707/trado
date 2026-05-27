"use client";

import * as React from "react";
import type { LucideIcon } from "lucide-react";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
  variant?: "card" | "inline";
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
  variant = "card",
}: EmptyStateProps) {
  const content = (
    <div className={cn("flex flex-col items-center justify-center gap-3 text-center", className)}>
      {Icon && (
        <div className="grid h-12 w-12 place-items-center rounded-full border border-border bg-bg-2 text-fg-3">
          <Icon className="h-5 w-5" />
        </div>
      )}
      <div className="space-y-1">
        <div className="text-sm font-medium text-fg">{title}</div>
        {description && (
          <div className="max-w-md text-xs text-fg-3">{description}</div>
        )}
      </div>
      {action && <div className="mt-1">{action}</div>}
    </div>
  );

  if (variant === "inline") {
    return <div className="py-10">{content}</div>;
  }
  return <Card className="grid place-items-center p-10">{content}</Card>;
}
