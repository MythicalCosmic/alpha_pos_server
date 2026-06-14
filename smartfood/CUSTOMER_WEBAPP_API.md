# Smart Food Customer Mini App — Frontend Integration Guide

> Audience: the frontend agent building the **Smart Food customer Telegram Mini App** (the webapp opened from inside the customer Telegram bot).
> Scope: the customer-facing HTTP API under `/api/smartfood`. Operator/staff endpoints under `/api/admins/smartfood` are **not** part of this contract (see Appendix B).

---

## 1. Overview

The Smart Food API is a JSON HTTP API that powers a **Telegram Mini App** (a webapp launched from the customer bot). The customer browses a menu, builds a cart, redeems loyalty points, places an order, and tracks it — all against a single store.

- **Example host:** `https://pos.78.111.90.65.nip.io`
- **Path prefix (all customer routes):** `/api/smartfood`
- **Full example URL:** `https://pos.78.111.90.65.nip.io/api/smartfood/config`
- **No trailing slash** on any route (e.g. `/api/smartfood/me`, `/api/smartfood/auth`).

**High-level request flow:**

1. The Mini App boots inside Telegram and reads `window.Telegram.WebApp.initData` (a signed querystring).
2. It POSTs that `initData` once to `POST /api/smartfood/auth` and receives a **Bearer session token** (valid ~24h by default).
3. Every other call sends `Authorization: Bearer <token>` (initData is **not** resent).
4. The app loads `/config` (store on/off, delivery params, feature flags), then catalog, builds a cart, quotes it server-side, checks out, and polls for tracking.

Two things make this API unusual and you must handle both:

- **There is no server-side cart.** The cart lives entirely in the client. `POST /cart/quote` is a stateless reprice/validate call; you send the full items array every time.
- **"Closed" is not an error.** When the store/bot is off there is no active cashier, gated endpoints return **HTTP 200** with `{"success": false, "closed": true, "reason": "..."}`. You must check `closed` before `success` and render a closed screen — never treat it as a network/error failure.

> **First-boot default: the store is closed.** `BotConfig.enabled` defaults to **false** on a fresh install (models.py:44). The very first `/config` the Mini App ever sees returns `enabled: false`, and catalog/quote return `bot_off` (200), until an operator turns the bot ON via the admin `/config/enable` toggle. Render the closed screen by default and only open ordering once `config.enabled === true`.

---

## 2. Quickstart

The minimum to make an authenticated call:

```js
// 1) Inside the Telegram Mini App, grab the signed launch payload.
const initData = window.Telegram.WebApp.initData; // raw querystring, do NOT modify

// 2) Exchange it for a session token (one time per launch).
const loginRes = await fetch('https://pos.78.111.90.65.nip.io/api/smartfood/auth', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ init_data: initData }),
}).then(r => r.json());

const token = loginRes.data.token; // 64-hex bearer

// 3) Use the token on every subsequent call.
const headers = { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };

const config = await fetch('https://pos.78.111.90.65.nip.io/api/smartfood/config', { headers })
  .then(r => r.json());

const categories = await fetch('https://pos.78.111.90.65.nip.io/api/smartfood/catalog/categories', { headers })
  .then(r => r.json());
```

> **Do not assert on a specific success status code for login.** `POST /auth` returns **HTTP 200** with message `"Success"` (not 201/"Created"). Branch on `res.ok` / `env.success`, never on `status === 201`.

### Copy-paste API client wrapper

```js
// smartfood-api.js
const BASE = 'https://pos.78.111.90.65.nip.io/api/smartfood';

let _token = null;
export function setToken(t) { _token = t; }
export function getToken() { return _token; }

// Thrown for real failures (4xx/5xx envelopes). NOT thrown for "closed" or
// "code" conflicts — those are returned so the UI can branch on them.
export class ApiError extends Error {
  constructor(envelope, httpStatus) {
    // NOTE: framework-level errors (e.g. 405 method-not-allowed, 5xx) come from a
    // DIFFERENT envelope: {status, status_code, success:false, data, meta} — there is
    // NO top-level `message` on those, so `message` falls back to 'Request failed'.
    super(envelope?.message || envelope?.status || 'Request failed');
    this.envelope = envelope;        // full server body
    this.httpStatus = httpStatus;    // HTTP status code
    this.errors = envelope?.errors;  // field errors (422), if any
    this.code = envelope?.code;      // cart/order conflict code, if any
  }
}

async function request(method, path, { body, query } = {}) {
  let url = BASE + path;
  if (query) {
    const qs = new URLSearchParams(
      Object.entries(query).filter(([, v]) => v !== undefined && v !== null)
    ).toString();
    if (qs) url += '?' + qs;
  }

  const headers = { 'Content-Type': 'application/json' };
  if (_token) headers['Authorization'] = `Bearer ${_token}`;

  const res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  let env;
  try { env = await res.json(); } catch { env = { success: false, message: 'Bad response' }; }

  // GATING: store/bot closed or no cashier. HTTP 200, success:false, closed:true.
  if (env && env.closed === true) {
    return { closed: true, reason: env.reason, _http: res.status };
  }

  // CART/ORDER CONFLICTS: flat body with a top-level `code` (HTTP 409/422).
  if (env && env.success === false && env.code) {
    return { conflict: true, code: env.code, message: env.message, _http: res.status };
  }

  if (!res.ok || env.success === false) {
    throw new ApiError(env, res.status);
  }

  return env.data; // success -> unwrap envelope; may be undefined when data omitted
}

export const api = {
  // auth
  login:  (initData) => request('POST', '/auth', { body: { init_data: initData } }),
  logout: ()         => request('POST', '/auth/logout'),

  // profile + config
  me:        ()      => request('GET', '/me'),
  updateMe:  (patch) => request('PATCH', '/me', { body: patch }),
  config:    ()      => request('GET', '/config'),

  // catalog
  categories:    (lang)              => request('GET', '/catalog/categories', { query: { lang } }),
  products:      (q)                 => request('GET', '/catalog/products', { query: q }),
  product:       (id, lang)          => request('GET', `/catalog/products/${id}`, { query: { lang } }),

  // cart + orders
  quote:         (body)             => request('POST', '/cart/quote', { body }),
  createOrder:   (body)             => request('POST', '/orders', { body }),
  listOrders:    (status)           => request('GET', '/orders', { query: { status } }),
  order:         (id)               => request('GET', `/orders/${id}`),
  trackOrder:    (id)               => request('GET', `/orders/${id}/track`),
  cancelOrder:   (id)               => request('POST', `/orders/${id}/cancel`),

  // addresses + geo
  addresses:        ()              => request('GET', '/addresses'),
  createAddress:    (body)          => request('POST', '/addresses', { body }),
  updateAddress:    (id, body)      => request('PUT', `/addresses/${id}`, { body }),
  deleteAddress:    (id)            => request('DELETE', `/addresses/${id}`),
  setDefaultAddr:   (id)            => request('PUT', `/addresses/${id}/default`),
  geoReverse:       (lat, lng, lang)=> request('GET', '/geo/reverse', { query: { lat, lng, lang } }),
  geoForward:       (q, lang, limit)=> request('GET', '/geo/forward', { query: { q, lang, limit } }),

  // loyalty + support
  loyalty:          ()              => request('GET', '/loyalty'),
  support:          ()              => request('GET', '/support'),
  tickets:          ()              => request('GET', '/support/tickets'),
  createTicket:     (body)          => request('POST', '/support/tickets', { body }),
  addTicketMessage: (id, text)      => request('POST', `/support/tickets/${id}/messages`, { body: { text } }),
};
```

