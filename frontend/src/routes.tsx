import type { RouteObject } from "react-router";

import { Layout } from "./Layout";
import { BacklogPage } from "./pages/BacklogPage";
import { ItemPage } from "./pages/ItemPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { SettingsPage } from "./pages/SettingsPage";
import { WorkspacesPage } from "./pages/WorkspacesPage";

export const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { path: "/", element: <WorkspacesPage /> },
      { path: "/settings", element: <SettingsPage /> },
      { path: "/workspaces/:workspaceId", element: <BacklogPage /> },
      { path: "/workspaces/:workspaceId/items/:itemKey", element: <ItemPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
