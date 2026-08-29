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
- **Closed responses:** quote/new-order calls return HTTP **200** with
  `{"success": false, "closed": true, "reason": "bot_off" | "no_cashier"}`
  when ordering is unavailable. Catalog reads remain available while closed; a
  stale ACTIVE shift alone is insufficient for a new checkout.
- **Envelope:** success → `{"success": true, "data": ...}`; errors →
  `{"success": false, "message": ...}` (+ `code` for cart/order conflicts).

## Customer endpoints (`/api/smartfood`)

| Method | Path | Auth | Body / notes |
|---|---|---|---|
| POST | `/auth` | none | `{init_data}` → `{token, customer, is_new}` |
| POST | `/auth/logout` | bearer | invalidate session |
| POST | `/analytics/visit` | bearer | `{client_visit_id: UUID}`; idempotently records one successful Mini App boot |
| GET/PATCH | `/me` | bearer | profile; PATCH `{first_name, last_name, phone, confirm, language, broadcast_opted_in}` |
| GET | `/config` | bearer | delivery fee, thresholds, support, and independent `loyalty_earning` / `loyalty_spending` flags |
| GET | `/catalog/categories` | bearer | browsable while ordering is closed; published+selling |
| GET | `/catalog/products` | bearer | browsable while ordering is closed; `?category_id&tag&q&lang` |
| GET | `/catalog/products/<id>` | bearer | browsable while ordering is closed; incl. sizes + topping groups |
| GET | `/banners` | bearer | active, imaged, in-schedule Home banners; `?lang` |
| POST | `/cart/quote` | bearer | `{items, order_type, tip, points_used}` → authoritative totals |
| POST | `/orders` | bearer | gated; required UUID `client_order_id`; idempotently creates PENDING order + durable automatic dispatch job |
| GET | `/orders` | bearer | `?status=active|history`; filters terminal linked POS states correctly |
| GET | `/orders/<id>` | bearer | own order only; includes `client_order_id`, raw `status`, derived `effective_status`, snapshotted `address_text`/`address_location`, linked `pos_order` |
| POST | `/orders/<id>/cancel` | bearer | only while PENDING |
| GET | `/orders/<id>/track` | bearer | authoritative polling fallback; same full order contract as detail |
| GET/POST | `/addresses` | bearer | list / create (`line`, `lat`, and `lng` required) |
| PUT/DELETE | `/addresses/<id>` | bearer | update / delete; the resulting record must retain valid coordinates |
| PUT | `/addresses/<id>/default` | bearer | set default |
| GET | `/geo/reverse` | bearer | `?lat&lng&lang` (Yandex proxy) |
| GET | `/geo/forward` | bearer | `?q&lang&limit` (Yandex proxy) |
| GET | `/loyalty` | bearer | points, earn/checkout rates, ledger history, issued redemptions |
| GET | `/rewards` | bearer | personalized active reward catalog; `?lang` |
| POST | `/rewards/<id>/redeem` | bearer | atomically spend the reward point price and mint a code |
| GET | `/redemptions` | bearer | this customer's recent redemption records |
| GET | `/support` | bearer | contacts + FAQ |
| GET/POST | `/support/tickets` | bearer | list / open `{subject, text}` |
| POST | `/support/tickets/<id>/messages` | bearer | `{text}` |

**Cart item shape:** `{ "product_id": int, "size_id": int?, "topping_ids": [int], "quantity": int }`.
The server **recomputes** every price from the live POS price + size delta + topping
prices and re-validates publish/stop-selling at submit — client prices are ignored.
Cart/order inputs are strict: integer ids/quantities, distinct toppings, bounded
line counts/quantity/tip/points, and explicit DELIVERY/PICKUP + CASH/CARD enums.

**Identity, language, and notifications:** checkout requires separately stored,
non-empty first and last names, a canonical Uzbekistan phone (`998` plus nine
digits), and `profile_confirmed_at` set by an explicit `PATCH /me` with
`confirm:true`. Editing either name or the phone clears the earlier confirmation
unless the same request reconfirms the resulting values. A supplied order phone
must match the confirmed account phone. Initial language follows Telegram's
`language_code`; unknown/missing values become Uzbek. An explicit `uz|ru|en`
profile choice is marked as an override and survives later Telegram logins.
`broadcast_opted_in` is a durable server preference; order-status messages are
operational and remain enabled.

**Exact catalog visibility:** a customer product must have a published/selling
`BotProduct`, an undeleted POS product, and a published/selling `BotCategory` for
its POS category. Category and product lists silently omit anything else; detail
returns 404. Admin `customer_available`, product banner destinations, and
`FREE_PRODUCT` rewards use this same cohort. Resuming a product never overrides a
stopped category.

Catalog reads are deliberately not store-open gated. When `BotConfig.enabled` is
false, the Mini App may still show the current menu with closed-ordering copy;
quote and genuinely new order creation remain authoritative gates.

