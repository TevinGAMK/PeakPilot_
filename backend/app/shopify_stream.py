"""Live Shopify data for the merchant dashboard: signed webhooks in, order summaries and catalog resyncs out.

Shopify pushes product, inventory and order changes to ``POST /api/shopify/webhooks``. Every request is
checked against the app's client secret (HMAC-SHA256 of the raw body) before anything is stored.

* product / inventory topics -> a coalesced, in-process catalog resync (the same projection as ``app.shopify sync``)
* order topics               -> an order summary in ``shopify_orders``: totals, statuses and line items only.
                                No customer names, emails or addresses are stored.

``connect`` registers the webhooks (only for topics the app has scopes for), syncs the catalog and backfills
recent orders. It runs inside the API process so the file store never loses writes to a second process.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from decimal import Decimal

import httpx

from .core import wall_now
from .shopify import API_VERSION, PREFIX, _numeric_id, fetch_products, get_token, sync_catalog
from .store import BaseStore

TOPICS_BY_SCOPE = {
    "read_products": ["PRODUCTS_CREATE", "PRODUCTS_UPDATE", "PRODUCTS_DELETE"],
    "read_inventory": ["INVENTORY_LEVELS_UPDATE"],
    "read_orders": ["ORDERS_CREATE", "ORDERS_UPDATED"],
}
CATALOG_TOPICS = {"products/create", "products/update", "products/delete", "inventory_levels/update"}
ORDER_TOPICS = {"orders/create", "orders/updated"}
STATUS_ID = "shopify_status"
# REST says null / "partial"; GraphQL says UNFULFILLED / PARTIALLY_FULFILLED. Normalise to the GraphQL words.
FULFILLMENT = {"": "unfulfilled", "partial": "partially_fulfilled"}
CATALOG_LABELS = {"products/create": "Product added", "products/update": "Product updated", "products/delete": "Product removed",
                  "inventory_levels/update": "Inventory changed"}

ORDERS_QUERY = """
query Orders($after: String) {
  orders(first: 50, after: $after, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 20) { nodes { title variantTitle quantity originalUnitPriceSet { shopMoney { amount } } product { id } } }
    }
  }
}
"""


def shop_domain() -> str:
    return os.environ.get("SHOPIFY_SHOP", "").removeprefix("https://").strip("/")


def configured() -> bool:
    return bool(shop_domain() and (os.environ.get("SHOPIFY_ADMIN_TOKEN") or os.environ.get("SHOPIFY_CLIENT_SECRET")))


# ---------------------------------------------------------------- webhooks in


def verify_webhook(raw: bytes, given_hmac: str, secret: str) -> bool:
    """Shopify signs the raw body with the app's client secret; compare in constant time."""
    if not secret or not given_hmac:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, given_hmac)


def _cents(amount) -> int:
    return int(Decimal(str(amount or "0")) * 100)


def _order_doc(*, oid: str, name: str, created_at: str, cancelled: bool, financial: str | None, fulfillment: str | None,
               total, currency: str, items: list[dict]) -> dict:
    return {
        "_id": oid, "order_id": oid, "name": name, "created_at": created_at, "cancelled": cancelled,
        "financial_status": (financial or "pending").lower(),
        "fulfillment_status": FULFILLMENT.get((fulfillment or "").lower(), (fulfillment or "").lower()),
        "total_cents": _cents(total), "currency": currency, "items": items, "updated_at": wall_now(),
    }


def order_from_webhook(p: dict) -> dict:
    """REST-shaped webhook payload -> order summary. Customer fields are deliberately not read."""
    return _order_doc(
        oid=str(p["id"]), name=p.get("name") or f"#{p.get('order_number', p['id'])}", created_at=p.get("created_at") or wall_now(),
        cancelled=bool(p.get("cancelled_at")), financial=p.get("financial_status"), fulfillment=p.get("fulfillment_status"),
        total=p.get("current_total_price", p.get("total_price")), currency=p.get("currency", "USD"),
        items=[{"title": li.get("title", ""), "variant": li.get("variant_title") or "", "quantity": li.get("quantity", 1),
                "unit_cents": _cents(li.get("price")), "product_id": f"{PREFIX}{li['product_id']}" if li.get("product_id") else None}
               for li in p.get("line_items", [])],
    )


