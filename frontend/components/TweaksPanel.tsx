"use client";

/**
 * Legacy TweaksPanel — non piu' montato nella shell.
 *
 * Lo `scoreThreshold` ora vive in <ThresholdMenu /> nella Topbar.
 * I flag dashboard (fearGreed/news/activity) in `lib/hooks/useDashboardPrefs`.
 * Le opzioni accent/density sono state rimosse: tema dark professionale unico.
 *
 * Lasciato come no-op per backward compat con eventuali import non aggiornati.
 */

export interface TweakState {
  scoreThreshold: number;
  showFearGreed: boolean;
  showNews: boolean;
  showActivity: boolean;
}

export const TWEAK_DEFAULTS: TweakState = {
  scoreThreshold: 60,
  showFearGreed: true,
  showNews: true,
  showActivity: true,
};

export function TweaksPanel(): null {
  return null;
}
