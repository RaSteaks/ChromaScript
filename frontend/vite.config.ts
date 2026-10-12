import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// Production is served by the same local daemon; this proxy is development-only.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": process.env.CHROMASCRIPT_URL || "http://127.0.0.1:8765" },
  },
});