Usage:

```js
import { api, setToken } from './smartfood-api.js';

const { token } = await api.login(window.Telegram.WebApp.initData);
setToken(token);

const cfg = await api.config();
if (!cfg.enabled) { /* render closed screen */ }

const cats = await api.categories(); // -> { items: [...] }
```

---

## 3. Conventions

### Response envelope

Every response is a JSON object with `success` (bool) and usually `message` (string).

**Success (HTTP 200):**
```json
{ "success": true, "message": "Success", "data": { } }
```
The `data` key is **omitted** when there is no payload (e.g. deletes, set-default). This is the envelope used by `GET` reads **and by `POST /auth` login** (login is 200/"Success", not 201).

**Created (HTTP 201):**
```json
{ "success": true, "message": "Created", "data": { } }
```
Only three customer endpoints return 201/"Created": `POST /addresses`, `POST /support/tickets`, and `POST /orders` (order create). Everything else — including `POST /auth` — returns 200/"Success".

**Standard (JSON-envelope) errors** — these come from the smartfood views via `ServiceResponse` and always have a top-level `message`:
| Variant | HTTP | Shape |
|---|---|---|
| unauthorized | 401 | `{"success": false, "message": "<msg>"}` |
| forbidden | 403 | `{"success": false, "message": "<msg>"}` |
| not_found | 404 | `{"success": false, "message": "<msg>"}` |
| validation_error | 422 | `{"success": false, "message": "Validation failed", "errors": {"<field>": "<msg>"}}` |
| error | 400 | `{"success": false, "message": "<msg>"}` (optionally `errors`) |
| bad JSON body | 400 | `{"success": false, "message": "Invalid JSON" \| "Expected JSON object"}` |

