"use client";

import { useEffect } from "react";
import { registerServiceWorker } from "@/lib/push";

export function SwRegistrar() {
  useEffect(() => {
    registerServiceWorker();
  }, []);
  return null;
}