**Home banners:** `/banners` is authenticated but not store-open gated. It returns
only active rows with an image, `starts_at IS NULL OR starts_at <= now`,
`ends_at IS NULL OR ends_at > now`, and a still-visible product for `PRODUCT`
actions. Actions are `NONE`, `CATALOG`, `PRODUCT`, and `LOYALTY`; rows are ordered
by `sort_order`, then id.

**Idempotency:** generate and persist a UUID `client_order_id` before checkout.
Retry an ambiguous response with the same UUID and identical normalized payload;
the API returns the original order (200) instead of creating/dispatching twice.
Existing-key replay/conflict is resolved before live-availability gating, so the
accepted result stays discoverable after bot closure/till disconnect and replays
cannot consume dispatch attempts or bypass backoff. Using the UUID for a changed
payload returns `idempotency_conflict` (409).

**Status/address round-trip:** every delivery address must have a pinned latitude
and longitude. Order creation freezes its text and both coordinates on the
`BotOrder`; later address edits/deletion cannot alter the accepted order, courier
assignment, or customer/order detail. Dispatch sets POS `order_origin=TELEGRAM`,
copies `address_text` into the POS order's canonical `delivery_address`, and
cloud/local sync returns POS status. Customer UI should render
`effective_status`: PENDING, PREPARING, READY, COMPLETED, CANCELED, or REJECTED.
Raw BotOrder `status` remains DISPATCHED while its linked POS order advances.

**Durable Telegram status outbox:** placed, dispatched, preparing, ready,
completed, canceled, and rejected transitions create localized, de-duplicated
outbox rows. They are processed in per-order event order and have priority over
broadcast rows. Telegram delivery is complementary to WebSocket/polling state;
it is retried durably rather than sent inside the order transaction.

**Loyalty settlement:** `loyalty_points_earned` on quote/order payloads is the
prospective award, not proof that the balance was credited. Earn is released
exactly once only after the linked POS order is COMPLETED, `is_paid=true`, has a
non-null `paid_at`, and passes concrete tender-integrity validation. PENDING
cancel/reject or linked POS cancellation restores checkout-reserved points once;
linked cancellation also reverses any released (including legacy) earn once.
Locked timestamp flags make reconciliation idempotent. POS-order signals trigger
it after commit, while the five-second worker performs a bounded repair sweep for
missed callbacks and pre-upgrade rows even when auto-dispatch is disabled.

Earning and checkout spending are independent runtime controls.
`loyalty_earn_per = 0` stops new earning, while `loyalty_point_value = 0` stops
using points as an order discount. The legacy `feature_flags.loyalty` is their OR;
the specific flags are `loyalty_earning` and `loyalty_spending`. Smart Club reward
redemption is independent of checkout point value and spends each reward's own
positive `points_cost`.

The public reward catalog includes only active, positive-cost, in-stock rewards
with valid kind-specific configuration. `FREE_PRODUCT` also needs a currently
visible linked product/category, and `DISCOUNT` needs a positive amount. Null stock
and a per-customer limit of 0 mean unlimited. Customer payloads expose
`affordable`, `limit_reached`, and authoritative `can_redeem`; canceled
redemptions do not count toward the limit.

## Operator console (`/api/admins/smartfood`, manager auth)

The standalone Telegram Bot Studio signs in through the existing admin endpoints
under `/api/admins`: `POST /auth-login`, `GET /auth-me`, and `POST /auth-logout`.
It intentionally does not expose dispatch, cashier, or order-queue controls;
those remain available to legacy/operator clients only.

| Method | Path | Notes |
|---|---|---|
| GET | `/analytics/overview?days=7\|30\|90` | opens, distinct visitors, converted visitors, orders, conversion rate, daily series, and catalog photo readiness |
| GET | `/visitors` | searchable visitor list; `?q&converted=true\|false&page&per_page` |
| GET | `/users/summary` | registered/new/active/profile-ready/customer/broadcast-eligible totals and language counts |
| GET | `/users` | customer registry; `?q&language=uz\|ru\|en&profile=complete\|incomplete&ordered=true\|false&page&per_page` |
| GET | `/users/<id>` | customer identity, reachability/preferences, metrics, addresses, and 20 recent orders |
| GET/POST | `/broadcasts` | list/filter drafts and delivery reports, or create a localized draft; `?status&q&page&per_page` |
| GET/PATCH/DELETE | `/broadcasts/<id>` | detail/update/delete; only `DRAFT` rows are mutable/deletable |
| POST | `/broadcasts/<id>/send` | `{expected_updated_at}` exact-draft fence; refreshes and freezes the current eligible audience into durable outbox rows |
| POST/DELETE | `/broadcasts/<id>/image` | attach/remove an optional Telegram photo while still a draft |
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
| GET/POST | `/marketing/banners` | list (with live/draft summary) or create Home banners |
| PATCH/DELETE | `/marketing/banners/<id>` | edit/pause/schedule or permanently delete a banner |
| POST/DELETE | `/marketing/banners/<id>/image` | upload/remove banner art; removal also pauses it |
| GET/POST | `/loyalty/rewards` | list (with global availability summary) or create rewards |
| PATCH/DELETE | `/loyalty/rewards/<id>` | edit/hide or delete; redemption history blocks deletion |
| POST/DELETE | `/loyalty/rewards/<id>/image` | upload/remove optional reward art |
| POST/PATCH/POST | `/categories/<id>/accept · <id> · stop/resume` | same for categories |
| POST/PATCH/DELETE | `/products/<id>/sizes` · `/sizes/<id>` | size tiers |
| POST/PATCH/DELETE | `/products/<id>/topping-groups` · `/topping-groups/<id>` | option sets |
| POST/PATCH/DELETE | `/topping-groups/<id>/toppings` · `/toppings/<id>` | options |