**Framework-level errors are a DIFFERENT shape.** A `405 Method Not Allowed` (sent by Django's `require_GET`/`require_POST`/`require_http_methods` when you use the wrong method), and any other non-`JsonResponse` 4xx/5xx, are re-wrapped by `JSONOnlyMiddleware` into:
```json
{
  "status": "Method Not Allowed",
  "status_code": 405,
  "success": false,
  "data": null,
  "meta": { "path": "/api/smartfood/config", "method": "POST", "timestamp": "2026-06-14T10:05:00+00:00" }
}
```
There is **no** top-level `message`, no `errors`, no `code` on these. The `status` string follows standard reason phrases (`"Bad Request"`, `"Unauthorized"`, `"Forbidden"`, `"Not Found"`, `"Conflict"`, `"Unprocessable Entity"`, `"Internal Server Error"`, …). The §2 wrapper's `ApiError` reads `message` first and falls back to `status`, so handle these defensively — but in practice you avoid 405 entirely by calling each route with its documented method. (force_json_middleware.py:38-63, 113-129)

**Cart/Order conflict envelope** (used by `cart/quote`, `orders` create, `cancel`) — note the top-level `code`:
```json
{ "success": false, "code": "item_unavailable", "message": "Cheeseburger is sold out" }
```
HTTP status is `409` or `422` depending on the code (see §5).

**Closed/gating envelope** (HTTP **200**, render a closed screen — see §5):
```json
{ "success": false, "closed": true, "reason": "bot_off" }
```
There is **no** `data`, `message`, or `code` on a closed payload — only `success`, `closed`, `reason`.

> Branch order in client code: check `closed` first, then `code`, then `success`. (The wrapper in §2 does this.)

### Money format

All monetary values are **integers in Uzbek so'm (UZS)** — never decimals, never strings. UZS has no minor unit. Example: `25000` means 25 000 so'm.

```js
// Format integer so'm for display, e.g. 25000 -> "25 000 so'm"
export function formatUZS(amount) {
  const n = Number(amount) || 0;
  return n.toLocaleString('ru-RU').replace(/,/g, ' ') + " so'm";
}
```

The currency code string `"UZS"` is returned by `/config` (`data.currency`) and by `/cart/quote` (`data.currency`).

### Date/time format

Timestamps are ISO 8601 strings via `.isoformat()`, e.g. `"2026-06-14T10:05:00+00:00"`, or `null` when absent (e.g. `dispatched_at` before dispatch). Parse with `new Date(value)`.

### Pagination

**There is no pagination** on any customer list endpoint. Orders, addresses, loyalty history, and tickets return the full set under `data.items` (loyalty history under `data.history`). The only `limit` anywhere is on `GET /geo/forward` (`limit`, clamped to 1..20, default 5).

### i18n / language

Three languages: `uz`, `ru`, `en`. Language is resolved per request as: `?lang=` query param → else the customer's stored `language` → else `uz`.

- Catalog text fields come in **two forms**: a resolved single string under the bare key (`name`, `description`) for the active language, **and** a full map under the plural key (`names`, `descriptions`) = `{"uz": ..., "ru": ..., "en": ...}`.
- **Exception:** toppings and topping-groups return only the resolved single `name` (no `names` map).
- Resolution/fallback order for the resolved string: `[lang, uz, ru, en]`, then the POS base value.

---

## 4. Authentication

### The handshake (two-step, not initData-per-request)

**Step 1 — Login.** POST the raw Telegram `initData` querystring in the JSON **body** field `init_data` (it is NOT a header):

```
POST /api/smartfood/auth
Content-Type: application/json

{ "init_data": "user=%7B...%7D&auth_date=1718352000&hash=abc..." }
```

`init_data` must be the **verbatim** `window.Telegram.WebApp.initData` string — do not re-encode or reorder it.

**What the server validates** (`verify_init_data`):
- Parses the querystring (keeping blank values) and pops `hash`.
- Builds `data_check_string` from the remaining fields sorted by key, joined by `\n` as `k=v`.
- `secret_key = HMAC_SHA256(key="WebAppData", msg=CUSTOMER_BOT_TOKEN).digest()`.
- Valid iff `HMAC_SHA256(secret_key, data_check_string).hexdigest()` matches the received `hash` (constant-time compare).
- **Freshness/replay:** `auth_date` must be present and `> 0`, and `(now - auth_date)` must be `<= SMARTFOOD_INITDATA_MAX_AGE` (**default 3600s / 1 hour**). Missing/zero/stale `auth_date` fails.
- On success, the parsed Telegram `user` object (must contain `id`) is used to upsert the `Customer` by `telegram_id`.

**Step 2 — Authenticated calls.** Login returns a raw bearer token (only its SHA-256 digest is stored server-side). Send it on every other endpoint via:

```
Authorization: Bearer <token>
```

…or, alternatively, the cookie `session_key` (the server checks the cookie first, then the `Authorization` header). **initData is not resent.**

### Freshness window vs session lifetime

These are two different knobs — do not conflate them:
- `SMARTFOOD_INITDATA_MAX_AGE` (default **3600s / 1h**) — how long an `initData` payload stays valid for **login only**.
- `SMARTFOOD_AUTH_TTL` (default **86400s / 24h**) — the **issued session** lifetime. Returned as `expires_in` from `/auth`.

If the session expires (or `me`/any call returns 401 `Invalid or expired session`), re-run login with a fresh `window.Telegram.WebApp.initData`.

### Worked example

Request (note this example carries both `first_name` and `last_name` so the returned `name` is a full name — see the note below):
```json
POST /api/smartfood/auth
{ "init_data": "user=%7B%22id%22%3A99887766%2C%22first_name%22%3A%22Ali%22%2C%22last_name%22%3A%22Valiyev%22%7D&auth_date=1718352000&hash=abc123..." }
```

Success (**HTTP 200**, message **"Success"**):
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "token": "<64-hex raw bearer>",
    "token_type": "Bearer",
    "expires_in": 86400,
    "is_new": true,
    "customer": {
      "id": 12,
      "telegram_id": 99887766,
      "name": "Ali Valiyev",
      "phone": "+998901234567",
      "language": "uz",
      "photo_url": "https://...",
      "loyalty": { "points": 0 }
    }
  }
}
```
- `is_new` is `true` when the `Customer` row was just created (use it to trigger onboarding).
- `customer.name` is `("first_name last_name").strip()`, falling back to the Telegram username, then to the stringified `telegram_id`. So a first-time login whose `initData` user only has `first_name: "Ali"` returns `name: "Ali"` — the full `"Ali Valiyev"` above only appears because the request carries a `last_name`. Treat the name as best-effort.

### Auth failure responses

POST /auth (login):
| Condition | HTTP | Body |
|---|---|---|
| HMAC invalid, OR `auth_date` missing/zero/stale, OR `user` missing/no `id` | 401 | `{"success": false, "message": "Invalid Telegram init data"}` |
| Customer blocked at login | 403 | `{"success": false, "message": "Account blocked"}` |
| Bad request body | 400 | `{"success": false, "message": "Invalid JSON" \| "Expected JSON object"}` |

> **403 vs 401 at login:** the `403 "Account blocked"` is only reachable when the `initData` HMAC is **valid** and the matched `Customer.is_blocked` is true. Any HMAC/`auth_date`/`user` failure returns the single `401 "Invalid Telegram init data"` — login never reveals whether a given Telegram id is blocked unless the request was genuinely signed by that user.

On **authenticated** endpoints, `@customer_required` returns these literal bodies:
| Condition | HTTP | Body |
|---|---|---|
| No token | 401 | `{"success": false, "message": "Authentication required"}` |
| Token unknown/expired | 401 | `{"success": false, "message": "Invalid or expired session"}` |
| Customer blocked | 403 | `{"success": false, "message": "Account blocked"}` |

---

## 5. Edge / gating states the UI MUST handle

These are the states that block ordering. **Closed states are HTTP 200, not errors** — branch on `closed`/`code`, not on the HTTP status.

| State | Where it's signalled | Exact server signal | UI should show |
|---|---|---|---|
| **Store/bot closed** (operator turned bot OFF, or it has never been enabled; `BotConfig.enabled == false`, default on fresh install) | `GET /catalog/categories`, `GET /catalog/products`, `GET /catalog/products/:id`, `POST /cart/quote`, `POST /orders`. Also surfaced as `config.enabled === false`. | HTTP 200 `{"success": false, "closed": true, "reason": "bot_off"}` | "Store is currently closed" screen; hide ordering. Note: the gating decorator runs **before** auth on catalog/quote, so even an unauthenticated/expired caller gets `bot_off` (200) rather than 401. |
| **No active cashier** (bot ON but zero on-duty cashiers) | **Only** `POST /orders` (order creation). Browsing & quote are NOT cashier-gated. | HTTP 200 `{"success": false, "closed": true, "reason": "no_cashier"}` | "We can't take orders right now" at checkout. Let the user keep browsing/quoting. |
| **Category disabled / sold-out item (browse)** | Catalog list/detail filtering | Disabled categories & sold-out products are **silently absent** from `items` (still `success: true`, 200). A hidden/sold-out/disabled-category product detail → **404** `{"success": false, "message": "Product not found"}`. | Render only what's returned. On a 404 for a product the user had cached, show "no longer available" and refresh the menu. |
| **Item unavailable (quote/submit re-validation)** | `POST /cart/quote`, `POST /orders` | `409` `{"success": false, "code": "item_unavailable", "message": "<...>"}`. The message is one of these **exact** strings: `"Product is not available"`, `"<name> is sold out"`, `"Selected size is invalid"`, `"Selected size is unavailable"`, `"A selected topping is invalid"`, `"A selected topping is sold out"`. | Mark the offending line, prompt to remove/re-pick, re-quote. **Branch on `code === "item_unavailable"`, not on the message text** (the strings are display copy and may change). |
| **Empty cart** | `cart/quote`, `orders` | `422` code `empty_cart` ("Cart is empty") | Disable checkout. |
| **Invalid quantity** | `cart/quote`, `orders` | `422` code `invalid_quantity` ("Quantity must be greater than 0") | See note below — send clean positive ints. |
| **Topping group rules** | `cart/quote`, `orders` | `422` `topping_required` ("Choose at least N for `<group>`"), `topping_min` (same text), `topping_max` ("Choose at most N for `<group>`") | Enforce min/max selection in the configurator. |
| **Below minimum order** | `cart/quote`, `orders` | `422` code `min_order` ("Minimum order is `<amount>`") | Show "add `X` more to reach the minimum". |
| **Delivery without address** | `POST /orders` (order_type DELIVERY) | `422` `{"success": false, "message": "A delivery address is required", "errors": {"address_id": "required"}}` | Force address selection before checkout. |
| **Address not owned/missing** | `POST /orders` | `404` `{"success": false, "message": "Address not found"}` | Refresh address list. |
| **Cancel non-PENDING order** | `POST /orders/:id/cancel` | `409` `{"success": false, "code": "cannot_cancel", "message": "Only pending orders can be canceled"}` | Hide/disable the cancel button once status ≠ PENDING. |

> **Quantity coercion gotcha:** `quantity` is parsed server-side as `int(item.get('quantity', 1))` inside a try/except. So: an **omitted** quantity defaults to **1** (no error); a fractional value is **truncated** (`2.9 → 2`, accepted); a non-numeric/garbage value coerces to `0` and raises `invalid_quantity` (422); and any value `<= 0` raises `invalid_quantity` (422). It does **not** use the stricter `coerce_quantity` helper, so do not rely on the server to reject fractional quantities — always send clean positive integers from the client.

> There is **no** separate "store hours / selling stopped" flag. Store-level open/closed is purely `BotConfig.enabled` (`bot_off`). "Selling stopped" is expressed per item/category (filtering + `item_unavailable`).

---

## 6. Endpoint Reference

All paths are relative to `https://pos.78.111.90.65.nip.io/api/smartfood`. Unless stated, every endpoint requires `Authorization: Bearer <token>` and can return the shared auth errors from §4 (401/403). Calling any route with the wrong HTTP method returns a framework-level `405` in the `{status, status_code, success, data, meta}` shape (see §3), not a `{success, message}` envelope.

### 6.1 Config

