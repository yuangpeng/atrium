import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.ATRIUM_BACKEND ?? "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../atrium/static", emptyOutDir: true, chunkSizeWarningLimit: 6000 },
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});
