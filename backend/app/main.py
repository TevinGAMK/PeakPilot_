"""PerkPilot FastAPI application."""

from __future__ import annotations

import os
import secrets

import json

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import demo, learning, metrics, resolution, shopping, shopify_stream
from .ai import ai_status
from .core import MERCHANT_ID, DomainError, live_mode, load_env, now
from .evaluation import run_evaluation
from .fixtures import seed
from .models import (AdvanceRequest, CheckoutConfirmRequest, ClaimRequest, ClarifyRequest, ConfirmIntentRequest, OutcomeRequest,
                     ParseRequest, RecommendRequest, RemedyAcceptRequest, RuleDecisionRequest, RuleToggleRequest, VerifyRequest)
from .pricing import catalog_facets, sizes_in_order
from .records import customer_purchase, integrity, is_verified, next_delivery_time, shipment_for
from .store import BaseStore, create_store
from .verifier import active_rules_for


def require_admin(authorization: str = Header(default="")) -> None:
    """Audit data (record hashes, event chain, per-rule checks, internal IDs) needs the admin bearer token in every
    mode, demo included. Without PERKPILOT_ADMIN_TOKEN configured, the audit routes stay closed."""
    token = os.environ.get("PERKPILOT_ADMIN_TOKEN", "")
    if not token:
        raise DomainError("Audit access is disabled. Set PERKPILOT_ADMIN_TOKEN on the server to enable it.", 401, "admin_not_configured")
    if not secrets.compare_digest(authorization.removeprefix("Bearer ").encode(), token.encode()):
        raise DomainError("Admin authentication required.", 401, "unauthorized")


def customer_quote(q: dict) -> dict:
    return {k: v for k, v in q.items() if k != "quote_hash"}


class PriceChange(BaseModel):
    price_cents: int = Field(ge=100, le=1_000_00)