#### `GET /config`
- **Auth:** Bearer required. **Not** gated by store-open — returns even when the bot is OFF (read `data.enabled` to detect closed).
- **Purpose:** Bootstrap payload the Mini App reads on launch: store on/off, delivery params, currency, service area, languages, feature flags, support contacts.
- **Request:** GET only, no body, no query params.
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "currency": "UZS",
    "enabled": true,
    "delivery_fee": 15000,
    "free_delivery_threshold": 150000,
    "min_order_amount": 30000,
    "default_tip_options": [0, 5000, 10000, 20000],
    "supported_languages": ["uz", "ru", "en"],
    "default_language": "uz",
    "service_area": {
      "city": "Tashkent",
      "center": { "lat": 41.2995, "lng": 69.2401 },
      "polygon": [[41.35, 69.18], [41.35, 69.32], [41.24, 69.32], [41.24, 69.18]]
    },
    "feature_flags": { "loyalty": true, "card_payments": false, "scheduled_delivery": false },
    "support": { "phone": "+998901234567", "telegram": "@smartfood_support", "email": "help@smartfood.uz" }
  }
}
```
- **Notes:** `enabled` is the master store ON/OFF (no working-hours/schedule field exists) and **defaults to `false` on a new install** — expect `enabled: false` on first boot until an operator turns the bot on. `card_payments` and `scheduled_delivery` are hardcoded `false` (cash-only launch). The loyalty **rate** is not here — use `GET /loyalty`. No store name/branding/logo, timezone, tax/VAT, or per-zone fees are returned.
- **Errors:** 401/403 only (plus 405 on wrong method).

---

### 6.2 Catalog

> All three catalog endpoints are gated by store-open: when the bot is OFF they return **HTTP 200** `{"success": false, "closed": true, "reason": "bot_off"}` (checked before auth). A valid customer session is still required to actually browse — there is no anonymous catalog. Sold-out / unpublished / disabled-category rows are **filtered out server-side**, never flagged.

#### `GET /catalog/categories`
- **Auth:** Bearer required; store-open gated.
- **Purpose:** Published + in-stock menu categories, in display order.
- **Query:** `lang` (optional, `uz|ru|en`). No filters, no search, no pagination.
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "items": [
      {
        "id": 7,
        "name": "Burgerlar",
        "names": { "uz": "Burgerlar", "ru": "Бургеры", "en": "Burgers" },
        "sort": 1,
        "image_url": "https://cdn.example.com/cat/burgers.jpg"
      }
    ]
  }
}
```
- **Notes:** `id` is the POS category id. `image_url` may be `""`. Disabled/unpublished categories are simply absent.
- **Errors:** closed `bot_off` (200); 401/403; 405 if not GET (framework-shape).

#### `GET /catalog/products`
- **Auth:** Bearer required; store-open gated.
- **Purpose:** Published + in-stock products (list shape), filterable.
- **Query (all optional):** `category_id` (POS category id), `tag` (exact match, e.g. `bestseller|new|spicy`), `q` (case-insensitive search across product name + name_uz/ru/en), `lang`. No pagination.
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "items": [
      {
        "id": 142,
        "category_id": 7,
        "name": "Chizburger",
        "names": { "uz": "Chizburger", "ru": "Чизбургер", "en": "Cheeseburger" },
        "price": 32000,
        "image_url": "https://cdn.example.com/prod/cheeseburger.jpg",
        "tag": "bestseller",
        "kcal": 540,
        "available": true
      }
    ]
  }
}
```
- **Notes:** List items omit `sizes`/`topping_groups`/`description`. `price` is integer so'm, live from POS. `kcal` may be `null`. `available` is effectively always `true` here (the query already requires in-stock). Unknown/empty `category_id` or no matches → `200` with `items: []` (not 404).
- **Errors:** closed `bot_off` (200); 401/403; 405.

#### `GET /catalog/products/:product_id`
- **Auth:** Bearer required; store-open gated.
- **Purpose:** Full product detail incl. description, sizes, and topping groups (for the configurator).
- **Path:** `product_id` (POS Product id). **Query:** `lang`.
- **Success (200):** (product object is in `data` **directly**, not under `items`)
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "id": 142,
    "category_id": 7,
    "name": "Cheeseburger",
    "names": { "uz": "Chizburger", "ru": "Чизбургер", "en": "Cheeseburger" },
    "price": 32000,
    "image_url": "https://cdn.example.com/prod/cheeseburger.jpg",
    "tag": "bestseller",
    "kcal": 540,
    "available": true,
    "description": "Juicy beef patty with cheddar.",
    "descriptions": { "uz": "...", "ru": "...", "en": "Juicy beef patty with cheddar." },
    "sizes": [
      { "id": 20, "name": "Large", "names": { "uz": "Katta", "ru": "Большой", "en": "Large" }, "price_delta": 8000, "is_default": false }
    ],
    "topping_groups": [
      {
        "id": 5,
        "name": "Sauces",
        "required": false,
        "min_select": 0,
        "max_select": 2,
        "toppings": [
          { "id": 31, "name": "Ketchup", "price": 0 },
          { "id": 32, "name": "BBQ", "price": 2000 }
        ]
      }
    ]
  }
}
```
- **Notes:** Only sizes/toppings with `is_selling = true` are returned. `max_select: 0` means **unlimited**. Toppings/groups have a resolved `name` only (no `names` map). `price_delta` and topping `price` are integer so'm **deltas**. The **final price is computed client-side**: base `price` + chosen size `price_delta` + sum of chosen topping `price` — the server does NOT pre-sum per-variant; it recomputes authoritatively at quote/order time.
- **Errors:** `404 {"success": false, "message": "Product not found"}` if not published/selling or category disabled/deleted; closed `bot_off` (200); 401/403; 405.

---

### 6.3 Cart

> There is **no persistent cart** and **no** add/update/remove/clear endpoints. The cart lives in the client. The only cart endpoint is the stateless quote below; send the full `items` array every time.