def order_from_graphql(n: dict) -> dict:
    money = n["currentTotalPriceSet"]["shopMoney"]
    return _order_doc(
        oid=_numeric_id(n["id"]), name=n["name"], created_at=n["createdAt"], cancelled=bool(n.get("cancelledAt")),
        financial=n.get("displayFinancialStatus"), fulfillment=n.get("displayFulfillmentStatus"),
        total=money["amount"], currency=money["currencyCode"],
        items=[{"title": li["title"], "variant": li.get("variantTitle") or "", "quantity": li["quantity"],
                "unit_cents": _cents(li["originalUnitPriceSet"]["shopMoney"]["amount"]),
                "product_id": f"{PREFIX}{_numeric_id(li['product']['id'])}" if li.get("product") else None}
               for li in n["lineItems"]["nodes"]],
    )


def _log(store: BaseStore, kind: str, summary: str, webhook_id: str | None = None) -> None:
    at = wall_now()
    eid = webhook_id or f"{kind}-{time.time_ns()}"
    store.put("shopify_events", {"_id": eid, "event_id": eid, "kind": kind, "summary": summary, "at": at})
    status = store.get("meta", STATUS_ID) or {"_id": STATUS_ID}
    status["last_event_at"] = at
    store.put("meta", status)


def handle_webhook(store: BaseStore, topic: str, webhook_id: str, payload: dict) -> str:
    """Apply one verified webhook. Returns what happened; a repeated webhook id is acknowledged and ignored."""
    if webhook_id and store.get("shopify_events", webhook_id):
        return "duplicate"
    if topic in ORDER_TOPICS:
        order = order_from_webhook(payload)
        is_new = store.get("shopify_orders", order["order_id"]) is None
        store.put("shopify_orders", order)
        _log(store, "order", f"Order {order['name']} {'placed' if is_new else 'updated'}: "
                             f"{order['financial_status'].replace('_', ' ')}, {order['fulfillment_status'].replace('_', ' ')}", webhook_id)
        return "order"
    if topic in CATALOG_TOPICS:
        title = payload.get("title")
        _log(store, "catalog", f"{CATALOG_LABELS[topic]}{f': {title}' if title else ''}", webhook_id)
        return "catalog"
    _log(store, "other", f"Ignored topic {topic}", webhook_id)
    return "ignored"


# ---------------------------------------------------------------- catalog resync (coalesced)

_sync_lock = threading.Lock()
_sync_pending = threading.Event()


def resync_catalog(store: BaseStore, client: httpx.Client | None = None) -> dict | None:
    """Pull the catalog and project it. Bursts (an order touches several inventory levels) collapse into
    at most one running sync plus one follow-up, instead of one full sync per webhook."""
    _sync_pending.set()
    if not _sync_lock.acquire(blocking=False):
        return None  # the running sync will pick up the pending flag
    try:
        res = None
        while _sync_pending.is_set():
            _sync_pending.clear()
            with (client or httpx.Client()) as c:
                shop = shop_domain()
                nodes = fetch_products(c, shop, get_token(c, shop), os.environ.get("SHOPIFY_API_VERSION", API_VERSION))
            res = sync_catalog(store, nodes, os.environ.get("SHOPIFY_SIZE_OPTION", "Size"),
                               os.environ.get("SHOPIFY_COLOR_OPTION", "Color"), shop)
            status = store.get("meta", STATUS_ID) or {"_id": STATUS_ID}
            status.update(last_sync_at=wall_now(), last_sync={k: len(v) for k, v in res.items()})
            store.put("meta", status)
            client = None  # a follow-up pass opens its own client
        return res
    finally:
        _sync_lock.release()


# ---------------------------------------------------------------- connect: scopes, webhooks, backfill


def _gql(client: httpx.Client, shop: str, token: str, query: str, variables: dict | None = None) -> dict:
    r = client.post(f"https://{shop}/admin/api/{os.environ.get('SHOPIFY_API_VERSION', API_VERSION)}/graphql.json",
                    json={"query": query, "variables": variables or {}}, headers={"X-Shopify-Access-Token": token}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"Shopify GraphQL error: {body['errors']}")
    return body["data"]


