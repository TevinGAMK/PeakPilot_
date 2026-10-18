"""Live Shopify data: signed webhooks, order summaries, catalog resync, webhook registration (Shopify is mocked)."""

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app import shopify_stream
from app.store import MemoryStore

from .conftest import ok
from .test_live_shopify import APPAREL

SECRET = "shpss_test"
ORDER = {
    "id": 5551, "name": "#1001", "created_at": "2026-10-18T09:00:00-07:00", "cancelled_at": None,
    "financial_status": "paid", "fulfillment_status": None, "total_price": "28.00", "current_total_price": "28.00",
    "currency": "USD", "email": "pat@example.com", "customer": {"first_name": "Pat", "email": "pat@example.com"},
    "shipping_address": {"address1": "1 Main St"},
    "line_items": [{"title": "Heavyweight Hoodie", "variant_title": "Grey / M", "quantity": 1, "price": "28.00", "product_id": 5}],
}


@pytest.fixture()
def shop(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SHOP", "x.myshopify.com")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "cid")
    monkeypatch.delenv("SHOPIFY_ADMIN_TOKEN", raising=False)


def send(client, topic, payload, webhook_id, secret=SECRET):
    raw = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    return client.post("/api/shopify/webhooks", content=raw, headers={
        "content-type": "application/json", "x-shopify-hmac-sha256": sig, "x-shopify-topic": topic,
        "x-shopify-webhook-id": webhook_id, "x-shopify-shop-domain": "x.myshopify.com"})


def test_webhooks_must_be_signed_by_the_app(client, store, shop):
    assert send(client, "orders/create", ORDER, "w1", secret="forged").status_code == 401
    assert store.find("shopify_orders") == []


def test_order_webhooks_store_a_pii_free_summary(client, store, shop):
    assert ok(send(client, "orders/create", ORDER, "w1"))["result"] == "order"
    [o] = store.find("shopify_orders")
    assert (o["name"], o["total_cents"], o["financial_status"], o["fulfillment_status"]) == ("#1001", 2800, "paid", "unfulfilled")
    assert o["items"][0]["product_id"] == "sfy_5"
    assert "pat" not in json.dumps(o).lower() and "main st" not in json.dumps(o).lower()  # no customer data kept
    assert ok(send(client, "orders/create", ORDER, "w1"))["result"] == "duplicate"  # Shopify retries are idempotent
    ok(send(client, "orders/updated", {**ORDER, "fulfillment_status": "fulfilled", "financial_status": "refunded"}, "w2"))
    [o] = store.find("shopify_orders")
    assert (o["financial_status"], o["fulfillment_status"]) == ("refunded", "fulfilled")
    assert [e["summary"] for e in store.find("shopify_events")] == [
        "Order #1001 placed: paid, unfulfilled", "Order #1001 updated: refunded, fulfilled"]


def test_catalog_webhooks_trigger_a_resync(client, store, shop, monkeypatch):
    calls = []
    monkeypatch.setattr(shopify_stream, "resync_catalog", lambda s: calls.append(s))
    assert ok(send(client, "inventory_levels/update", {"inventory_item_id": 1, "available": 3}, "w9"))["result"] == "catalog"
    assert len(calls) == 1 and store.find("shopify_events")[0]["summary"] == "Inventory changed"


def gql_transport(handler):
    def route(req):
        if req.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "tok"})
        body = json.loads(req.content)
        return httpx.Response(200, json={"data": handler(body["query"], body.get("variables") or {})})
    return httpx.Client(transport=httpx.MockTransport(route))


def test_register_replaces_stale_urls_and_skips_unscoped_topics():
    seen = []

    def handler(query, variables):
        seen.append((query.split("(")[0].split("{")[0].strip(), variables))
        if "currentAppInstallation" in query:
            return {"currentAppInstallation": {"accessScopes": [{"handle": "read_products"}, {"handle": "read_inventory"}]},
                    "webhookSubscriptions": {"nodes": [
                        {"id": "gid://1", "topic": "PRODUCTS_UPDATE", "uri": "https://old.ngrok-free.dev/api/shopify/webhooks"},
                        {"id": "gid://2", "topic": "PRODUCTS_CREATE", "uri": "https://new.example/api/shopify/webhooks"}]}}
        if "webhookSubscriptionDelete" in query:
            return {"webhookSubscriptionDelete": {"userErrors": []}}
        return {"webhookSubscriptionCreate": {"userErrors": []}}

    res = shopify_stream.register_webhooks(gql_transport(handler), "x.myshopify.com", "tok", "https://new.example/api/shopify/webhooks")
    assert res["registered"] == ["PRODUCTS_CREATE", "PRODUCTS_UPDATE", "PRODUCTS_DELETE", "INVENTORY_LEVELS_UPDATE"]
    assert res["missing_scopes"] == ["read_orders"] and not res["errors"]
    deleted = [v["id"] for q, v in seen if q == "mutation" and "id" in v]
    created = [v["topic"] for q, v in seen if q == "mutation" and "topic" in v]
    assert deleted == ["gid://1"] and created == ["PRODUCTS_UPDATE", "PRODUCTS_DELETE", "INVENTORY_LEVELS_UPDATE"]


def test_connect_backfills_orders_and_syncs_catalog(shop):
    def handler(query, variables):
        if "currentAppInstallation" in query:
            return {"currentAppInstallation": {"accessScopes": [{"handle": h} for h in ("read_products", "read_inventory", "read_orders")]},
                    "webhookSubscriptions": {"nodes": []}}
        if "webhookSubscriptionCreate" in query:
            return {"webhookSubscriptionCreate": {"userErrors": []}}
        if "orders(" in query:
            return {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{
                "id": "gid://shopify/Order/77", "name": "#1002", "createdAt": "2026-10-18T10:00:00Z", "cancelledAt": None,
                "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED",
                "currentTotalPriceSet": {"shopMoney": {"amount": "45.0", "currencyCode": "USD"}},
                "lineItems": {"nodes": [{"title": "Heavyweight Hoodie", "variantTitle": "Grey / XL", "quantity": 1,
                                         "originalUnitPriceSet": {"shopMoney": {"amount": "45.0"}}, "product": {"id": "gid://shopify/Product/5"}}]}}]}}
        return {"shop": {"currencyCode": "USD"}, "products": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": APPAREL[:2]}}

    store = MemoryStore()
    res = shopify_stream.connect(store, "https://new.example/", gql_transport(handler))
    assert res["orders_backfilled"] == 1 and res["products_synced"] == 2 and "ORDERS_CREATE" in res["registered"]
    view = shopify_stream.overview(store)
    assert view["connected"] and view["callback"] == "https://new.example/api/shopify/webhooks"
    assert view["orders"] == {"count": 1, "today": view["orders"]["today"], "revenue_cents": 4500, "awaiting_fulfillment": 1, "refunded": 0}
    assert view["catalog"]["products"] == 2 and view["recent_orders"][0]["name"] == "#1002"


def test_shopify_dashboard_routes_need_the_admin_token(client, admin, shop, monkeypatch):
    assert client.get("/api/admin/shopify").status_code == 401
    assert ok(client.get("/api/admin/shopify", headers=admin))["shop"] == "x.myshopify.com"
    monkeypatch.delenv("PERKPILOT_PUBLIC_URL", raising=False)
    r = client.post("/api/admin/shopify/connect", headers=admin)
    assert r.status_code == 422 and r.json()["error"]["code"] == "public_url_missing"