#### `POST /cart/quote`
- **Auth:** Bearer required; store-open gated (`bot_off`).
- **Purpose:** Authoritative reprice + validation. Recomputes every unit price from live POS price + size delta + topping prices, validates availability and group rules, applies delivery fee/free threshold, loyalty discount, and tip. Client prices are **ignored**.
- **Request:**
```json
{
  "items": [
    { "product_id": 42, "size_id": 7, "topping_ids": [3, 9], "quantity": 2 }
  ],
  "order_type": "DELIVERY",
  "tip": 5000,
  "points_used": 100
}
```
  - `items` required, non-empty. Each item: `product_id` (int, required), `size_id` (int, optional), `topping_ids` (int[], optional), `quantity` (optional). IDs are integer DB ids, not UUIDs.
  - `quantity`: **optional, defaults to 1**. Must coerce to a positive int; non-numeric/garbage → 0 → `invalid_quantity` (422); fractional values are truncated (`2.9 → 2`). See the quantity-coercion note in §5. Send clean positive ints.
  - `size_id`: must be a **truthy positive int** to select a size. `0`, `null`, or omitted all mean "no size selected" — sending `0` as a sentinel silently yields the base product (no size), **not** a validation error.
  - `order_type`: `"DELIVERY"` (default) or `"PICKUP"`; PICKUP → `delivery_fee` 0.
  - `tip`: UZS int, default 0; negative coerced to 0.
  - `points_used`: loyalty points to redeem, default 0; clamped to balance; discount capped at subtotal.
  - There is **no per-item notes** field — notes are order-level only (sent on create).
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "currency": "UZS",
    "subtotal": 84000,
    "delivery_fee": 10000,
    "free_delivery_applied": false,
    "discount": 5000,
    "tip": 5000,
    "total": 94000,
    "loyalty_points_used": 100,
    "loyalty_points_earned": 8,
    "lines": [
      {
        "product_id": 42,
        "size_id": 7,
        "name": "Margherita",
        "quantity": 2,
        "unit_price": 42000,
        "line_total": 84000,
        "toppings": [ { "topping_id": 3, "name": "Extra cheese", "price": 6000 } ],
        "detail": "Large · Extra cheese"
      }
    ]
  }
}
```
- **Notes:** `free_delivery_applied` is `true` when PICKUP or `subtotal >= free_delivery_threshold`. Quote does NOT need an address. `unit_price = base price + size price_delta + sum(topping prices)`, all re-read server-side.
- **Errors:** closed `bot_off` (200). Bad JSON 400. CartError conflicts (flat `{success, code, message}`): `empty_cart` (422), `invalid_quantity` (422), `item_unavailable` (409), `topping_required`/`topping_min`/`topping_max` (422), `min_order` (422). 401/403; 405.

---

### 6.4 Orders

#### `POST /orders`  (create / checkout)
- **Auth:** Bearer required; gated by store-open **and** active-cashier (`bot_off` then `no_cashier`). The create path is authenticated first, then gated.
- **Purpose:** Place an order. Re-prices server-side (same engine as `cart/quote`), creates a `BotOrder` in **PENDING** with frozen line snapshots, reserves redeemed loyalty points. Does **not** dispatch — an operator later sends it to a cashier.
- **Request:**
```json
{
  "items": [ { "product_id": 42, "size_id": 7, "topping_ids": [3, 9], "quantity": 2 } ],
  "order_type": "DELIVERY",
  "address_id": 15,
  "phone": "+998901234567",
  "note": "Leave at door",
  "tip": 5000,
  "points_used": 100,
  "payment_method": "CASH"
}
```
  - `items`: same shape and coercion rules as `cart/quote` (quantity optional/default 1, `size_id` truthy-positive to select).
  - `order_type`: `"DELIVERY"` (default) or `"PICKUP"`.
  - `address_id`: **required when DELIVERY**; ignored for PICKUP.
  - `phone`: optional; falls back to the customer's stored phone. The final stored value is `(phone or customer.phone or '')` — if both are blank, the order's `phone` is the **empty string `""`**, never `null`.
  - `note`: optional order-level note.
  - `tip`, `points_used`: as in quote.
  - `payment_method`: `"CASH"` (default) or `"CARD"`; anything not `"CARD"` is coerced to `"CASH"` (cash-only launch).
- **Success (201):**
```json
{
  "success": true,
  "message": "Created",
  "data": {
    "id": 1234,
    "code": "SF-1234",
    "status": "PENDING",
    "order_type": "DELIVERY",
    "created_at": "2026-06-14T10:05:00+00:00",
    "phone": "+998901234567",
    "note": "Leave at door",
    "address_text": "Tashkent, Amir Temur 12, apt 4",
    "payment_method": "CASH",
    "totals": { "subtotal": 84000, "delivery_fee": 10000, "discount": 5000, "tip": 5000, "total": 94000 },
    "loyalty_points_used": 100,
    "loyalty_points_earned": 8,
    "items": [
      {
        "product_id": 42, "size_id": 7, "quantity": 2,
        "unit_price": 42000, "line_total": 84000,
        "toppings": [ { "topping_id": 3, "name": "Extra cheese", "price": 6000 } ],
        "detail": "Large · Extra cheese"
      }
    ],
    "pos_order": null,
    "dispatched_at": null,
    "reject_reason": ""
  }
}
```
- **Notes:** `points_used` is reserved/debited now; `points_earned` is credited only at dispatch. The selected address is snapshotted into `address_text`. The human-facing code is `data.code` = `"SF-<id>"`. `phone` may be `""`.
- **Errors:** closed (200) `bot_off` or `no_cashier`. All `cart/quote` CartError codes apply (flat `{success, code, message}`). DELIVERY without address → `422 {"errors": {"address_id": "required"}, "message": "A delivery address is required"}`. Address not owned → `404 "Address not found"`. Bad JSON 400. 401/403.

#### `GET /orders`  (list my orders)
- **Auth:** Bearer required; NOT gated (works when bot is off).
- **Query:** `status=active` (PENDING+DISPATCHED) | `status=history` (REJECTED+CANCELED) | omit for ALL. Any **unrecognized** value (e.g. `status=foo`) is ignored and returns ALL orders — it is not a 400.
- **Success (200):** `data.items` is an array of the full order object (same shape as create). No pagination, ordered newest-first (`-id`).
```json
{ "success": true, "message": "Success", "data": { "items": [ { "id": 1234, "code": "SF-1234", "status": "DISPATCHED", "...": "...", "pos_order": { "id": 555, "uuid": "...", "status": "PREPARING", "display_id": 27 } } ] } }
```
- **Errors:** 401/403.

#### `GET /orders/:order_id`  (order detail)
- **Auth:** Bearer required; scoped to the customer (own orders only).
- **Path:** `order_id` = integer `BotOrder` id (NOT the `SF-` code).
- **Success (200):** the full order object (same shape as create), including the `pos_order` block once dispatched.
- **Errors:** `404 {"success": false, "message": "Order not found"}` if missing/not owned. 401/403.

#### `GET /orders/:order_id/track`  (poll tracking)
- **Auth:** Bearer required; own order only; NOT gated.
- **Purpose:** Tracking. **Functionally identical to order detail** — same full object. There is **no** websocket/SSE/long-poll and no status-only endpoint; the client polls this on an interval. (A complementary Telegram chat push is also sent to the customer on dispatch/reject — best-effort, you can't toggle or read it via the API.)
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "id": 1234, "code": "SF-1234",
    "status": "DISPATCHED",
    "order_type": "DELIVERY",
    "created_at": "2026-06-14T10:05:00+00:00",
    "dispatched_at": "2026-06-14T10:07:00+00:00",
    "reject_reason": "",
    "totals": { "subtotal": 84000, "delivery_fee": 10000, "discount": 5000, "tip": 5000, "total": 94000 },
    "loyalty_points_used": 100, "loyalty_points_earned": 8,
    "items": [],
    "pos_order": { "id": 555, "uuid": "...", "status": "PREPARING", "display_id": 27 }
  }
}
```
- **Notes:** Read `data.status` for the BotOrder lifecycle and `data.pos_order.status` for kitchen progress (PREPARING/READY/…). `pos_order == null` → still PENDING / awaiting cashier. `pos_order.display_id` is the till/queue number the customer can quote at pickup.
- **Errors:** `404 "Order not found"`; 401/403.

#### `POST /orders/:order_id/cancel`
- **Auth:** Bearer required; own order only; NOT gated.
- **Purpose:** Customer cancels their own order while still **PENDING**. Refunds reserved loyalty points.
- **Request:** no body required.
- **Success (200):**
```json
{ "success": true, "message": "Success", "data": { "id": 1234, "status": "CANCELED" } }
```
- **Errors:** `404 "Order not found"`; `409 {"success": false, "code": "cannot_cancel", "message": "Only pending orders can be canceled"}` once status ≠ PENDING; 401/403.

---

### 6.5 Addresses

