"use client";

import { useEffect } from "react";

import type { Route } from "@/components/shell/routes";

type HotkeyHandler = (event: KeyboardEvent) => void;

interface HotkeyOptions {
  enabled?: boolean;
}

/**
 * Hook minimale per hotkeys "Vim-like" a due tasti.
 *
 * Pattern: l'utente preme un tasto leader (es. "g"), poi entro `windowMs` un tasto target.
 * Lo skip in input/textarea/contenteditable e' automatico.
 */
export function useChordHotkey(
  leader: string,
  bindings: Record<string, HotkeyHandler>,
  options: HotkeyOptions = {},
) {
  useEffect(() => {
    if (options.enabled === false) return;
    const windowMs = 800;
    let pending = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    function onKey(event: KeyboardEvent) {
      if (isEditableTarget(event.target)) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      if (!pending) {
        if (event.key.toLowerCase() === leader.toLowerCase()) {
          event.preventDefault();
          pending = true;
          timer = setTimeout(() => {
            pending = false;
          }, windowMs);
        }
        return;
      }

      pending = false;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      const handler = bindings[event.key.toLowerCase()];
      if (handler) {
        event.preventDefault();
        handler(event);
      }
    }

    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (timer) clearTimeout(timer);
    };
  }, [leader, bindings, options.enabled]);
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (target.isContentEditable) return true;
  return false;
}

/**
 * Mapping standard delle scorciatoie "g <tasto>" verso le route.
 */
export const NAV_HOTKEY_TO_ROUTE: Record<string, Route> = {
  d: "dash",
  s: "signals",
  m: "coin",
  a: "account",
  l: "strategy-lab",
  c: "calibration",
  h: "history",
  t: "strategies",
  n: "alerts", // n di "notifications"
  ",": "settings",
};

export function buildNavBindings(setRoute: (r: Route) => void): Record<string, HotkeyHandler> {
  const out: Record<string, HotkeyHandler> = {};
  for (const [key, route] of Object.entries(NAV_HOTKEY_TO_ROUTE)) {
    out[key] = () => setRoute(route);
  }
  return out;
}
