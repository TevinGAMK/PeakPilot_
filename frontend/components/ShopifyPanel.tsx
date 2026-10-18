"use client";

import { useEffect, useState } from "react";
import AdminAccess from "./AdminAccess";
import { Badge, Button, Card, ErrorBox, cx } from "./ui";
import { api } from "@/lib/api";
import { dateTime, money } from "@/lib/format";

const FINANCIAL: Record<string, { label: string; tone: "pass" | "warn" | "neutral" | "fail" }> = {
  paid: { label: "Paid", tone: "pass" },
  pending: { label: "Payment pending", tone: "warn" },
  authorized: { label: "Authorized", tone: "neutral" },
  partially_paid: { label: "Partially paid", tone: "warn" },
  partially_refunded: { label: "Partially refunded", tone: "neutral" },
  refunded: { label: "Refunded", tone: "neutral" },
  voided: { label: "Voided", tone: "fail" },
};

function ago(iso: string | null, now: number) {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return dateTime(iso);
}

function Figure({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="px-5 py-4">
      <p className="text-xs text-ink-mute">{label}</p>
      <p className="mt-1 font-display text-3xl font-semibold tabular-nums">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-ink-mute">{sub}</p>}
    </div>
  );
}

function Live({ d, reload }: { d: any; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<any>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  async function sync() {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.adminPost("/api/admin/shopify/connect"));
      await reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  if (!d.configured) {
    return (
      <Card className="p-5">
        <h2 className="font-semibold">Shopify store</h2>
        <p className="mt-1 text-sm text-ink-soft">Add SHOPIFY_SHOP and your app's credentials to backend/.env, restart the backend, then connect here.</p>
      </Card>
    );
  }
  const missingOrders = d.webhooks?.missing_scopes?.includes("read_orders");
  const fresh = d.last_event_at && now - new Date(d.last_event_at).getTime() < 120_000;
  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-paper-line px-5 py-4">
        <div>
          <h2 className="font-semibold">Shopify store</h2>
          <p className="text-sm text-ink-mute">{d.shop}</p>
        </div>
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-2 text-xs text-ink-mute" aria-live="polite">
            <span className={cx("h-2 w-2 rounded-full", d.connected ? "bg-pass" : "bg-ink-faint", fresh && "animate-pulse")} aria-hidden="true" />
            {d.connected ? `Live. Last change ${ago(d.last_event_at, now)}` : "Not connected"}
          </span>
          <Button size="sm" variant={d.connected ? "secondary" : "primary"} loading={busy} onClick={sync}>
            {d.connected ? "Sync now" : "Connect store"}
          </Button>
        </div>
      </div>

      <div className="px-5 pt-4">
        <ErrorBox error={error} />
        {result?.errors?.length > 0 && <p className="mt-2 text-sm text-warn">Some webhooks weren't registered: {result.errors.join("; ")}</p>}
        {missingOrders && (
          <div className="rounded-xl border border-warn/30 bg-warn-soft px-4 py-3 text-sm text-warn">
            <p className="font-semibold">Orders aren't streaming yet</p>
            <p className="mt-0.5 text-ink-soft">
              Your app doesn't have the <code className="rounded bg-white/70 px-1">read_orders</code> scope. In the Shopify Dev Dashboard, add it to
              your app's scopes, release a new version, approve it in your store, then press Sync now.
            </p>
          </div>
        )}
      </div>

      <section aria-label="Store totals" className="grid grid-cols-2 divide-paper-line lg:grid-cols-4 lg:divide-x">
        <Figure label="Orders" value={missingOrders ? "—" : d.orders.count} sub={missingOrders ? "needs read_orders" : `${d.orders.today} today`} />
        <Figure label="Paid revenue" value={missingOrders ? "—" : money(d.orders.revenue_cents)} sub={d.orders.refunded ? `${d.orders.refunded} refunded` : undefined} />
        <Figure label="Awaiting fulfillment" value={missingOrders ? "—" : d.orders.awaiting_fulfillment} />
        <Figure
          label="Variants in stock"
          value={`${d.catalog.in_stock_variants}/${d.catalog.variants}`}
          sub={d.catalog.sold_out.length ? `Sold out: ${d.catalog.sold_out.join(", ")}` : `${d.catalog.products} products`}
        />
      </section>

      <div className="grid gap-6 border-t border-paper-line p-5 lg:grid-cols-[1.4fr_1fr]">
        <div>
          <h3 className="text-sm font-semibold">Recent orders</h3>
          {d.recent_orders.length === 0 ? (
            <p className="mt-2 text-sm text-ink-mute">{missingOrders ? "Orders appear here once read_orders is granted." : "No orders yet. New orders appear here within seconds."}</p>
          ) : (
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-ink-mute">
                  <tr>
                    <th className="py-1.5 pr-3 font-medium">Order</th>
                    <th className="py-1.5 pr-3 font-medium">Items</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Total</th>
                    <th className="py-1.5 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {d.recent_orders.map((o: any) => {
                    const f = FINANCIAL[o.financial_status] ?? { label: o.financial_status, tone: "neutral" as const };
                    return (
                      <tr key={o.order_id} className="border-t border-paper-line align-top">
                        <td className="py-2 pr-3">
                          <p className="font-medium">{o.name}</p>
                          <p className="text-xs text-ink-mute">{dateTime(o.created_at)}</p>
                        </td>
                        <td className="py-2 pr-3 text-ink-soft">
                          {o.items.map((i: any, k: number) => (
                            <p key={k}>{i.quantity} × {i.title}{i.variant && <span className="text-ink-mute"> ({i.variant})</span>}</p>
                          ))}
                        </td>
                        <td className="py-2 pr-3 text-right tabular-nums">{money(o.total_cents)}</td>
                        <td className="space-x-1 py-2">
                          {o.cancelled ? <Badge tone="fail">Cancelled</Badge> : <Badge tone={f.tone}>{f.label}</Badge>}
                          <Badge tone={o.fulfillment_status === "fulfilled" ? "pass" : "neutral"}>{o.fulfillment_status.replace(/_/g, " ")}</Badge>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div>
          <h3 className="text-sm font-semibold">Store activity</h3>
          {d.events.length === 0 ? (
            <p className="mt-2 text-sm text-ink-mute">{d.connected ? "Waiting for the first change from Shopify." : "Connect the store to start receiving changes."}</p>
          ) : (
            <ol className="mt-2 space-y-2 text-sm">
              {d.events.map((e: any) => (
                <li key={e.event_id} className="flex justify-between gap-3">
                  <span>{e.summary}</span>
                  <span className="shrink-0 text-xs text-ink-mute">{ago(e.at, now)}</span>
                </li>
              ))}
            </ol>
          )}
          <p className="mt-4 text-xs text-ink-mute">Catalog last synced {ago(d.last_sync_at, now)}.</p>
        </div>
      </div>
    </Card>
  );
}

// Shopify pushes changes to the backend by webhook; this panel re-reads the result every 5 seconds.
export default function ShopifyPanel() {
  return (
    <AdminAccess path="/api/admin/shopify" refreshMs={5000}>
      {(d, reload) => <Live d={d} reload={reload} />}
    </AdminAccess>
  );
}
