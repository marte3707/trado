"use client";

import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export function DashboardSkeleton() {
  return (
    <div className="space-y-7 px-7 py-7">
      {/* Header */}
      <div className="flex items-end justify-between gap-4">
        <div className="space-y-2">
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-3 w-48" />
        </div>
        <div className="flex items-center gap-3">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="h-9 w-64" />
        </div>
      </div>

      {/* Row 1 — KPI cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <KpiSkeleton key={i} />
        ))}
      </div>

      {/* Row 2 — Equity (8) + Funnel (4) */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2 p-5">
          <Skeleton className="h-3 w-32" />
          <Skeleton className="mt-2 h-7 w-44" />
          <Skeleton className="mt-4 h-44 w-full" />
        </Card>
        <Card className="p-5">
          <Skeleton className="h-3 w-24" />
          <div className="mt-4 space-y-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Skeleton className="h-3 w-20" />
                  <Skeleton className="h-3 w-8" />
                </div>
                <Skeleton className="h-1.5 w-full" />
              </div>
            ))}
          </div>
        </Card>
      </div>

      {/* Row 3 — Top signals (6) + Open positions (6) */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {Array.from({ length: 2 }).map((_, listIdx) => (
          <Card key={listIdx} className="overflow-hidden">
            <div className="flex items-center justify-between border-b border-border px-6 py-4">
              <Skeleton className="h-3 w-24" />
              <Skeleton className="h-3 w-8" />
            </div>
            <ul className="divide-y divide-border">
              {Array.from({ length: 4 }).map((_, i) => (
                <li key={i} className="flex items-center gap-3 px-6 py-3">
                  <Skeleton className="h-5 w-14 rounded-md" />
                  <Skeleton className="h-4 w-16" />
                  <Skeleton className="h-4 w-10" />
                  <div className="ml-auto flex items-center gap-2">
                    <Skeleton className="h-5 w-16 rounded-md" />
                    <Skeleton className="h-4 w-4" />
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        ))}
      </div>

      {/* Row 4 — Market intelligence strip */}
      <Card className="grid grid-cols-1 divide-y divide-border md:grid-cols-4 md:divide-x md:divide-y-0">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="space-y-2 p-5">
            <div className="flex items-center justify-between">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-3 w-8" />
            </div>
            <Skeleton className="h-6 w-24" />
            <Skeleton className="h-1 w-full" />
          </div>
        ))}
      </Card>
    </div>
  );
}

function KpiSkeleton() {
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between">
        <Skeleton className="h-3 w-16" />
        <Skeleton className="h-4 w-12 rounded-md" />
      </div>
      <Skeleton className="mt-3 h-8 w-32" />
      <Skeleton className="mt-1.5 h-3 w-24" />
    </Card>
  );
}
