import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router";

import { api } from "./api";
import { LoginPage } from "./pages/LoginPage";
import { authStatusKey } from "./queryClient";

export function Layout() {
  const queryClient = useQueryClient();
  const auth = useQuery({ queryKey: authStatusKey, queryFn: api.authStatus });
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => queryClient.clear(),
  });

  if (auth.isPending) return null;
  if (auth.isError) return <p className="error">Cannot reach the server: {auth.error.message}</p>;
  if (auth.data.required && !auth.data.authenticated) return <LoginPage />;

  return (
    <div className="app">
      <header className="app-header">
        <span className="app-title">AOSP Migrator</span>
        <nav>
          <NavLink to="/" end>
            Workspaces
          </NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        {auth.data.required && (
          <button className="app-logout" onClick={() => logout.mutate()}>
            Log out
          </button>
        )}
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
