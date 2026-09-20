import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";

const frontendRoot = fileURLToPath(new URL("./frontend", import.meta.url));

export default defineConfig({
  root: frontendRoot,
  base: "/ui/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/ready": "http://127.0.0.1:8000",
    },
  },
});
