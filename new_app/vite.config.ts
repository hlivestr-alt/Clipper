import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const controlToken = process.env.CLIPPER_CONTROL_TOKEN?.trim();

export default defineConfig({
  plugins: [react()],
  css: {
    devSourcemap: true
  },
  build: {
    cssCodeSplit: true,
    manifest: true,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
          icons: ["lucide-react"]
        }
      }
    }
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        configure(proxy) {
          if (!controlToken) {
            return;
          }
          proxy.on("proxyReq", (proxyRequest) => {
            proxyRequest.setHeader("Authorization", `Bearer ${controlToken}`);
          });
        }
      }
    }
  }
});
