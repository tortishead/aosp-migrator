import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet, useMatch, useNavigate } from "react-router";

import { api } from "./api";
import { LoginPage } from "./pages/LoginPage";
import { authStatusKey, workspacesKey } from "./queryClient";

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
        <WorkspaceSwitcher />
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

function WorkspaceSwitcher() {
  const navigate = useNavigate();
  const workspaces = useQuery({ queryKey: workspacesKey, queryFn: api.workspaces });
  const match = useMatch("/workspaces/:workspaceId/*");
  const current = match?.params.workspaceId;

  if (!workspaces.data?.length) return null;
  const selected = workspaces.data.some((w) => String(w.id) === current) ? current : "";

  return (
    <select
      className="workspace-switcher"
      aria-label="Workspace"
      value={selected}
      onChange={(e) => e.target.value && navigate(`/workspaces/${e.target.value}`)}
    >
      <option value="" disabled>
        Select workspace…
      </option>
      {workspaces.data.map((w) => (
        <option key={w.id} value={w.id}>
          {w.name}
        </option>
      ))}
    </select>
  );
}
