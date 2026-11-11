"use client";

import { useEffect, useState, type FormEvent } from "react";

import { getToken, setToken, UNAUTHORIZED_EVENT } from "@/lib/api";

type Need = "none" | "missing" | "rejected";

/**
 * A server started with ARCHAEO_API_TOKEN refuses requests without it. This bar appears
 * when that happens, takes the token and keeps it in this browser's local storage.
 */
export function TokenPrompt() {
  const [need, setNeed] = useState<Need>("none");
  const [value, setValue] = useState("");

  useEffect(() => {
    const ask = () => setNeed(getToken() ? "rejected" : "missing");
    window.addEventListener(UNAUTHORIZED_EVENT, ask);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, ask);
  }, []);

  if (need === "none") return null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const token = value.trim();
    if (!token) return;
    setToken(token);
    window.location.reload();
  };

  return (
    <div className="border-b border-accent/40 bg-accent/10" role="alert">
      <form
        onSubmit={submit}
        className="mx-auto flex w-full max-w-[1600px] flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2 text-sm"
      >
        <label htmlFor="api-token">
          {need === "rejected" ? "The server rejected the stored API token." : "This server requires an API token."}
        </label>
        <input
          id="api-token"
          type="password"
          className="input max-w-xs font-mono"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          required
        />
        <button type="submit" className="btn btn-primary">
          Use this token
        </button>
        <span className="text-xs text-dim">It is kept in this browser only.</span>
      </form>
    </div>
  );
}
