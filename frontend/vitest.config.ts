import { defineConfig } from "vitest/config";

export default defineConfig({
  // JSX runtime automatico (come Next): niente `import React` necessario nei test.
  esbuild: { jsx: "automatic" },
  resolve: {
    // Allinea l'alias "@/..." usato dai moduli applicativi (vedi tsconfig paths).
    alias: { "@": new URL(".", import.meta.url).pathname },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules", ".next"],
  },
});