def register_webhooks(client: httpx.Client, shop: str, token: str, callback: str) -> dict:
    """Subscribe every topic the app's scopes allow to ``callback``; replace subscriptions pointing elsewhere
    (e.g. an old tunnel URL). Topics without a scope are reported, not attempted."""
    data = _gql(client, shop, token, "{ currentAppInstallation { accessScopes { handle } } "
                                     "webhookSubscriptions(first: 100) { nodes { id topic uri } } }")
    scopes = {s["handle"] for s in data["currentAppInstallation"]["accessScopes"]}
    existing = data["webhookSubscriptions"]["nodes"]
    wanted = [t for scope, topics in TOPICS_BY_SCOPE.items() if scope in scopes for t in topics]
    registered, errors = [], []
    for t in wanted:
        same = [s for s in existing if s["topic"] == t and s["uri"] == callback]
        for stale in (s for s in existing if s["topic"] == t and s["uri"] != callback):
            _gql(client, shop, token, "mutation($id: ID!) { webhookSubscriptionDelete(id: $id) { userErrors { message } } }",
                 {"id": stale["id"]})
        if not same:
            out = _gql(client, shop, token,
                       "mutation($topic: WebhookSubscriptionTopic!, $uri: String!) { webhookSubscriptionCreate("
                       "topic: $topic, webhookSubscription: {uri: $uri, format: JSON}) { userErrors { message } } }",
                       {"topic": t, "uri": callback})["webhookSubscriptionCreate"]
            if out["userErrors"]:
                errors.append(f"{t}: {out['userErrors'][0]['message']}")
                continue
        registered.append(t)
    missing = [s for s in TOPICS_BY_SCOPE if s not in scopes]
    return {"scopes": sorted(scopes), "registered": registered, "missing_scopes": missing, "errors": errors}


def backfill_orders(store: BaseStore, client: httpx.Client, shop: str, token: str, limit: int = 100) -> int:
    after, n = None, 0
    while n < limit:
        page = _gql(client, shop, token, ORDERS_QUERY, {"after": after})["orders"]
        for node in page["nodes"]:
            store.put("shopify_orders", order_from_graphql(node))
            n += 1
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return n


def connect(store: BaseStore, public_url: str, client: httpx.Client | None = None) -> dict:
    """Register webhooks to ``{public_url}/api/shopify/webhooks``, resync the catalog, backfill orders."""
    shop = shop_domain()
    with (client or httpx.Client()) as c:
        token = get_token(c, shop)
        hooks = register_webhooks(c, shop, token, public_url.rstrip("/") + "/api/shopify/webhooks")
        orders = backfill_orders(store, c, shop, token) if "read_orders" in hooks["scopes"] else 0
        catalog = sync_catalog(store, fetch_products(c, shop, token, os.environ.get("SHOPIFY_API_VERSION", API_VERSION)),
                               os.environ.get("SHOPIFY_SIZE_OPTION", "Size"), os.environ.get("SHOPIFY_COLOR_OPTION", "Color"), shop)
    status = store.get("meta", STATUS_ID) or {"_id": STATUS_ID}
    status.update(webhooks=hooks, connected_at=wall_now(), callback=public_url.rstrip("/") + "/api/shopify/webhooks",
                  last_sync_at=wall_now(), last_sync={k: len(v) for k, v in catalog.items()})
    store.put("meta", status)
    _log(store, "connect", f"Connected: {len(hooks['registered'])} webhook topics, {len(catalog['synced'])} products, {orders} orders")
    return {**hooks, "orders_backfilled": orders, "products_synced": len(catalog["synced"])}


# ---------------------------------------------------------------- dashboard


def overview(store: BaseStore) -> dict:
    status = store.get("meta", STATUS_ID) or {}
    products = [p for p in store.find("products") if p.get("source") == "shopify" and p["status"] == "active"]
    variants = [(p, v) for p in products for v in p["variants"]]
    in_stock = sum(1 for p, v in variants if p["inventory_by_variant"].get(v["variant_id"], 0) > 0)
    orders = sorted(store.find("shopify_orders"), key=lambda o: o["created_at"], reverse=True)
    live = [o for o in orders if not o["cancelled"]]
    today = wall_now()[:10]
    return {
        "configured": configured(), "shop": shop_domain(),
        "connected": bool(status.get("webhooks")), "webhooks": status.get("webhooks"), "callback": status.get("callback"),
        "last_sync_at": status.get("last_sync_at"), "last_event_at": status.get("last_event_at"),
        "catalog": {"products": len(products), "variants": len(variants), "in_stock_variants": in_stock,
                    "sold_out": sorted({p["name"] for p in products if not any(p["inventory_by_variant"].get(v["variant_id"], 0) > 0
                                                                             for v in p["variants"])})},
        "orders": {
            "count": len(live), "today": sum(1 for o in live if o["created_at"][:10] == today),
            "revenue_cents": sum(o["total_cents"] for o in live if o["financial_status"] in ("paid", "partially_refunded")),
            "awaiting_fulfillment": sum(1 for o in live if o["fulfillment_status"] != "fulfilled"),
            "refunded": sum(1 for o in orders if o["financial_status"] in ("refunded", "partially_refunded")),
        },
        "recent_orders": orders[:10],
        "events": sorted(store.find("shopify_events"), key=lambda e: e["at"], reverse=True)[:20],
    }
