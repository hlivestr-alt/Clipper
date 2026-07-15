import React, { Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { appQueryClient } from "./queryClient";

const App = lazy(() => import("./App").then((module) => ({ default: module.App })));

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={appQueryClient}>
      <Suspense fallback={<div aria-label="Loading Clipper" />}>
        <App />
      </Suspense>
    </QueryClientProvider>
  </React.StrictMode>
);