def create_app(store: BaseStore | None = None) -> FastAPI:
    load_env()
    demo_mode = not live_mode()
    app = FastAPI(title="PerkPilot API", version="1.0.0",
                  description="Verified AI commerce demo. All payments, refunds and shipments are simulated." if demo_mode else
                  "Verified AI commerce assistant (live mode: no simulated payments, refunds or shipping).")
    origins = ["*"] if demo_mode else [o.strip() for o in os.environ.get("PERKPILOT_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])
    app.state.store = store or create_store(live=not demo_mode)
    if demo_mode and not app.state.store.get("meta", "seed_info"):
        seed(app.state.store)

    if not demo_mode:
        admin_token = os.environ.get("PERKPILOT_ADMIN_TOKEN", "")
        if not admin_token:
            raise RuntimeError("PERKPILOT_MODE=live requires PERKPILOT_ADMIN_TOKEN.")

        # ponytail: one shared merchant bearer token; replace with Shopify Admin session auth before multi-staff use.
        @app.middleware("http")
        async def _merchant_auth(request: Request, call_next):
            if request.url.path.startswith(("/api/merchant", "/api/eval")) and request.method != "OPTIONS":
                given = request.headers.get("authorization", "").removeprefix("Bearer ")
                if not secrets.compare_digest(given.encode(), admin_token.encode()):
                    return JSONResponse(status_code=401, content={"error": {"code": "unauthorized", "message": "Merchant authentication required."}})
            return await call_next(request)

    def S() -> BaseStore:
        return app.state.store

    def shopper_visible(p: dict) -> bool:
        # Live mode never presents a simulated payment or shipment as a real order.
        return demo_mode or not p["payment_method_reference"].get("simulated", True)

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}})

    # ------------------------------------------------------------ meta / demo

    @app.get("/api/health")
    def health():
        return {"ok": True, "mode": "demo" if demo_mode else "live", "store": S().mode, "ai": ai_status(), "now": now(S()).isoformat()}

    # Demo controls and every simulated money/fulfilment effect exist only in demo mode.
    if demo_mode:
        register_demo_routes(app, S)

    # ------------------------------------------------------------ catalog

    @app.get("/api/products")
    def products(category: str | None = None):
        items = S().find("products", {"merchant_id": MERCHANT_ID, "status": "active"})
        if category:
            items = [p for p in items if p["category"] == category]
        return {"products": sorted(items, key=lambda p: (p["category"] != "running_shoes", p["name"]))}

    @app.get("/api/catalog/facets")
    def facets():
        return catalog_facets(S())

    @app.get("/api/products/{product_id}")
    def product(product_id: str):
        p = S().get("products", product_id)
        if not p:
            raise DomainError("Unknown product.", 404, "not_found")
        sizes = sizes_in_order(p)
        guidance = {size: [{"rule_id": r["rule_id"], "message": r["customer_message"], "suggest_size": r["proposed_action"]["suggest_size"]}
                           for r in active_rules_for(S(), product_id, size)] for size in sizes}
        offers = [o for o in S().find("offers") if o["status"] == "active" and (not o["eligible_products"] or product_id in o["eligible_products"])]
        return {"product": p, "guidance_by_size": {k: v for k, v in guidance.items() if v}, "offers": offers}

    # ------------------------------------------------------------ shopping

    @app.post("/api/intent/parse")
    def intent_parse(body: ParseRequest):
        return shopping.parse_intent(S(), body.shopper_id, body.text)

    @app.post("/api/intent/confirm")
    def intent_confirm(body: ConfirmIntentRequest):
        return shopping.confirm_intent(S(), body.intent_id, body.confirmed_requirements, body.change_reason)

    @app.get("/api/intent/{intent_id}")
    def intent_get(intent_id: str):
        return shopping.get_intent(S(), intent_id)

    @app.post("/api/recommendations")
    def recommendations(body: RecommendRequest):
        return shopping.recommend(S(), body.intent_id, body.apply_learned_rules)

    @app.post("/api/checkout/verify")
    def checkout_verify(body: VerifyRequest):
        return customer_quote(shopping.verify_proposal(S(), body.intent_id, body.proposal.model_dump()))

    @app.get("/api/checkout/quotes/{quote_id}")
    def quote_get(quote_id: str):
        q = shopping.get_quote(S(), quote_id)
        intent = shopping.get_intent(S(), q["intent_id"])
        return {"quote": customer_quote(q),
                "intent": {k: intent[k] for k in ("intent_id", "status", "confirmed_requirements", "confirmation_version")},
                "product": S().get("products", q["proposal"]["product_id"]),
                "payment_method": S().get("payment_methods", q["proposal"]["payment_method_id"])}

    # ------------------------------------------------------------ purchases

    @app.get("/api/purchases")
    def purchases(shopper_id: str = Query(...)):
        s = S()
        out = [{**customer_purchase(s, p), "claims": _claim_summaries(s, p["purchase_id"])}
               for p in s.find("purchases", {"shopper_id": shopper_id}) if shopper_visible(p)]
        return {"purchases": sorted(out, key=lambda p: p["created_at"], reverse=True)}

    @app.get("/api/purchases/{purchase_id}")
    def purchase(purchase_id: str, shopper_id: str = Query(...)):
        s = S()
        p = s.get("purchases", purchase_id)
        if not p or p["shopper_id"] != shopper_id or not shopper_visible(p):
            raise DomainError("Purchase not found for this shopper.", 404, "not_found")
        children = [c for c in s.find("purchases", {"parent_purchase_id": purchase_id}) if shopper_visible(c)]
        return {"purchase": customer_purchase(s, p), "claims": _claim_summaries(s, purchase_id),
                "exchange_orders": [customer_purchase(s, c) for c in children], "product": s.get("products", p["product_id"])}

    def _claim_summaries(s: BaseStore, purchase_id: str) -> list[dict]:
        return [{k: c[k] for k in ("claim_id", "status", "classification")} for c in s.find("claims", {"purchase_id": purchase_id})]

    # ------------------------------------------------------------ Shopify live data

    @app.post("/api/shopify/webhooks")
    async def shopify_webhook(request: Request, background: BackgroundTasks):
        # Public endpoint: the HMAC over the raw body, signed with the app's client secret, is the authentication.
        raw = await request.body()
        h = request.headers
        if not shopify_stream.verify_webhook(raw, h.get("x-shopify-hmac-sha256", ""), os.environ.get("SHOPIFY_CLIENT_SECRET", "")):
            raise DomainError("Webhook signature is invalid.", 401, "invalid_signature")
        if h.get("x-shopify-shop-domain") and h["x-shopify-shop-domain"] != shopify_stream.shop_domain():
            raise DomainError("Webhook is for a different shop.", 403, "wrong_shop")
        result = shopify_stream.handle_webhook(S(), h.get("x-shopify-topic", ""), h.get("x-shopify-webhook-id", ""), json.loads(raw or b"{}"))
        if result == "catalog":
            background.add_task(shopify_stream.resync_catalog, S())  # ack within Shopify's 5 s; sync afterwards
        return {"ok": True, "result": result}

    @app.get("/api/admin/shopify", dependencies=[Depends(require_admin)])
    def shopify_overview():
        return shopify_stream.overview(S())

    @app.post("/api/admin/shopify/connect", dependencies=[Depends(require_admin)])
    def shopify_connect():
        # The callback comes only from server config, so a caller can't point the store's webhooks elsewhere.
        public = os.environ.get("PERKPILOT_PUBLIC_URL", "").strip()
        if not shopify_stream.configured():
            raise DomainError("Shopify isn't configured. Set SHOPIFY_SHOP and the app credentials in backend/.env.", 422, "shopify_not_configured")
        if not public.startswith("https://"):
            raise DomainError("Set PERKPILOT_PUBLIC_URL in backend/.env to the public HTTPS address of this app (e.g. your ngrok URL).",
                              422, "public_url_missing")
        try:
            return shopify_stream.connect(S(), public)
        except (httpx.HTTPError, RuntimeError) as exc:
            raise DomainError(f"Shopify request failed: {exc}", 502, "shopify_error")

    # ------------------------------------------------------------ admin audit (bearer token in every mode)

    @app.get("/api/admin/purchases", dependencies=[Depends(require_admin)])
    def admin_purchases():
        s = S()
        rows = [{**{k: p.get(k) for k in ("purchase_id", "kind", "shopper_id", "product_name", "approved_charge_cents", "status",
                                          "source", "created_at")}, "verified": is_verified(s, p)} for p in s.find("purchases")]
        return {"purchases": sorted(rows, key=lambda p: p["created_at"], reverse=True)}

    @app.get("/api/admin/purchases/{purchase_id}/audit", dependencies=[Depends(require_admin)])
    def admin_purchase_audit(purchase_id: str):
        s = S()
        p = s.get("purchases", purchase_id)
        if not p:
            raise DomainError("Unknown purchase.", 404, "not_found")
        return {"purchase": p, "integrity": integrity(s, p), "verified": is_verified(s, p),
                "verification_results": p["verification_results"],
                "events": sorted(s.find("events", {"purchase_id": purchase_id}), key=lambda e: e["seq"]),
                "shipment": shipment_for(s, purchase_id), "claims": s.find("claims", {"purchase_id": purchase_id}),
                "intent": s.get("intents", p["intent_id"]) if p.get("intent_id") else None,
                "quote": s.get("quotes", p["quote_id"]) if p.get("quote_id") else None}

    # ------------------------------------------------------------ claims / remedies

    @app.post("/api/claims")
    def claim_create(body: ClaimRequest):
        c = resolution.create_claim(S(), body.purchase_id, body.shopper_id, body.complaint_text)
        return resolution.claim_view(S(), c["claim_id"])

    @app.get("/api/claims/{claim_id}")
    def claim_get(claim_id: str):
        return resolution.claim_view(S(), claim_id)

    @app.post("/api/claims/{claim_id}/clarify")
    def claim_clarify(claim_id: str, body: ClarifyRequest):
        resolution.clarify_claim(S(), claim_id, body.text)
        return resolution.claim_view(S(), claim_id)

    @app.get("/api/claims/{claim_id}/remedies")
    def claim_remedies(claim_id: str):
        return {"remedies": resolution.generate_remedies(S(), claim_id)}

    @app.post("/api/claims/{claim_id}/outcome")
    def claim_outcome(claim_id: str, body: OutcomeRequest):
        res = resolution.confirm_outcome(S(), claim_id, body.shopper_id, body.outcome)
        return {**resolution.claim_view(S(), claim_id), "rule_proposal": res["rule_proposal"]}

    # ------------------------------------------------------------ merchant

    @app.get("/api/merchant/rules")
    def rules():
        return learning.rules_overview(S())

    @app.post("/api/merchant/rules/{rule_id}/approve")
    def rule_approve(rule_id: str, body: RuleDecisionRequest):
        return learning.approve_rule(S(), rule_id, body.actor, body.note)

    @app.post("/api/merchant/rules/{rule_id}/reject")
    def rule_reject(rule_id: str, body: RuleDecisionRequest):
        return learning.reject_rule(S(), rule_id, body.actor, body.note)

    @app.post("/api/merchant/rules/{rule_id}/toggle")
    def rule_toggle(rule_id: str, body: RuleToggleRequest):
        return learning.toggle_rule(S(), rule_id, body.enabled, body.actor)

    @app.get("/api/merchant/dashboard")
    def dashboard():
        return metrics.dashboard(S())

    @app.get("/api/merchant/claims")
    def merchant_claims():
        cs = sorted(S().find("claims", {"merchant_id": MERCHANT_ID}), key=lambda c: c["created_at"], reverse=True)
        return {"claims": cs}

    @app.post("/api/eval/run")
    def eval_run():
        return run_evaluation(S())

    @app.get("/api/eval/runs/latest")
    def eval_latest():
        runs = sorted(S().find("eval_runs"), key=lambda r: r["timestamp"], reverse=True)
        return {"run": runs[0] if runs else None}

    return app



