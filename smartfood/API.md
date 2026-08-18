# Smart Food — backend API (as built)

Server-only Django app `smartfood` in `alpha_pos_server`. The catalog is driven by
the existing POS (`base.Product` / `base.Category`) via a publish/stop-selling
shadow. Customer checkout creates a retry-safe **PENDING** order and durable
dispatch job; the worker automatically mints a real POS order on a live connected
till under its active on-shift cashier. Manager dispatch remains a fallback.

- **Base URL:** `https://<host>/api/smartfood` (customer) and
  `https://<host>/api/admins/smartfood` (operator console).
- **Money:** integer so'm (UZS), no decimals (e.g. `39000`).
- **Auth (customer):** Telegram Mini App `initData` → Bearer token. Send
  `Authorization: Bearer <token>` on every customer call.
- **Auth (operator):** existing staff manager session (`@manager_required`).
- **Closed responses:** when the bot is OFF or no connected till has an active
  on-shift cashier, customer endpoints return HTTP **200** with `{"success":
  false, "closed": true, "reason": "bot_off" | "no_cashier"}` — render a
  "closed" screen, not an error. A stale ACTIVE shift alone is insufficient.
- **Envelope:** success → `{"success": true, "data": ...}`; errors →
  `{"success": false, "message": ...}` (+ `code` for cart/order conflicts).

## Customer endpoints (`/api/smartfood`)

| Method | Path | Auth | Body / notes |
|---|---|---|---|
| POST | `/auth` | none | `{init_data}` → `{token, customer, is_new}` |
| POST | `/auth/logout` | bearer | invalidate session |
| POST | `/analytics/visit` | bearer | `{client_visit_id: UUID}`; idempotently records one successful Mini App boot |
| GET/PATCH | `/me` | bearer | profile; PATCH `{name, phone, language}` |
| GET | `/config` | bearer | delivery fee, thresholds, support, flags |
| GET | `/catalog/categories` | bearer | gated; published+selling |
| GET | `/catalog/products` | bearer | gated; `?category_id&tag&q&lang` |
| GET | `/catalog/products/<id>` | bearer | gated; incl. sizes + topping groups |
| POST | `/cart/quote` | bearer | `{items, order_type, tip, points_used}` → authoritative totals |
| POST | `/orders` | bearer | gated; required UUID `client_order_id`; idempotently creates PENDING order + durable automatic dispatch job |
| GET | `/orders` | bearer | `?status=active|history`; filters terminal linked POS states correctly |
| GET | `/orders/<id>` | bearer | own order only; includes `client_order_id`, raw `status`, derived `effective_status`, `address_text`, linked `pos_order` |
| POST | `/orders/<id>/cancel` | bearer | only while PENDING |
| GET | `/orders/<id>/track` | bearer | authoritative polling fallback; same full order contract as detail |
| GET/POST | `/addresses` | bearer | list / create (`line` required) |
| PUT/DELETE | `/addresses/<id>` | bearer | update / delete (promotes a new default) |
| PUT | `/addresses/<id>/default` | bearer | set default |
| GET | `/geo/reverse` | bearer | `?lat&lng&lang` (Yandex proxy) |
| GET | `/geo/forward` | bearer | `?q&lang&limit` (Yandex proxy) |
| GET | `/loyalty` | bearer | points + earn rate + history |
| GET | `/support` | bearer | contacts + FAQ |
| GET/POST | `/support/tickets` | bearer | list / open `{subject, text}` |
| POST | `/support/tickets/<id>/messages` | bearer | `{text}` |

**Cart item shape:** `{ "product_id": int, "size_id": int?, "topping_ids": [int], "quantity": int }`.
The server **recomputes** every price from the live POS price + size delta + topping
prices and re-validates publish/stop-selling at submit — client prices are ignored.
Cart/order inputs are strict: integer ids/quantities, distinct toppings, bounded
line counts/quantity/tip/points, and explicit DELIVERY/PICKUP + CASH/CARD enums.

**Idempotency:** generate and persist a UUID `client_order_id` before checkout.
Retry an ambiguous response with the same UUID and identical normalized payload;
the API returns the original order (200) instead of creating/dispatching twice.
Existing-key replay/conflict is resolved before live-availability gating, so the
accepted result stays discoverable after bot closure/till disconnect and replays
cannot consume dispatch attempts or bypass backoff. Using the UUID for a changed
payload returns `idempotency_conflict` (409).

**Status/address round-trip:** dispatch sets POS `order_origin=TELEGRAM`, copies
the snapshotted `address_text` into the POS order's canonical `delivery_address`,
and cloud/local sync returns POS status. Customer UI should render
`effective_status`: PENDING, PREPARING, READY, COMPLETED, CANCELED, or REJECTED.
Raw BotOrder `status` remains DISPATCHED while its linked POS order advances.

