import { lazy, Suspense } from "react";
import type { ReactNode } from "react";

import {
  Outlet,
  createRootRoute,
  createRoute,
  createRouter,
  redirect,
} from "@tanstack/react-router";

import { AppLayout } from "@/app/AppLayout";
import { PageFallback } from "@/app/PageFallback";
import { LoginPage } from "@/features/auth/LoginPage";
import { ChatPage } from "@/features/chat/ChatPage";

const ConversationPage = lazy(() =>
  import("@/features/chat/ConversationPage").then((module) => ({
    default: module.ConversationPage,
  })),
);
const KnowledgePage = lazy(() =>
  import("@/features/knowledge/KnowledgePage").then((module) => ({
    default: module.KnowledgePage,
  })),
);
const TasksPage = lazy(() =>
  import("@/features/tasks/TasksPage").then((module) => ({
    default: module.TasksPage,
  })),
);
const MemoryPage = lazy(() =>
  import("@/features/memory/MemoryPage").then((module) => ({
    default: module.MemoryPage,
  })),
);
const QuotaPage = lazy(() =>
  import("@/features/quota/QuotaPage").then((module) => ({
    default: module.QuotaPage,
  })),
);
const SettingsPage = lazy(() =>
  import("@/features/settings/SettingsPage").then((module) => ({
    default: module.SettingsPage,
  })),
);

function suspend(node: ReactNode) {
  return <Suspense fallback={<PageFallback />}>{node}</Suspense>;
}

const rootRoute = createRootRoute({
  component: Outlet,
  notFoundComponent: () => (
    <div className="page">
      <div className="page-inner">
        <div className="panel">页面不存在。</div>
      </div>
    </div>
  ),
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/chat" });
  },
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
});

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  component: AppLayout,
});

const chatRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "chat",
  component: ChatPage,
});

const conversationRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "chat/$conversationId",
  component: () => suspend(<ConversationPage />),
});

const knowledgeRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "knowledge",
  component: () => suspend(<KnowledgePage />),
});

const tasksRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "tasks",
  component: () => suspend(<TasksPage />),
});

const memoryRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "memory",
  component: () => suspend(<MemoryPage />),
});

const quotaRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "quota",
  component: () => suspend(<QuotaPage />),
});

const settingsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "settings",
  component: () => suspend(<SettingsPage />),
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  appRoute.addChildren([
    chatRoute,
    conversationRoute,
    knowledgeRoute,
    tasksRoute,
    memoryRoute,
    quotaRoute,
    settingsRoute,
  ]),
]);

export const router = createRouter({
  routeTree,
  defaultPreload: "intent",
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