#### `GET /addresses`
- **Auth:** Bearer required.
- **Purpose:** List the customer's addresses (ordered default-first, then newest). No pagination.
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "items": [
      {
        "id": 12, "label": "Home",
        "line": "Toshkent sh., Chilonzor 9-kvartal, 24-uy",
        "lat": 41.285612, "lng": 69.203491,
        "city": "Tashkent", "street": "Chilonzor", "house": "24",
        "apartment": "15", "entrance": "2", "floor": "5", "intercom": "15K",
        "comment": "Call on arrival", "precision": "exact", "is_default": true
      }
    ]
  }
}
```
- **Notes:** `lat`/`lng` emitted as float or `null`. Empty → `items: []`.
- **Errors:** 401/403.

#### `POST /addresses`  (create)
- **Auth:** Bearer required.
- **Purpose:** Add an address. The first address ever, or any with `make_default: true`, becomes the default.
- **Request:** `line` is **required** (non-empty after trim). Settable fields: `label`, `line`, `lat`, `lng`, `city`, `street`, `house`, `apartment`, `entrance`, `floor`, `intercom`, `comment`, `precision`, plus `make_default` (optional). `null` values are ignored.
```json
{
  "line": "Toshkent sh., Chilonzor 9-kvartal, 24-uy",
  "label": "Home", "lat": 41.285612, "lng": 69.203491,
  "city": "Tashkent", "street": "Chilonzor", "house": "24",
  "apartment": "15", "entrance": "2", "floor": "5", "intercom": "15K",
  "comment": "Call on arrival", "precision": "exact", "make_default": false
}
```
- **Success (201):** `data` = the created `address_dict` (same fields as a list item, including `is_default`).
- **Errors:** `422 {"success": false, "message": "An address line is required", "errors": {"line": "required"}}`; bad JSON 400; 401/403.

#### `PUT /addresses/:address_id`  (update)
- **Auth:** Bearer required; own address only.
- **Purpose:** Update an address. Only provided non-null fields change. `make_default: true` promotes it.
- **Request:** any subset of the settable fields above; if `line` is present it must be non-blank.
- **Success (200):** `data` = updated `address_dict`.
- **Errors:** `404 {"success": false, "message": "Address not found"}`; `422` line-required if `line` blank; bad JSON 400; 401/403. (Only PUT and DELETE allowed on this route — no PATCH; a PATCH returns the framework 405.)

#### `DELETE /addresses/:address_id`
- **Auth:** Bearer required; own address only.
- **Purpose:** Delete an address. If it was the default, the most recent remaining address is auto-promoted.
- **Success (200):** message-only, **no `data`**:
```json
{ "success": true, "message": "Address deleted" }
```
- **Errors:** `404 "Address not found"`; 401/403.

#### `PUT /addresses/:address_id/default`
- **Auth:** Bearer required; own address only.
- **Purpose:** Mark this address as default (clears the flag on all others).
- **Request:** no body required.
- **Success (200):** message-only, **no `data`**:
```json
{ "success": true, "message": "Default address set" }
```
- **Errors:** `404 "Address not found"`; 401/403.

#### `GET /geo/reverse`  (Yandex reverse geocode proxy)
- **Auth:** Bearer required.
- **Purpose:** Resolve a lat/lng pin to a human address (when the customer drops a map pin).
- **Query:** `lat` (required), `lng` (required), `lang` (optional).
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "results": [
      { "formatted": "Uzbekistan, Tashkent, Chilonzor district, 24", "lat": 41.285612, "lng": 69.203491, "precision": "exact", "kind": "house" }
    ]
  }
}
```
- **Notes:** `results` may be `[]`. Reverse always requests 1 result.
- **Errors:** `422 {"message": "lat and lng are required", "errors": {"lat": "required", "lng": "required"}}`; `400 "Geocoding not configured"` (key unset); `400 "Geocoding service unavailable"` (upstream failure); 401/403.
- **Operational note:** `YANDEX_GEOCODER_KEY` is **not declared in `settings_base.py`** and is read via `getattr(settings, 'YANDEX_GEOCODER_KEY', '')`. Out of the box it is empty, so **both `/geo/reverse` and `/geo/forward` return `400 "Geocoding not configured"` until the key is added** to settings/env. Plan a graceful fallback (manual address entry) for launch.

#### `GET /geo/forward`  (Yandex forward geocode / search proxy)
- **Auth:** Bearer required.
- **Purpose:** Search addresses by free text (autocomplete).
- **Query:** `q` (required), `lang` (optional), `limit` (optional int, default 5, clamped 1..20).
- **Success (200):** same `data.results[]` item shape as reverse.
- **Errors:** `422 {"message": "A search query is required", "errors": {"q": "required"}}`; `400 "Geocoding not configured"` / `"Geocoding service unavailable"`; 401/403. (See the `YANDEX_GEOCODER_KEY` operational note above — this endpoint is off by default.)

---

### 6.6 Loyalty

#### `GET /loyalty`
- **Auth:** Bearer required. Read-only — there is **no** redemption endpoint here; points are reserved/applied via order creation (`points_used`).
- **Purpose:** Points balance, earn/redeem rates, and per-order history.
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "points": 1250,
    "earn_rate": { "points_per_uzs": 1000, "point_value_uzs": 1 },
    "history": [
      { "code": "SF-87", "points_earned": 42, "points_used": 0, "created_at": "2026-06-13T18:24:10.512000+00:00" },
      { "code": "SF-71", "points_earned": 0, "points_used": 500, "created_at": "2026-06-10T12:02:55.000000+00:00" }
    ]
  }
}
```
- **Notes:**
  - `points` = integer balance.
  - `earn_rate.points_per_uzs` — **the field name is misleading**: it is actually the UZS spent per 1 point **earned** (i.e. "1 point per N UZS"; `0` = loyalty off).
  - `earn_rate.point_value_uzs` — UZS each point is worth at redeem.
  - No tiers, no explicit redemption-rules object. `history` is one row per order (newest first); `code` = `SF-<id>`.
  - Loyalty on/off is also surfaced as `config.feature_flags.loyalty`.
- **Errors:** 401/403 (GET-only).

---

### 6.7 Support

#### `GET /support`
- **Auth:** Bearer required.
- **Purpose:** Support contact channels + a static trilingual FAQ for the help screen.
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "contacts": { "phone": "+998901234567", "telegram": "@smartfood_support", "email": "help@smartfood.uz" },
    "faq": [
      {
        "q": { "uz": "Buyurtmani qanday beraman?", "ru": "Как сделать заказ?", "en": "How do I place an order?" },
        "a": { "uz": "...", "ru": "...", "en": "Pick items from the menu, add them to the cart and confirm your order." }
      }
    ]
  }
}
```
- **Notes:** FAQ is a hardcoded 3-entry list; each `q`/`a` is `{uz, ru, en}`. `contacts` mirror `config.support`.
- **Errors:** 401/403.

