"use client";

import { useEffect, useState } from "react";

export interface DashboardPrefs {
  showFearGreed: boolean;
  showNews: boolean;
  showActivity: boolean;
  sidebarCollapsed: boolean;
}

const DEFAULTS: DashboardPrefs = {
  showFearGreed: true,
  showNews: true,
  showActivity: true,
  sidebarCollapsed: false,
};

const STORAGE_KEY = "trado.dashboard-prefs";

function loadPrefs(): DashboardPrefs {
  if (typeof window === "undefined") return DEFAULTS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    const parsed = JSON.parse(raw) as Partial<DashboardPrefs>;
    return { ...DEFAULTS, ...parsed };
  } catch {
    return DEFAULTS;
  }
}

export function useDashboardPrefs() {
  const [prefs, setPrefs] = useState<DashboardPrefs>(DEFAULTS);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setPrefs(loadPrefs());
    setHydrated(true);
  }, []);

  function update<K extends keyof DashboardPrefs>(key: K, value: DashboardPrefs[K]) {
    setPrefs((prev) => {
      const next = { ...prev, [key]: value };
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* ignore quota errors */
      }
      return next;
    });
  }

  return { prefs, update, hydrated } as const;
}
