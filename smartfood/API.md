# Smart Food — backend API (as built)

Server-only Django app `smartfood` in `alpha_pos_server`. The catalog is driven by
the existing POS (`base.Product` / `base.Category`) via a publish/stop-selling
shadow; a bot order is created **PENDING** and an operator **dispatches** it to a
specific on-duty cashier, which mints a real POS order under that cashier.

- **Base URL:** `https://<host>/api/smartfood` (customer) and
  `https://<host>/api/admins/smartfood` (operator console).
- **Money:** integer so'm (UZS), no decimals (e.g. `39000`).
- **Auth (customer):** Telegram Mini App `initData` → Bearer token. Send
  `Authorization: Bearer <token>` on every customer call.
- **Auth (operator):** existing staff manager session (`@manager_required`).
- **Closed responses:** when the bot is OFF or no cashier is on duty, customer
  endpoints return HTTP **200** with `{"success": false, "closed": true, "reason":
  "bot_off" | "no_cashier"}` — render a "closed" screen, not an error.
- **Envelope:** success → `{"success": true, "data": ...}`; errors →
  `{"success": false, "message": ...}` (+ `code` for cart/order conflicts).

## Customer endpoints (`/api/smartfood`)

| Method | Path | Auth | Body / notes |
|---|---|---|---|
| POST | `/auth` | none | `{init_data}` → `{token, customer, is_new}` |
| POST | `/auth/logout` | bearer | invalidate session |
| GET/PATCH | `/me` | bearer | profile; PATCH `{name, phone, language}` |
| GET | `/config` | bearer | delivery fee, thresholds, support, flags |
| GET | `/catalog/categories` | bearer | gated; published+selling |
| GET | `/catalog/products` | bearer | gated; `?category_id&tag&q&lang` |
| GET | `/catalog/products/<id>` | bearer | gated; incl. sizes + topping groups |
| POST | `/cart/quote` | bearer | `{items, order_type, tip, points_used}` → authoritative totals |
| POST | `/orders` | bearer | gated + needs cashier on duty; creates PENDING order |
| GET | `/orders` | bearer | `?status=active|history` |
| GET | `/orders/<id>` | bearer | own order only |
| POST | `/orders/<id>/cancel` | bearer | only while PENDING |
| GET | `/orders/<id>/track` | bearer | status + linked POS order uuid/status |
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

## Operator console (`/api/admins/smartfood`, manager auth)

| Method | Path | Notes |
|---|---|---|
| GET/POST | `/config` | read / update (incl. `enabled`, fees, service area, loyalty) |
| POST | `/config/enable` | `{enabled: bool}` — the dynamic bot ON/OFF |
| GET | `/orders/pending` | the dispatch queue |
| GET | `/cashiers/active` | cashiers currently on an ACTIVE shift |
| POST | `/orders/<id>/dispatch` | `{cashier_id}` → mints a POS order under that cashier |
| POST | `/orders/<id>/reject` | `{reason}` → refunds reserved loyalty, no POS order |
| GET | `/catalog/unpublished` | POS products not yet accepted to the bot |
| POST | `/products/<id>/accept` | publish to the bot (`{name_*, image_url, tag, kcal, ...}`) |
| PATCH | `/products/<id>` | edit bot fields |
| POST | `/products/<id>/stop` · `/resume` | runtime stop-selling toggle |
| POST/PATCH/POST | `/categories/<id>/accept · <id> · stop/resume` | same for categories |
| POST/PATCH/DELETE | `/products/<id>/sizes` · `/sizes/<id>` | size tiers |
| POST/PATCH/DELETE | `/products/<id>/topping-groups` · `/topping-groups/<id>` | option sets |
| POST/PATCH/DELETE | `/topping-groups/<id>/toppings` · `/toppings/<id>` | options |

## Bot + deploy
- Customer bot runs by **long-polling**: `python manage.py run_customer_bot` (the
  `bot` service in `docker-compose.yaml`). It honors `BotConfig.enabled` at runtime
  (no restart to turn off) and shows the WebApp "open menu" button when enabled.
- `deploy.sh` bakes `CUSTOMER_BOT_TOKEN`, `CUSTOMER_WEBAPP_URL`,
  `CUSTOMER_WEBHOOK_SECRET`, `SMARTFOOD_AUTH_TTL`, `YANDEX_GEOCODER_KEY` into `.env`.
