# PerkPilot

**Buy it right. Fix it fast. Learn from every return.**

PerkPilot is an AI shopping and post-purchase assistant for a mid-sized footwear merchant. It checks every AI-assisted purchase against the conditions the shopper explicitly approved. It uses that same record to resolve problems after the sale, and it turns confirmed returns into merchant-approved guidance for the next shopper.

Built for DubHacks 2026, Visa track: *Reimagine Shopping*. The demo store, **StrideLab Running**, is fictional. Every payment, refund and shipment is simulated.

---

## The problem

AI shopping agents can misread requirements, misapply promotions or ignore shipping and tax. Afterward it can be hard to tell what the shopper actually approved. Returns systems know *what* was bought, but not the conditions behind it. Merchants also lack a safe way to turn a recurring problem into better guidance at the next checkout.

## The idea: one Verified Purchase Record

The shopper confirms a structured version of what they want. A deterministic Python verifier enforces it at checkout. The approved transaction is saved as a hash-chained record, and that record is reused:

| Stage | What happens | Who decides |
|---|---|---|
| Understand | Gemini (or the disclosed fallback interpreter) turns language into structured requirements | AI proposes |
| Confirm | Shopper edits and confirms every essential field | Shopper |
| Recommend | Catalog search; a shopping agent drafts a cart | Code + AI agent |
| **Verify** | Recomputes price, offers, budget, stock and delivery in integer cents; blocks violations; offers re-verified corrections | **Deterministic code** |
| Authorize | Shopper approves the exact charge; the server re-verifies at the moment of approval | Shopper |
| Resolve | The complaint is classified against the record, carrier and payment evidence; only eligible remedies are offered | Code + shopper |
| Improve | Confirmed outcomes cross demo thresholds and produce a proposed rule | Merchant approves |

**Core principle:** AI interprets language, deterministic code enforces rules, and humans approve. The model never approves payments, changes amounts, issues refunds or activates rules.

## What we built

- **Shopper app** (Next.js + Tailwind): storefront, product pages, AI assistant with an editable confirmation card, checkout verification with corrected options, an order confirmation page (order, payment and delivery status plus a single "verified purchase" indicator), orders, and a resolution center. Shoppers never receive record hashes, the event log or per-rule checks.
- **Merchant console**: a dashboard computed from persisted events, rule review with evidence and small-sample warnings, approve/reject/pause controls, a claims list, an order audit (record hash, event chain, every verification check, full record), and a sandboxed evaluation runner. The order audit is served by `/api/admin/*`, which requires `PERKPILOT_ADMIN_TOKEN` as a bearer token in every mode.
- **Backend** (FastAPI + Pydantic): verifier, pricing/offer engine, purchase records with SHA-256 hashes and an append-only hash-chained event log, claim classifier, policy-constrained remedy engine, exchange/refund simulator, learning engine, metrics and evaluation.
- **Persistence**: MongoDB when `MONGODB_URI` is set; otherwise a local JSON-file store that survives refreshes and restarts. The UI shows which one is active.

### How Gemini is used

- Parsing shopping requests into a schema-constrained JSON object (`responseSchema`), validated by Pydantic. Relative dates such as "Friday" are resolved by code against the demo clock in the store's timezone.
- Interpreting complaints into structured issues.
- Drafting the wording of proposed rules, checked so that both sizes appear and no new numbers are introduced.

Models: `gemini-3.5-flash` with low thinking (about 1.5s per call), with `gemini-3.5-flash-lite` as a backup when the primary is overloaded. On a 429/5xx the call retries once and then tries the backup, all within a 10-second budget. If no key is set, Gemini stays unavailable, or its output fails validation, a deterministic rule-based interpreter runs instead. The UI says so ("fallback interpreter"); it is never presented as live AI. The shopper-facing guidance sentence uses fixed wording; Gemini drafts only the merchant summary, which must restate the real counts and contain no other numbers.

### How deterministic verification works (`backend/app/verifier.py`, `pricing.py`)

Fixture order of operations: merchandise → flat demo tax (on the pre-coupon price) → shipping → coupons subtracted from merchandise → **charge**. Card-linked offers are evaluated against their declared basis (`final_charge`). They are *later statement credits* and never reduce the charge.

The checks are: product type, brand, color, size, stock, quantity, payment method, coupon validity/expiry/eligibility/minimum spend, offer stacking, card-offer enrollment and threshold, claimed charge, claimed savings, max charge, delivery deadline, plus non-blocking merchant-approved fit guidance. Each failure returns the rule, observed value, required value, explanation and corrected options that are themselves re-verified.

