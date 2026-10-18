"use client";

import { useEffect, useState, type ReactNode } from "react";
import { ApiError, adminToken, api } from "@/lib/api";
import { useLoad } from "@/lib/hooks";
import { Button, Card, ErrorBox, Loading } from "./ui";

// Loads an /api/admin route with the tab's admin token. The server enforces the token; this only asks for it.
export default function AdminAccess({ path, children, refreshMs }: {
  path: string;
  children: (data: any, reload: () => Promise<void>) => ReactNode;
  refreshMs?: number; // poll for live data; paused while the tab is hidden
}) {
  const [attempt, setAttempt] = useState(0);
  const [draft, setDraft] = useState("");
  const res = useLoad(() => api.admin(path), [path, attempt]);
  const { reload, error } = res;
  useEffect(() => {
    if (!refreshMs || error) return;
    const id = window.setInterval(() => document.visibilityState === "visible" && reload(), refreshMs);
    return () => window.clearInterval(id);
  }, [refreshMs, error, reload]);

  if (res.loading && !res.data) return <Loading />;
  const err = res.error instanceof ApiError ? res.error : null;
  if (err?.code === "admin_not_configured") {
    return (
      <Card className="max-w-xl p-6">
        <h2 className="font-semibold">Admin access is turned off</h2>
        <p className="mt-1 text-sm text-ink-soft">
          Set <code className="rounded bg-paper-sunk px-1">PERKPILOT_ADMIN_TOKEN</code> in <code className="rounded bg-paper-sunk px-1">backend/.env</code>, restart the backend, then reload this page.
        </p>
      </Card>
    );
  }
  if (err?.code === "unauthorized") {
    return (
      <Card className="max-w-xl p-6">
        <h2 className="font-semibold">Enter the admin token</h2>
        <p className="mt-1 text-sm text-ink-soft">Store orders and order audits are private merchant data, so this view needs the admin token.</p>
        <form
          className="mt-4 flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            adminToken.set(draft.trim());
            setAttempt((n) => n + 1);
          }}
        >
          <label className="sr-only" htmlFor="admin-token">Admin token</label>
          <input
            id="admin-token"
            type="password"
            autoComplete="off"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-paper-line bg-white px-3 py-2 text-sm outline-none focus:border-ink-faint"
          />
          <Button type="submit" disabled={!draft.trim()}>Unlock</Button>
        </form>
        {adminToken.get() && <p className="mt-2 text-sm text-fail">That token was rejected.</p>}
      </Card>
    );
  }
  if (res.error) return <ErrorBox error={res.error} onRetry={res.reload} />;
  return (
    <>
      {children(res.data, res.reload)}
      <button
        className="mt-6 text-xs text-ink-mute underline-offset-2 hover:underline"
        onClick={() => {
          adminToken.set("");
          setDraft("");
          setAttempt((n) => n + 1);
        }}
      >
        Lock admin access in this tab
      </button>
    </>
  );
}