**Loyalty settlement:** `loyalty_points_earned` on quote/order payloads is the
prospective award, not proof that the balance was credited. Earn is released
exactly once only after the linked POS order is COMPLETED, `is_paid=true`, has a
non-null `paid_at`, and passes concrete tender-integrity validation. PENDING
cancel/reject or linked POS cancellation restores checkout-reserved points once;
linked cancellation also reverses any released (including legacy) earn once.
Locked timestamp flags make reconciliation idempotent. POS-order signals trigger
it after commit, while the five-second worker performs a bounded repair sweep for
missed callbacks and pre-upgrade rows even when auto-dispatch is disabled.

## Operator console (`/api/admins/smartfood`, manager auth)

The standalone Telegram Bot Studio signs in through the existing admin endpoints
under `/api/admins`: `POST /auth-login`, `GET /auth-me`, and `POST /auth-logout`.
It intentionally does not expose dispatch, cashier, or order-queue controls;
those remain available to legacy/operator clients only.

| Method | Path | Notes |
|---|---|---|
| GET | `/analytics/overview?days=7\|30\|90` | opens, distinct visitors, converted visitors, orders, conversion rate, daily series, and catalog photo readiness |
| GET | `/visitors` | searchable visitor list; `?q&converted=true\|false&page&per_page` |
| GET/POST | `/config` | read / update bot behavior; admin reads expose only masked token metadata and accept a write-only `bot_token` |
| POST | `/config/enable` | `{enabled: bool}` — the dynamic bot ON/OFF |
| GET | `/orders/pending` | pending/retry queue and manual fallback |
| GET | `/cashiers/active` | on-shift cashiers (public checkout also requires live till presence) |
| POST | `/orders/<id>/dispatch` | manual fallback `{cashier_id}` → mints a POS order |
| POST | `/orders/<id>/reject` | `{reason}` → refunds reserved loyalty, no POS order |
| GET | `/catalog/products` | published Telegram products; `?q&availability=all\|available\|stopped` |
| GET | `/catalog/unpublished` | POS products not yet accepted to the bot |
| POST | `/catalog/import` | bulk publish `{product_ids: [int]}` (maximum 200) |
| POST | `/products/<id>/accept` | publish to the bot (`{name_*, image_url, tag, kcal, ...}`) |
| PATCH | `/products/<id>` | edit bot fields |
| POST | `/products/<id>/stop` · `/resume` | runtime stop-selling toggle |
| POST/DELETE | `/products/<id>/image` | upload or remove a managed JPEG/PNG/WebP product photo (8 MB maximum) |
| POST/PATCH/POST | `/categories/<id>/accept · <id> · stop/resume` | same for categories |
| POST/PATCH/DELETE | `/products/<id>/sizes` · `/sizes/<id>` | size tiers |
| POST/PATCH/DELETE | `/products/<id>/topping-groups` · `/topping-groups/<id>` | option sets |
| POST/PATCH/DELETE | `/topping-groups/<id>/toppings` · `/toppings/<id>` | options |

## Bot + deploy
- Customer bot runs by **long-polling**: `python manage.py run_customer_bot` (the
  `bot` service in `docker-compose.yaml`). It stays alive without a configured
  token, picks up a database override or environment fallback without a container
  restart, honors `BotConfig.enabled` at runtime, and shows the WebApp "open menu"
  button when enabled.
- Customer order realtime is read-only and ownership-scoped at
  `/ws/smartfood/orders/<id>/?token=<customerToken>`. Frames contain the full order
  including `effective_status`; clients must keep `/track` polling as fallback.
- Public launch requires `SMARTFOOD_AUTO_DISPATCH=true` and the restartable
  `smartfood_dispatch` service (`process_smartfood_dispatch_jobs`). It performs an
  every-five-second due-job scan, crash recovery, and bounded exponential retry.
  The HTTP create/replay path only commits or repairs the due-now job; it never
  performs a dispatch attempt, so worker backoff remains authoritative.
  Defaults: 12 attempts, 5s base, 60s ceiling, 60s processing lease. Exhaustion
  rejects the still-PENDING order, refunds loyalty, and exposes only localized,
  customer-safe failure/support copy; internal worker errors are never returned.
  The same worker also repairs due loyalty settlement/reversal independently of
  the `SMARTFOOD_AUTO_DISPATCH` flag.
- `deploy.sh` bakes `CUSTOMER_BOT_TOKEN`, `CUSTOMER_WEBAPP_URL`,
  `CUSTOMER_WEBHOOK_SECRET`, `SMARTFOOD_AUTH_TTL`, `YANDEX_GEOCODER_KEY`,
  `SMARTFOOD_AUTO_DISPATCH`, retry/lease settings, and
  `SMARTFOOD_MAX_ITEM_QUANTITY` into `.env`.