### How Visa concepts are represented

- **Verified payment intent:** the shopper approves the merchant, product/variant, full price breakdown, final charge, simulated payment method and delivery promise. That approval is stored as an event that references the quote hash.
- **Payment controls:** the max-charge limit is enforced against the actual charge. A future statement credit can never justify exceeding it. Card-linked offer eligibility depends on enrollment and on the post-discount charge.
- **Payment method:** "Visa Demo Card •••• 4242" is a labeled fixture, not a credential or token. No card numbers, CVVs or bank data are collected.
- **Post-purchase:** simulated refund to the original payment method, versus a clearly labeled *push-to-card payout concept*.

PerkPilot does not implement, integrate with, or claim certification from Visa Intelligent Commerce or any Visa API. These are application-layer demonstrations of the same trust ideas.

## What is simulated

Store, catalog, shoppers, prices, tax, coupons, card-linked offer, payment authorization and settlement, refunds, store credit, push-to-card, carrier events and the clock. Seeded history (26 orders, 3 fit claims, 5 edge-case orders for shopper Riley) is synthetic and labeled as seeded everywhere it appears.

---

## Run it

Requirements: **Python 3.10+**, **Node 18+** and Git. Works on macOS, Linux and Windows.

### Quick start (one command)

```bash
git clone https://github.com/ABasa08/UW-DevHack.git && cd UW-DevHack
./start.sh          # on Windows, run it from Git Bash
```

`start.sh` creates `backend/.venv`, installs the Python and npm dependencies, and starts the API on port 8000 and the app on port 3000. Open http://localhost:3000. API docs are at http://localhost:8000/docs.

### Manual setup

Backend (terminal 1):

