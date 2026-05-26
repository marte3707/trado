"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

interface ProgressProps extends React.HTMLAttributes<HTMLDivElement> {
  value?: number | null;
  max?: number;
  indicatorClassName?: string;
}

const Progress = React.forwardRef<HTMLDivElement, ProgressProps>(
  ({ className, value, max = 100, indicatorClassName, ...props }, ref) => {
    const clamped = Math.max(0, Math.min(max, value ?? 0));
    const pct = (clamped / max) * 100;
    return (
      <div
        ref={ref}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={clamped}
        className={cn("relative h-1.5 w-full overflow-hidden rounded-full bg-bg-3", className)}
        {...props}
      >
        <div
          className={cn("h-full bg-primary transition-[width] duration-300 ease-out", indicatorClassName)}
          style={{ width: `${pct}%` }}
        />
      </div>
    );
  },
);
Progress.displayName = "Progress";

export { Progress };