def register_demo_routes(app: FastAPI, S) -> None:
    """Reset/clock/price scenarios plus simulated checkout payment and remedy execution."""

    @app.get("/api/demo/state")
    def demo_state():
        s = S()
        return {"now": now(s).isoformat(), "timezone": "America/Los_Angeles", "store": s.mode, "ai": ai_status(),
                "merchant": s.get("merchants", MERCHANT_ID), "shoppers": s.find("shoppers"),
                "payment_methods": s.find("payment_methods"),
                "next_delivery_at": (nd.isoformat() if (nd := next_delivery_time(s)) else None),
                "disclosure": "Simulated store, shoppers, payments, refunds and shipping. No real money moves."}

    @app.post("/api/demo/reset")
    def demo_reset():
        return demo.reset(S())

    @app.post("/api/demo/advance")
    def demo_advance(body: AdvanceRequest):
        return demo.advance(S(), body.days, body.hours, body.until, body.purchase_id)

    @app.post("/api/merchant/products/{product_id}/price")
    def change_price(product_id: str, body: PriceChange):
        return demo.change_price(S(), product_id, body.price_cents)

    @app.post("/api/checkout/confirm")
    def checkout_confirm(body: CheckoutConfirmRequest):
        res = shopping.confirm_checkout(S(), body.quote_id, body.shopper_id, body.approved_charge_cents, body.shopper_approved)
        return {**res, "purchase": customer_purchase(S(), res["purchase"]) if res["purchase"] else None}

    @app.post("/api/remedies/{remedy_id}/accept")
    def remedy_accept(remedy_id: str, body: RemedyAcceptRequest):
        res = resolution.accept_remedy(S(), remedy_id, body.shopper_id, body.confirm, body.idempotency_key, body.payout_method)
        return {**resolution.claim_view(S(), res["claim"]["claim_id"]), "duplicate": res["duplicate"], "message": res.get("message")}


app = create_app()
