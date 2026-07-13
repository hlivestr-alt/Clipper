import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const controlToken = process.env.CLIPPER_CONTROL_TOKEN?.trim();

export default defineConfig({
  plugins: [react()],
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
