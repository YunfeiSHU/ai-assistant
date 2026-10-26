import { StrictMode } from "react";

import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { createRoot } from "react-dom/client";

import { router } from "@/app/router";
import { queryClient } from "@/app/query-client";
import { useAuthStore } from "@/features/auth/store";

import "./styles/app.css";
import "../prototype/styles.css";

void useAuthStore.getState().bootstrap();

const container = document.getElementById("root");
if (!container) throw new Error("缺少 #root");

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
