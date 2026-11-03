import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built files go inside the Python package so `taylortrader dashboard` can serve them.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/taylortrader/web/static", emptyOutDir: true },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": { target: "http://127.0.0.1:8787", changeOrigin: true } },
  },
});
