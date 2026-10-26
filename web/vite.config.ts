import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: фронт на :5173, запросы /api проксируются на бэкенд :8000.
// Prod: `npm run build` → web/dist, его раздаёт сам FastAPI (один контейнер).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000" },
  },
});
