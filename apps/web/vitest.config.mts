import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Mirror tsconfig's "@/*" -> "./*" so `@/lib/health` resolves in tests.
    // A string alias only matches "@" exactly or "@/...", so packages like
    // "@testing-library/react" are unaffected.
    alias: { "@": fileURLToPath(new URL(".", import.meta.url)) },
  },
  test: {
    environment: "jsdom", // default for component tests; pure-function tests opt into "node"
  },
});