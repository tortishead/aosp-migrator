import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { api } from "../api";
import { authStatusKey } from "../queryClient";

export function LoginPage() {
  const queryClient = useQueryClient();
  const [token, setToken] = useState("");
  const login = useMutation({
    mutationFn: api.login,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: authStatusKey }),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    login.mutate(token);
  }

  return (
    <main className="login">
      <h1>AOSP Migrator</h1>
      <form onSubmit={submit} className="form">
        <label>
          Access token
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoFocus />
        </label>
        <p className="hint">The server runs in LAN mode. The token is in config.toml under [server] lan_token.</p>
        {login.isError && <p className="error">{login.error.message}</p>}
        <button type="submit" disabled={!token || login.isPending}>
          Log in
        </button>
      </form>
    </main>
  );
}