```bash
cd backend
python -m venv .venv
# macOS/Linux: source .venv/bin/activate      Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

Frontend (terminal 2):

```bash
cd frontend
npm install
npm run dev
```

The frontend proxies `/api/*` to `http://127.0.0.1:8000`. Set `PERKPILOT_API_URL` to point it at a different backend.

### Configuration (optional)

Copy `backend/.env.example` to `backend/.env`. Every value is optional; the file documents each one.

| Variable | Effect |
|---|---|
| `GEMINI_API_KEY` | Live Gemini interpretation. Without it the disclosed fallback interpreter runs. |
| `MONGODB_URI` | MongoDB persistence. Without it state is saved to `backend/data/demo_state.json`. |
| `PERKPILOT_ADMIN_TOKEN` | Unlocks the merchant console's order audit (`/api/admin/*`) in every mode; the audit stays closed without it. |
| `PERKPILOT_MODE=live` | Real clock, no seed data, simulated payment/refund/shipping routes removed, and simulated orders are never shown to shoppers. Requires `MONGODB_URI` and `PERKPILOT_ADMIN_TOKEN` (bearer token for `/api/merchant/*` and `/api/eval/*`). |
| `SHOPIFY_*` | Shopify catalog sync. See the next section. |

### Tests

```bash
cd backend
python -m pytest -q     # use the venv's python: .venv/bin/python or .venv\Scripts\python
cd ../frontend && npm run typecheck && npm run build
```

The backend has 34 tests. They include all 16 required acceptance tests from the spec: correct product, wrong variant, budget, coupon-vs-card-offer threshold, expired promo, missed deadline, approval required, changed amount after approval, wrong delivery, ambiguous fit, exchange verification, duplicate remedy, rule approval gating, learning loop, persistence across restarts, and demo reset. They also cover tamper detection, shopper isolation, missing-field handling, the sandboxed evaluation, live-mode isolation, and the Shopify adapter (pagination, currency guard, both auth methods, mapping/skip/archive, fail-closed verification, and the checkout handoff). Tests force the deterministic interpreter and mock Shopify's HTTP API, so they never call a live model or store.

---

## Add PerkPilot to an existing Shopify store

PerkPilot reads your Shopify catalog, and a floating assistant on your storefront helps shoppers pick the right product. Shopify still owns the cart, checkout, payments and orders. When PerkPilot finds a match, the shopper continues to **your normal Shopify checkout**. PerkPilot verifies everything it can know (product type, brand, color, size, stock, and item price against the shopper's budget). It never claims to verify shipping or tax, because only Shopify's checkout calculates them. For those products it shows "Continue to store checkout" in place of its own simulated checkout.

You need a Shopify store with an Online Store theme and USD pricing. Any product type works. Categories come from each product's **Product type** (blank becomes `other`), and the assistant only offers categories, colors and sizes your catalog actually has. A **Size** option can be numeric (`10.5`) or not (`S`, `XL`, `32x30`), and a **Color** option is optional. Products without either (e.g. a tote bag) sync as one size. Prices are per variant. Only products with no variants are skipped, and they're listed in the sync output.

### 1. Create a Shopify app with read access

1. In the [Shopify Dev Dashboard](https://dev.shopify.com/dashboard), create an app in the same organization as your store.
2. Configure Admin API access scopes `read_products` and `read_inventory`, release a version, and install the app on your store.
3. Copy the app's **Client ID** and **Client secret** from its settings.

Already have a legacy custom app with an Admin API token (`shpat_...`)? You can use that token instead.

### 2. Configure and sync the catalog

In `backend/.env`:

```bash
SHOPIFY_SHOP=your-store.myshopify.com
SHOPIFY_CLIENT_ID=...
SHOPIFY_CLIENT_SECRET=...
# or: SHOPIFY_ADMIN_TOKEN=shpat_...
# If your options are named differently: SHOPIFY_SIZE_OPTION=Shoe size  SHOPIFY_COLOR_OPTION=Colour
```

Then, from `backend/` with the venv active:

```bash
python -m app.shopify sync --only-shopify
# synced 12, skipped 0, archived 6
```

`--only-shopify` archives the fictional StrideLab demo catalog so shoppers see only your products. Drop the flag to keep both. The sync is a one-way, repeatable projection: re-run it whenever your catalog changes (a cron job works). Products removed from Shopify are archived, never deleted. The demo **Reset** button re-seeds the demo data, so re-run the sync after using it.

### 3. Deploy PerkPilot

Host the backend and frontend anywhere that runs Python and Node, for example Render, Railway, Fly.io or a VM:

- Backend: `pip install -r requirements.txt && python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT`, with `MONGODB_URI` set so state survives redeploys.
- Frontend: `npm install && npm run build && npm start`, with `PERKPILOT_API_URL` set to the backend's URL.

The frontend must be served over HTTPS, because Shopify storefronts won't load an `http://` iframe.

### 4. Add the assistant to your theme

1. Open **Online Store → Themes → Customize**.
2. In the **Footer** group, click **Add section → Custom Liquid**.
3. Paste the contents of [`shopify/perkpilot-embed.liquid`](shopify/perkpilot-embed.liquid) and replace `https://YOUR-PERKPILOT-FRONTEND` with your frontend URL.
4. Save. A **Shopping assistant** button now appears at the bottom-right of every page.

The button opens `https://<your-frontend>/embed`, which is the assistant without the demo chrome. You can preview that URL directly. To remove PerkPilot, delete the Custom Liquid section; nothing else in your theme changes.

### 5. Stream live store data to the merchant dashboard

The dashboard's **Shopify store** panel shows orders, paid revenue, orders awaiting fulfillment, stock, and a feed of store changes. It refreshes every 5 seconds. Shopify pushes changes by webhook to `POST /api/shopify/webhooks`, and each request must carry a valid HMAC signed with your app's client secret.

1. Add `read_orders` to your app's scopes in the Dev Dashboard, release the version and approve it in your store. Products and inventory stream without it; orders need it.
2. Set `PERKPILOT_PUBLIC_URL` in `backend/.env` to the app's public HTTPS address (your deployment, or an `ngrok http 3000` tunnel), then restart the backend.
3. Open **Merchant console → Dashboard**, enter `PERKPILOT_ADMIN_TOKEN`, and press **Connect store**. This registers the webhooks your scopes allow, backfills the last 100 orders and resyncs the catalog.

Product and inventory events trigger a catalog resync inside the API process; bursts collapse into one sync. Order events store a summary (number, totals, statuses, line items) and never store customer names, emails or addresses. If the tunnel URL changes, update `PERKPILOT_PUBLIC_URL` and press **Sync now**; stale webhook URLs are replaced.

### What the Shopify pilot does not do yet

- Shoppers in the embed use the demo shopper identity; Shopify customer accounts aren't linked yet.
- The merchant console's returns and learning features run on PerkPilot's own claims. Shopify orders show on the dashboard, but Shopify returns don't feed claims or learning yet.
- Single store, USD only.
- Variants are matched on Size and Color; a third option (e.g. Material) isn't matched, and the first variant wins.

[SHOPIFY_INTEGRATION_REPORT.md](SHOPIFY_INTEGRATION_REPORT.md) covers the full production roadmap: theme app extension, checkout enforcement, order and return webhooks, and authenticated merchant sessions.

## Evaluation (Merchant console → Evaluation)

Each run seeds an isolated in-memory copy of the store, so live demo data is never touched. It reports exact counts:

| Metric | Latest result |
|---|---|
| Checkout violation detection | 10/10 invalid carts blocked with the right rule |
| False rejections | 0/5 valid carts |
| Request parsing (field accuracy) | 26/26 with live Gemini 3.5 Flash, and 26/26 with the fallback interpreter (1 run each) |
| Claim classification | 9/9, including 3/3 appropriate abstentions |
| Remedy policy compliance | 5/5 (duplicate acceptance, overcharge amount, ambiguous → review only, refund always available, sold-out exchange blocked) |
| End-to-end workflow | 12/12 steps |
| Controlled comparison (held-out requests) | baseline 2/5 vs. approved rule 5/5 matching labels, constraints preserved 5/5 |

These numbers describe program behavior on synthetic fixtures. They do not show real-world return reduction.

---

## Judge demo (about 4 minutes)

See [DEMO.md](DEMO.md) for the click-by-click script. In short:

1. **Catch the mistake:** the agent stacks STRIDE15 with a $10 card credit ("effective $100"). The verifier blocks it because the $110 charge is under the $120 threshold, and shows Option A ($110, passes) next to Option B ($125, over budget).
2. **Confirm:** approve the $110 simulated charge. The Verified Purchase Record is saved with its hash.
3. **Fix:** advance to delivery and report "These shoes feel too tight." The order matches what was delivered, and 2 prior confirmed fit exchanges exist, so it is a *suspected* fit issue. Exchange to 10.5 (re-verified, inventory reserved), deliver it, and confirm the fit.
4. **Learn:** that third confirmed outcome proposes a rule. The merchant reviews 3 outcomes among 21 orders with a small-sample warning and approves it.
5. **Prevent:** switch to Sam and make the same request. Guidance appears ("review size 10.5?"). Toggle it off and on to compare. Sam chooses 10.5; nothing changes silently.
6. **Dashboard:** blocked mistake, saved purchase, exchange, approved rule, changed recommendation.

Use **Reset** in the demo bar to replay. Use **Next delivery →** / **+1 day** to move the simulated clock.

## Known limitations

- Single merchant, footwear only, US sizes, USD. Tax is a flat per-product demo fixture.
- Learning thresholds (≥20 fulfilled orders, ≥3 confirmed outcomes, ≥2/3 agreeing direction) are illustrative, not statistically validated.
- The "shopping agent" mistake is produced by a deliberately naive planner that evaluates card-offer thresholds before discounts. That makes the demo repeatable; the verifier treats it like any untrusted proposal.
- No authentication: the persona switcher stands in for login. Endpoints still enforce shopper ownership of purchases and claims.
- The record hash detects changes relative to a trusted copy of the hash; it does not make storage tamper-proof by itself.
- Remedy "merchant value" ranking uses labeled demo assumptions. It only orders eligible options, and a cash refund always outranks equivalent store credit.

## Future work

Pilot with a real merchant's order and returns data, measure real return-rate and support-cost changes, add authenticated sessions and multi-merchant scoping, strengthen policy configuration, and investigate approved payment-network or PSP integrations for credentials, authorization and commerce signals where technically and commercially feasible.

## Repository layout

```
backend/app/
  main.py         FastAPI routes (spec §12 contracts plus a few read endpoints)
  models.py       Pydantic request/requirement schemas
  ai.py           Gemini calls with schema validation, and the disclosed fallback interpreter
  pricing.py      Integer-cent price breakdown, coupons, card-linked offers, delivery estimates
  verifier.py     Checkout rules, corrected options, the naive agent planner
  shopping.py     Intent parse/confirm, recommendations, quotes, checkout confirmation
  records.py      Verified Purchase Records, hashes, simulated shipments
  resolution.py   Claim classification, remedies, exchange/refund simulation, outcomes
  learning.py     Return-to-rule proposals, approve/reject/pause
  metrics.py      Dashboard numbers from persisted events
  evaluation.py   Sandboxed labeled-fixture evaluation runs
  fixtures.py     Seeded StrideLab data (deterministic IDs)
  store.py        MongoDB or local JSON-file persistence
  shopify.py      Read-only Shopify catalog sync (python -m app.shopify sync)
backend/tests/    Acceptance, live-mode and Shopify adapter tests
frontend/app/     Next.js App Router pages (shopper, /merchant, and /embed for storefronts)
shopify/          Theme snippet that embeds the assistant in a Shopify store
```