#### `GET /support/tickets`
- **Auth:** Bearer required.
- **Purpose:** List this customer's ticket threads (newest first), each with its full message thread. No pagination.
- **Success (200):**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "items": [
      {
        "id": 5, "subject": "Order SF-87 was late", "status": "OPEN",
        "created_at": "2026-06-13T19:00:00.000000+00:00",
        "messages": [
          { "id": 9, "sender": "CUSTOMER", "text": "My order arrived an hour late.", "created_at": "2026-06-13T19:00:00.000000+00:00" },
          { "id": 10, "sender": "OPERATOR", "text": "Sorry! We refunded the delivery fee.", "created_at": "2026-06-13T19:05:00.000000+00:00" }
        ]
      }
    ]
  }
}
```
- **Notes:** `status` ∈ `OPEN | CLOSED`. `message.sender` ∈ `CUSTOMER | OPERATOR`. Empty → `items: []`.
- **Errors:** 401/403.

#### `POST /support/tickets`  (open ticket)
- **Auth:** Bearer required.
- **Request:** `text` **required** (non-empty after trim); `subject` optional (truncated to 160 chars, may be `""`).
```json
{ "subject": "Order SF-87 was late", "text": "My order arrived an hour late." }
```
- **Success (201):** `data` = the new ticket (status `OPEN`, first message `sender: CUSTOMER`).
- **Errors:** `422 {"message": "A message is required", "errors": {"text": "required"}}`; bad JSON 400; 401/403.

#### `POST /support/tickets/:ticket_id/messages`  (append message)
- **Auth:** Bearer required; own ticket only.
- **Request:** `{ "text": "Any update on the refund?" }` — `text` required, non-empty.
- **Success (200):** `data` = the full refreshed ticket (all messages). Customer messages are always `sender: CUSTOMER`.
- **Errors:** `422` text-required; `404 {"success": false, "message": "Ticket not found"}`; bad JSON 400; 401/403.

#### `POST /auth/logout`
- **Auth:** Bearer required.
- **Purpose:** Delete the current session.
- **Success (200):** `{ "success": true, "message": "Logged out" }`. **Idempotent** — returns 200 even if the session was already gone/expired.
- **Errors:** 401/403.

#### `GET` / `PATCH /me`  (profile)
- **Auth:** Bearer required. Same path serves GET and PATCH.
- **GET success (200):** `data` = the `customer_dict`:
```json
{ "success": true, "message": "Success", "data": { "id": 12, "telegram_id": 99887766, "name": "Ali Valiyev", "phone": "+998901234567", "language": "uz", "photo_url": "https://...", "loyalty": { "points": 150 } } }
```
- **PATCH request (all optional):** `{ "name": "Ali Valiyev", "phone": "+998901234567", "language": "uz" }`. `name` is split on the first space into first/last; `language` is normalized to `uz|ru|en`.
- **Errors:** 401/403; bad JSON 400 on PATCH.

---

## 7. Order lifecycle

`BotOrder.status` has exactly four values. The **POS kitchen status** (`pos_order.status`, e.g. `PREPARING`, `READY`) is a separate, finer-grained value that only exists after dispatch.

| `status` | Meaning | `pos_order` | Recommended UI |
|---|---|---|---|
| **PENDING** | Initial state on create. **Awaiting cashier confirmation** — sent to the operator queue but not yet a real POS order; no cashier has accepted. **Only state the customer can cancel.** | `null` | "Order received — awaiting confirmation." Show a **Cancel** button. Keep polling. |
| **DISPATCHED** | An operator dispatched it to an on-duty cashier. A real POS order was minted (POS status `PREPARING`, or `READY` immediately if all items are instant), earned loyalty points credited, Telegram confirmation pushed. | `{id, uuid, status, display_id}` | "Order confirmed!" Hide Cancel. Show kitchen progress from `pos_order.status` and the pickup/queue number `pos_order.display_id`. |
| **REJECTED** | Operator rejected the pending order; reserved loyalty points refunded; `reject_reason` set; Telegram rejection pushed. | `null` | "Order rejected." Show `reject_reason`. Offer re-order. |
| **CANCELED** | Customer canceled their own PENDING order; reserved loyalty points refunded. | `null` | "Order canceled." |

**The "awaiting cashier confirmation" gate:** order creation does **not** auto-dispatch. `POST /orders` only checks that ≥1 cashier is on an active shift at create time (`no_cashier` otherwise); the order is stored PENDING until an operator manually dispatches it (admin `POST /api/admins/smartfood/orders/:id/dispatch`). The customer learns of the resolution by **polling** `/track` (PENDING → DISPATCHED with `pos_order` populated, or → REJECTED) and via the complementary Telegram push.

List filters: `?status=active` = PENDING+DISPATCHED; `?status=history` = REJECTED+CANCELED; any other/omitted value = ALL.

---

## 8. Recommended end-to-end flow

1. **Boot.** Telegram launches the Mini App; read `window.Telegram.WebApp.initData`.
2. **Auth.** `POST /auth` with `{ init_data }`. The success is **HTTP 200 / "Success"** — do not assert 201. Store `data.token`, set the `Authorization` header. (If `data.is_new`, run onboarding.)
3. **Load config.** `GET /config`. If `data.enabled === false`, render the closed screen and stop (remember `enabled` is `false` by default until an operator turns the bot on). Cache `delivery_fee`, `free_delivery_threshold`, `min_order_amount`, `default_tip_options`, `service_area`, `feature_flags`, `support`. Pick language from `default_language` / customer language.
4. **Browse catalog.** `GET /catalog/categories`, then `GET /catalog/products?category_id=...` (or `?q=` for search). On any catalog response, **check `closed` first** — `bot_off` means render the closed screen. Open `GET /catalog/products/:id` for the configurator (sizes + topping groups). Enforce `min_select`/`max_select` (`0` = unlimited) per group.
5. **Build cart (client-side).** Maintain the items array `[{product_id, size_id?, topping_ids?[], quantity}]`. Send clean positive integer quantities and a truthy positive `size_id` (or omit it). Estimate prices client-side as base + size delta + toppings, but treat the server quote as authoritative.
6. **Quote.** On every cart change call `POST /cart/quote` with the full items array, `order_type`, `tip`, `points_used`. Handle conflict `code`s (`item_unavailable`, `topping_*`, `min_order`, `empty_cart`, `invalid_quantity`) by branching on `code` (not message text), and handle `bot_off`. Render `subtotal`, `delivery_fee`, `discount`, `tip`, `total`, and the loyalty preview from the response.
7. **Checkout.** For DELIVERY, ensure an `address_id` (create/select via the address endpoints; use `/geo/*` for the map, with a manual-entry fallback since the geocoder may be off). `POST /orders`. Handle the same conflict codes, plus `closed` (`bot_off` / `no_cashier`), `address_id` required (422), and `Address not found` (404). On success you get a PENDING order (HTTP 201) with `code` `SF-<id>`. Treat `order.phone` as possibly `""`.
8. **Track.** Poll `GET /orders/:id/track` on an interval. Show `status` (PENDING/DISPATCHED/REJECTED/CANCELED) and, once `pos_order != null`, the kitchen `pos_order.status` and `pos_order.display_id`. Allow **Cancel** only while PENDING. Also expect a Telegram push on dispatch/reject.

---

## 9. Appendix

### A. Field reference tables (canonical)

**`config_dict`** (`GET /config`)
| Field | Type | Notes |
|---|---|---|
| `currency` | string | e.g. `"UZS"` |
| `enabled` | bool | master store ON/OFF (no schedule); **defaults to false on a new install** |
| `delivery_fee` | int UZS | flat fee |
| `free_delivery_threshold` | int UZS | subtotal ≥ this → free delivery (0 = none) |
| `min_order_amount` | int UZS | min subtotal |
| `default_tip_options` | int[] UZS | suggested tips, may be `[]` |
| `supported_languages` | string[] | always `["uz","ru","en"]` |
| `default_language` | string | `uz|ru|en` |
| `service_area` | object | `{city, center{lat,lng}, polygon[]}` or `{}` (single zone) |
| `feature_flags` | object | `{loyalty: bool, card_payments: false, scheduled_delivery: false}` (latter two hardcoded false) |
| `support` | object | `{phone, telegram, email}` |

**`category_dict`** — `id` (POS id), `name`, `names{uz,ru,en}`, `sort`, `image_url`.

**`product_dict`** (list) — `id` (POS id), `category_id` (POS id), `name`, `names`, `price` (int UZS, live), `image_url`, `tag` (`bestseller|new|spicy|""`), `kcal` (int|null), `available` (bool).
**`product_dict`** (detail) — all of the above **plus** `description`, `descriptions{uz,ru,en}`, `sizes[]`, `topping_groups[]`.

**`size_dict`** — `id`, `name`, `names`, `price_delta` (int UZS), `is_default`. (Only `is_selling` sizes returned.)
**`topping_group_dict`** — `id`, `name` (resolved only, no `names`), `required`, `min_select`, `max_select` (0 = unlimited), `toppings[]`.
**`topping_dict`** — `id`, `name` (resolved only), `price` (int UZS). (Only `is_selling` toppings returned.)

**`customer_dict`** — `id`, `telegram_id`, `name` (`"first last".strip()` → username → `str(telegram_id)`), `phone`, `language`, `photo_url`, `loyalty{points}`.

**`address_dict`** — `id`, `label`, `line`, `lat` (float|null), `lng` (float|null), `city`, `street`, `house`, `apartment`, `entrance`, `floor`, `intercom`, `comment`, `precision`, `is_default`.

**`bot_order_dict`** — `id`, `code` (`SF-<id>`), `status`, `order_type` (`DELIVERY|PICKUP`), `created_at` (ISO|null), `phone` (string, may be `""`, never null), `note`, `address_text`, `payment_method` (`CASH|CARD`), `totals{subtotal, delivery_fee, discount, tip, total}` (all int UZS), `loyalty_points_used`, `loyalty_points_earned`, `items[]`, `pos_order` (null OR `{id, uuid, status, display_id}`), `dispatched_at` (ISO|null), `reject_reason`.

**`bot_order_item_dict`** — `product_id`, `size_id` (int|null), `quantity`, `unit_price` (int UZS), `line_total` (int UZS), `toppings[]` (`[{topping_id, name, price}]`), `detail`.

**Cart `quote` data** — `currency`, `subtotal`, `delivery_fee`, `free_delivery_applied`, `discount`, `tip`, `total`, `loyalty_points_used`, `loyalty_points_earned`, `lines[]` (`{product_id, size_id, name, quantity, unit_price, line_total, toppings[], detail}`).

**ID domains:** category `id`, product `id`/`category_id`, and order-item `product_id` are **POS-domain** ids. `size_id`, topping ids, group ids are **smartfood-local**. `BotOrder.id` is smartfood-local; the human code is `SF-<id>`.

### B. Endpoints NOT for the customer webapp (operator/staff — do not call)

These live under `/api/admins/smartfood` and require staff `manager_required` auth. They are listed only for lifecycle context. **The customer Mini App must not call any of them.**

- `GET /api/admins/smartfood/orders/pending` — pending-dispatch queue.
- `GET /api/admins/smartfood/cashiers/active` — cashiers on active shift (drives the `no_cashier` state customers see).
- `POST /api/admins/smartfood/orders/:bot_order_id/dispatch` — operator accepts a PENDING order, assigns a cashier, mints the POS order, sets `DISPATCHED`. Body `{ "cashier_id": 5 }`.
- `POST /api/admins/smartfood/orders/:bot_order_id/reject` — operator rejects (status `REJECTED`, refunds points). Body `{ "reason": "..." }` (optional).
- `GET | POST /api/admins/smartfood/config` — GET reads, POST edits store config.
- `POST /api/admins/smartfood/config/enable` — the master bot ON/OFF toggle. Body `{ "enabled": bool }`. **This is what flips the customer-facing `bot_off` "closed" state.**

Customers observe the *results* of dispatch/reject/enable only via `/config`, `/orders/:id/track`, and the Telegram push.

### C. Environment / config notes

- **Backend settings (server-side; not client-tunable):**
  - `CUSTOMER_BOT_TOKEN` — the Telegram bot token used to validate `initData` HMAC and to send customer push messages (defined in `settings_base.py`).
  - `SMARTFOOD_INITDATA_MAX_AGE` — initData freshness window for login (default 3600s).
  - `SMARTFOOD_AUTH_TTL` — issued session lifetime (default 86400s, returned as `expires_in`).
  - `YANDEX_GEOCODER_KEY` — backs `/geo/reverse` and `/geo/forward`. **It is NOT declared in `settings_base.py`** (read via `getattr(settings, 'YANDEX_GEOCODER_KEY', '')`), so it is empty out of the box and both geo endpoints return `400 "Geocoding not configured"` until it is added. Provide a manual-address-entry fallback.
- **`CUSTOMER_WEBAPP_URL`** — the URL the customer bot opens for this Mini App, used by the bot's "Open app" button. Confirmed: env var name is `CUSTOMER_WEBAPP_URL`, declared in `settings_base.py` with default `'https://example.com'`; the deploy script sets it to `https://<host>/webapp/`. Only the **per-deployment value** needs confirming, not the var name.
- **Session transport:** the server accepts the token via either the `Authorization: Bearer <token>` header **or** the `session_key` cookie (cookie checked first). For a Mini App, the Bearer header is recommended.

### D. CORS

CORS is configured in core settings (`corsheaders` installed; `CorsMiddleware` first in `MIDDLEWARE`). The mechanism:
- **Allowlist:** `CORS_ALLOWED_ORIGINS` is read from the `CORS_ALLOWED_ORIGINS` env (comma-separated).
- **Credentials:** `CORS_ALLOW_CREDENTIALS` is `True` **only when** that allowlist is non-empty — i.e. credentialed CORS is intentionally limited to explicitly-listed origins.
- **Open modes:** `CORS_ALLOW_ALL` (`1`/`true`) or `OPEN_LAN`, and `DEBUG`, open all origins but **with credentials OFF**.

The Bearer-header approach in this guide does **not** require credentialed CORS and is therefore the recommended transport. If you instead rely on the cookie `session_key`, the Mini App's served origin must be in `CORS_ALLOWED_ORIGINS` and the client must send `credentials: 'include'`. Confirm the deployed `CORS_ALLOWED_ORIGINS` value (the Mini App origin) with the deploy team before relying on cookie auth.

### E. Changelog

| Date | Change |
|---|---|
| 2026-06-14 | Initial integration guide (customer Mini App API under `/api/smartfood`). |
| 2026-06-14 | Corrections: `POST /auth` is 200/"Success" (not 201/"Created"); documented framework-level 405 envelope shape; clarified login 401-vs-403 logic; quantity is optional/defaults-to-1 with truncating coercion; `size_id` must be truthy-positive; `enabled` defaults to false on fresh install; added admin `/config/enable` toggle; resolved `CUSTOMER_WEBAPP_URL` and CORS from `// verify` to confirmed config; noted `YANDEX_GEOCODER_KEY` is undeclared so geo is off by default; exact `item_unavailable` strings; unrecognized `?status=` returns all; order `phone` may be `""`; logout is idempotent. |

---

This document was generated from the backend extraction in `smartfood/` (config/urls.py, smartfood/urls.py, security.py, gating.py, serializers.py, models.py, services/*, and the per-module view files), plus core helpers (`base/helpers/response.py`, `base/middlewares/force_json_middleware.py`) and `alpha_pos_core/settings_base.py` / `deploy.sh`. All previously-flagged `// verify` items have been resolved against code; only per-deployment **values** (the deployed `CUSTOMER_WEBAPP_URL`, `CORS_ALLOWED_ORIGINS`, and whether `YANDEX_GEOCODER_KEY` has been provisioned) need confirmation with the deploy team before launch.
