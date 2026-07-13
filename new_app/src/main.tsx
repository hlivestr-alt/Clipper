import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { appQueryClient } from "./queryClient";
import "./styles.css";

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={appQueryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>
);
