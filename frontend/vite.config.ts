import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// Assets are referenced relative to <base href>, which Nginx rewrites when the app runs under a sub-path.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: { outDir: "dist", assetsDir: "assets", sourcemap: false, chunkSizeWarningLimit: 900 },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8740", ws: true },
      "/mcp": "http://127.0.0.1:8740",
      "/oauth/": "http://127.0.0.1:8740",
      "/.well-known": "http://127.0.0.1:8740",
    },
  },
});
