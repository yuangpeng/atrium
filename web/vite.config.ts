import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.ATRIUM_BACKEND ?? "http://127.0.0.1:8787";
// keep the Host header (the shorthand `"/api": backend` rewrites it): the backend refuses requests
// whose Origin isn't the host they are addressed to (atrium/auth.py)
const http = { target: backend, changeOrigin: false };

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../atrium/static", emptyOutDir: true, chunkSizeWarningLimit: 6000 },
  server: {
    port: 5173,
    proxy: {
      "/api": http,
      "/login": http,
      "/logout": http,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});