Admin product rows expose both resolved `names` / `descriptions` (with POS
fallbacks applied) and raw `name_overrides` / `description_overrides`. Editors
must populate writable fields from the raw override maps so an unrelated save
does not freeze a fallback as a bot-owned translation. Product writes validate
localized-name/tag limits and non-negative whole-number `kcal` / `sort_order`
values before saving; invalid supplied fields return HTTP 422 with an `errors`
map and do not mutate the product.

Broadcast recipients are the send-time snapshot of customers who are not blocked,
are opted in, and are currently Telegram-reachable. Russian/English blank copy
falls back to required Uzbek. The worker checks eligibility again immediately
before each broadcast delivery, so a later opt-out/block/unreachable state becomes
`SKIPPED`. Successful bot replies/sends restore reachability; permanent Telegram
blocked/chat-not-found failures suppress the customer from later audiences.
Queued content is immutable. Delivery reports progress through
`QUEUED`/`SENDING` to `SENT`, `PARTIAL`, or `FAILED` and expose delivered, failed,
and skipped counts.

All managed image endpoints inspect and fully decode file bytes and accept JPEG,
PNG, or WebP up to and including 8 MB. Recommended client framing is 1200×900
(4:3) for products, 1440×720 (2:1) for banners, 800×800 (1:1) for rewards, and
1200×628 (1.91:1) for broadcast photos. Only broadcasts enforce Telegram photo
geometry (`width + height <= 10000`, aspect ratio at most 20:1); uploaded WebP
broadcasts are converted once to JPEG and the converted file must remain within
8 MB. Other recommended dimensions are guidance. Storage failures return stable
503 copy and leave the previous URL intact. Public immutable media lives under
`/api/smartfood/media/products|banners|rewards|broadcasts/<uuid>.<ext>`; unknown,
unsafe, damaged, or missing files are rejected/404 as appropriate.

## Bot + deploy
- The checked-in production surface for the current host is:
  `https://pos.78.111.90.65.nip.io` for Django/POS APIs,
  `https://delivery.78.111.90.65.nip.io/webapp/` for the customer Mini App (with
  same-origin `/api/smartfood` proxying to `pos.`), and
  `https://alpha-pos-admin.78.111.90.65.nip.io` for the standalone admin SPA
  (which calls `https://pos.78.111.90.65.nip.io/api/admins` directly).
- Caddy terminates HTTPS on the external `edge` network. Stable aliases are
  `alpha-web` (Django), `delivery-webapp` (customer nginx), and `alpha-pos-admin`
  (admin nginx). `CUSTOMER_WEBAPP_URL` and the Telegram menu button must exactly
  match the canonical `delivery.` `/webapp/` URL.
- Customer chat entry is immediate in both supported update paths: the webhook
  handler and the **long-polling** `python manage.py run_customer_bot` service.
  Every message offers the Mini App even when ordering is closed. The reply first
  removes the retired persistent contact keyboard, then attaches the inline Web
  App button to the same message (with an inline fallback). Polling deletes any
  webhook before `getUpdates`, installs a persistent Telegram chat-menu Web App
  shortcut, stays parked without a token, and picks up runtime token changes.
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
- The restartable `smartfood_messages` service runs
  `process_smartfood_messages --interval 2 --batch-size 25`. It is isolated from
  POS dispatch, reclaims five-minute stale claims, prioritizes ordered status
  events, and durably fans out/retries Telegram sends (five delivery attempts;
  exponential delay capped at 15 minutes, with Telegram `retry_after` honored).
- `deploy.sh` bakes `CUSTOMER_BOT_TOKEN`, `CUSTOMER_WEBAPP_URL`,
  `CUSTOMER_WEBHOOK_SECRET`, `SMARTFOOD_AUTH_TTL`, `YANDEX_GEOCODER_KEY`,
  `SMARTFOOD_AUTO_DISPATCH`, retry/lease settings, and
  `SMARTFOOD_MAX_ITEM_QUANTITY` into `.env`.
