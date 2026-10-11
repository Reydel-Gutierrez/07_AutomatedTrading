import { useState } from "react";
import { api } from "../api";
import { ErrorLine, useAction } from "../components/ui";

export default function Login({ hasAdmin, onLogin }: { hasAdmin: boolean; onLogin: () => void }) {
  const [username, setUsername] = useState("reydel");
  const [password, setPassword] = useState("");
  const login = useAction(() => api.post("/api/login", { username, password }), onLogin);
  return (
    <div className="grid min-h-screen place-items-center p-4">
      <form
        className="card w-full max-w-sm"
        onSubmit={(e) => {
          e.preventDefault();
          login.run();
        }}
      >
        <h1 className="font-display text-2xl text-slate-50">
          Agentic <span className="text-gold-400">Capital</span>
        </h1>
        <p className="mb-6 mt-1 text-sm text-slate-400">Investment Headquarters</p>
        {!hasAdmin ? (
          <p className="text-sm text-amber-300">No admin account yet. On the Pi run <code className="text-slate-200">scripts/firm_admin.py set-password</code>.</p>
        ) : (
          <>
            <input className="input mb-3" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Username" />
            <input className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password" autoFocus />
            <button className="btn-primary mt-4 w-full" disabled={login.pending || !password}>Sign in</button>
            <ErrorLine error={login.error} />
          </>
        )}
      </form>
    </div>
  );
}
