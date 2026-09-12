# Loyalty and Waiter Backend Audit

Date: 2026-09-12  
Status: Final review complete; server and delivery app deployed. Desktop 1.0.45 is published on the control server; restaurant installation remains outstanding.  
Scope: Smart Food delivery loyalty, legacy stamp loyalty, waiter backend, cashier handoff, shifts, stock, discounts, synchronization, admin management, and current production evidence

## Remediation and configurable settings

The audit findings have been addressed in the cloud backend, local waiter/POS backend, their shared-core worktrees, and the existing delivery customer app. No waiter frontend was introduced. The cloud backend and delivery app are now committed and deployed. Desktop 1.0.45 is published on the control server after its frozen startup and signature checks; restaurant installation remains a separate operator action.

### Settings the operator can choose

| Setting | Choices | Default | Where it is enforced |
|---|---|---|---|
| `waiter_enabled` | `true` / `false` | `false` | Login and authenticated waiter operations, including shared POS routes |
| `waiter_payment_mode` | `CASHIER_HANDOFF` / `PERMITTED_WAITER` | `CASHIER_HANDOFF` | Shared payment endpoint and payment service |
| `waiter_require_shift` | `true` / `false` | `false` | Order creation, under the active shift lock |
| `loyalty_earning_basis` | `PAID_MERCHANDISE` / `MERCHANDISE_SUBTOTAL` | `PAID_MERCHANDISE` | Server cart pricing and saved order policy |
| `reward_valid_days` | Whole number, 1–3650 | 30 | New reward issuance |

`PERMITTED_WAITER` also requires the individual waiter's `order.pay` permission and an active collecting shift. Selecting the setting alone does not grant financial access. Paid-order refunds remain a manager operation. Waiter discount and cancellation operations require `discount.apply` and `order.cancel` respectively. The default waiter role still grants only create/update.

For delivery orders, `PAID_MERCHANDISE` earns on merchandise after the point discount; `MERCHANDISE_SUBTOTAL` earns on merchandise before that discount. Delivery fees and tips earn no points. POS receipt scans use the verified, discounted receipt total; they cannot trust a client-supplied amount. Each new delivery order stores a versioned policy snapshot and its computed points. Later settings changes do not recalculate previous orders. Each new waiter order stores its creation policy; current access/payment settings take effect immediately for subsequent actions.

Use the existing Django admin forms for **App settings** and **Bot config**, or the following APIs. These are backend/operator settings; a new waiter UI remains outside this change.

- Local restaurant backend: `GET` / `PUT /api/waiters/settings` (Manager/Admin).
- Cloud app settings: `GET` / `PUT /api/admins/app-settings` (Manager/Admin).
- Cloud loyalty: `GET` / `POST /api/admins/smartfood/config` (Manager/Admin).
- Waiter capabilities: `GET /api/waiters/venue-config`, now reflects actual payment and discount permissions.

Example local settings update:

```json
{
  "waiter_enabled": true,
  "waiter_payment_mode": "CASHIER_HANDOFF",
  "waiter_require_shift": true
}
```

Example loyalty configuration update:

```json
{
  "loyalty_earning_basis": "PAID_MERCHANDISE",
  "reward_valid_days": 30
}
```

App settings belong to the backend database being configured. The cloud settings endpoint does not silently update every restaurant's local settings. Configure the local waiter backend for the restaurant; loyalty configuration is deployment-wide on the delivery cloud. This release does not introduce tenant-specific loyalty balances or cross-terminal distributed table locking.

### Completed repairs

| Area | Implemented behavior |
|---|---|
| Point spending | Debit only whole points whose full value fits the merchandise subtotal; preserve fractional point value and unspent balance. The delivery app supports a chosen partial amount. |
| Earning policy | Both requested earning choices are selectable. Saved order points/policy remain historical snapshots. |
| Point ledger arithmetic | Late reversals preserve exact signed debt instead of clamping the balance to zero. Negative balances cannot fund spending; future awards offset the debt. |
| One program per receipt | TELEGRAM orders cannot earn legacy stamps. Receipt-scanned points and legacy stamps exclude one another under the order lock. Concrete tender evidence is required. |
| Reward retry safety | Required `Idempotency-Key`; request fingerprint, mutations, and exact response commit together. Same key/different payload returns 409. Browser retries preserve a member-scoped command key. |
| Staff award safety | Explicit loyalty permissions, strict integer bounds, mandatory adjustment reasons, receipt identity for scans, fresh balances, staff/transaction evidence, and durable replay. |
| Reward lifecycle | Immutable promised-gift snapshot, configurable expiry for new gifts, customer/staff cancellation, once-only point refund and reserved-stock restoration, fulfillment replay, and worker expiry. |
| Receipt refunds | Receipt-backed scan awards are reversed once after cancellation/refund by the existing worker. |
| Admin bypasses | Customer points are read-only in Django admin. Unsafe direct redemption actions were removed; the API owns those transitions. Catalogue/profile saves no longer overwrite unrelated concurrent balance/stock changes. |
| Worthless free delivery | A free-delivery reward cannot be purchased when delivery already costs zero. |
| API/client fidelity | Exact decimal loyalty rates, canonical `uzs_per_point` with the old alias retained, full signed history, balance-after/reason, gift expiry/actions, visible load failures with retry, and stale-response protection. |
| Waiter attribution | Separate `Order.waiter` and `waiter_shift`, preserved across cashier collection, serialized/synchronized, with legacy creator fallback. No guessed historical backfill. |
| Waiter access | Feature switch enforced, per-action permissions checked before retry replay, own-order reads/writes, and customer directory/KDS enumeration blocked. |
| Shared order engine | Waiter mutations delegate to the existing POS aggregate instead of maintaining duplicate payment/stock/state logic. |
| Order states | Shared transition validation; no reopening completed/canceled work; valid paid PREPARING → READY preserved. Readiness records actual stock transitions. |
| Stock timing | CREATED, PREPARING, READY and instant creation use actual events. Global stock settings govern timing; per-product timing is explicitly retired as an editable policy. Duplicate deductions/reversals serialize on the order; errors roll back regardless of negative-stock policy. |
| Floor | Locked one-active-unpaid-ticket claim, active/branch/place validation, HALL/table shape checks, and occupancy reconciliation that also tolerates legacy duplicate tickets safely. |
| Shifts/attendance | Attendance follows waiter shift start/end, not login/logout. Optional shift requirement is configurable; unpaid served orders block shift closing. |
| Statistics | Waiter service identity survives cashier handoff. Paid and refund counts are separate; cross-period refunds cannot create negative paid counts. Creation/payment/refund clock definitions are returned. Kitchen reports default to CHEF and explicitly identify branch-window metrics, not personal preparation. |
| Observability | Table, shift, and policy HTTP evidence; explicit discount/table/policy audit events; read-only drift command below. |

### New and changed API contracts

Required `Idempotency-Key` on customer reward redeem/cancel; staff scan/grant/fulfill/cancel; dedicated waiter create/add/cancel; waiter shared create/add and discount apply/remove. Existing cashier payment action identity remains intact. Check authentication and current permissions before returning any stored response.

- `POST /api/smartfood/rewards/{id}/redeem` — empty JSON object plus key.
- `POST /api/smartfood/redemptions/{code}/cancel` — `{ "reason": "..." }`, own issued gift only.
- `POST /api/admins/smartfood/loyalty/scan` — `{ "member_id": "SF-...", "order_id": 123 }`; an amount alone is rejected.
- `POST /api/admins/smartfood/loyalty/grant` — signed whole `points`, member ID, and 1–200 character reason.
- `POST /api/admins/smartfood/loyalty/fulfill` — gift code.
- `POST /api/admins/smartfood/loyalty/cancel` — gift code and reason.

New permissions: `loyalty.member.view`, `loyalty.award`, `loyalty.adjust`, `loyalty.fulfill`, `loyalty.correct`. New Manager role templates include these permissions; Admin remains authorized. Existing explicit user permission lists are preserved and must be intentionally updated when an operator needs these actions. Do not grant general warehouse/Treasury privileges for loyalty operations.

### Migrations and rollout

- Local/standalone core: `base.0057_waiter_service_identity`, `base.0058_waiter_policy_audit`.
- Cloud core: `base.0066_waiter_service_identity`, `base.0067_waiter_policy_audit`.
- Cloud delivery: `smartfood.0008_loyalty_accounting_and_policy`, `smartfood.0009_receipt_refund_reversal`, `smartfood.0010_loyalty_config_validation`.
- Both `makemigrations --check --dry-run` checks report **No changes detected**.
- All five cloud migrations have been applied to production after an isolated image canary and a database backup. Desktop migrations apply when the restaurant installs 1.0.45.
- Old waiter identities and old gift expiry/reservation/snapshot evidence are not invented. Historical rows remain reviewable; drift is reported rather than automatically changing financial history.
- Apply the cloud schema/backend and delivery app together because reward mutation APIs now require keys. Local waiter changes need a desktop/backend update; a cloud-only deployment does not replace restaurant code.
- Keep the existing Smart Food dispatch worker running: it also repairs settlement, expires rewards, and reverses refunded scan awards.

Read-only pre/post rollout check:

```bash
python manage.py audit_loyalty_waiter --limit 100
```

The command emits counts and bounded example IDs for ledger mismatches, double-program credits, table inconsistencies, missing legacy waiter identity, shift identity/branch conflicts, and potentially missing stock deduction. It never repairs or invents points, stock, sales, or financial evidence. Missing stock evidence needs human review of when tracking was enabled.

### Validation evidence for remediation

Final validation used isolated PostgreSQL 17 databases, not restaurant or cloud production data. Counts below belong to individual runs and overlap; they must not be added as a unique-test total.

| Validation | Result |
|---|---|
| Full Smart Food suite + cloud checkout + app-setting regression tests, PostgreSQL | **380 passed**, no skips |
| Waiter + customer orders/payments + stock inventory + shifts + discounts, PostgreSQL | **354 passed**, no skips |
| Final expanded waiter policy/ownership/retry/attendance/cross-period suite, PostgreSQL | **20 passed**, no skips |
| Sync identity, nullable FK, deny-list and settled-shift regressions | **22 passed** |
| Legacy stamp loyalty under its own edition settings | **23 passed**, 2 edition-specific tests skipped |
| Delivery customer app contract tests | **20 passed** |
| Delivery production build | Passed |
| Django system checks, cloud and local | No issues |
| Migration drift, cloud and local | No changes detected |
| Git diff checks, all changed worktrees | Passed |
| Impeccable static UI detector for checkout/loyalty | No findings |

The PostgreSQL runs include actual concurrent requests: two callers claiming one table, two identical waiter create commands returning one order, and two identical reward commands returning one redemption and the exact same response. READY/CREATED/instant stock timing, one-time reversal, and cashier tender/shift regressions were also exercised.

Commands (run in the matching repository, with the isolated test database environment when indicated):

```bash
# Cloud, PostgreSQL
python -m pytest -q smartfood/tests admins/tests/checkout admins/tests/analytics/test_business_day_and_analytics.py
# Local, PostgreSQL
python -m pytest -q waiters/tests customers/tests/orders customers/tests/payments alpha_pos_core/stock/tests/inventory alpha_pos_core/core/shifts/tests alpha_pos_core/discounts/tests
python -m pytest -q waiters/tests/test_waiter_hardening.py
# Shared core, its own pytest configuration
python -m pytest -q base/tests/sync/test_waiter_identity.py base/tests/sync/test_sync_fk_clearing.py base/tests/sync/test_sync_denylist.py base/tests/sync/test_sync_settled_shift_guard.py
python -m pytest -q notifications/tests/test_loyalty.py
# Each backend
python manage.py check --settings=config.settings_test
python manage.py makemigrations --check --dry-run --settings=config.settings_test
# Delivery app
npm test
npm run build
```

The deployed server image passed isolated API/ledger/permission/worker/checkout/stock checks, and the production health, CORS, worker state, source hashes, and read-only audit passed. The in-app browser was unavailable, so mobile/desktop visual inspection was not completed. Frontend build, contract tests, and the static detector passed. Native Windows GUI and restaurant peripherals still need operator verification. These checks do not prove that all future defects are impossible.

The updated source is in the server branch `fix/loyalty-waiter-hardening-2026-09-12`, local branch `fix/waiter-loyalty-hardening-2026-09-12`, and delivery-app branch `fix/loyalty-hardening-2026-09-12`. Shared-core files were applied to each pinned edition without importing unrelated cloud warehouse/Treasury changes into the desktop core. The standalone core checkout mirrors the local core changes. Implementation changes are committed and pushed under MythicalCosmic. Release revisions are listed below; the original revision evidence at the bottom records the pre-remediation base commits.


## Final deployment review — 12 September 2026

The full review found and fixed two additional runtime issues before release:

- Local courier assignment lifecycle queries attempted an unscoped PostgreSQL
  row lock across a nullable courier join. Assignment/accept/decline/ready/delivery
  now use the existing cloud implementation's assignment-only lock helper,
  preserving the Order → Assignment → Courier lock order. All affected courier
  regression cases passed after the fix.
- A stock failure could mark the transaction for rollback and then continue to
  query the next product. Deduction now stops at the failure and rolls back the
  whole order. Real first-line and last-line failures are covered.

Django operator forms now validate non-negative loyalty rates and expiry from
1 to 3650 days, matching the API. No actual financial records were edited to make
validation pass. A PDF export failure in the local test environment was caused
by the missing declared `reportlab==5.0.1` dependency; all export checks passed
once that dependency was installed. Test fixtures were updated for the shared
waiter service and to isolate embedded-database tests from PostgreSQL test env vars.

| Final verification run | Result |
|---|---|
| Full cloud suite on PostgreSQL | 740 passed; one missing test dependency resolved and rechecked below |
| All export tests after dependency installation | 28 passed |
| Full local suite after courier fix | 672 passed, 8 Windows-specific skips; one test fixture corrected and rechecked below |
| Final waiter + side-effect rollback + stock timing/requirements | 70 passed, no skips |
| Final complete Smart Food suite including config form validation | 204 passed, no skips |
| Shared sync/notifications/shifts/stock/discount regression suite | 506 passed, 2 edition-specific skips |
| Stock inventory after rollback fix | 52 passed |
| Delivery app | 20 tests passed; production build passed |
| Frozen Windows executable under Wine | SELFTEST OK, including embedded PostgreSQL, migrations, health and mock fiscal/sync |
| Desktop bundle and signed archive | 453 core source files matched; no private configuration files |
| Isolated built server image | Checkout, stock, loyalty HTTP replay/cancel, permissions, settings, worker checks passed |
| Deployed server image | 689 runtime/migration source-file hashes matched |
| Production read-only audit | Zero findings across all 10 checks; waiter setting remains disabled |

Counts overlap and must not be summed as unique cases. All original failures
were investigated and resolved; the table retains the full-run outcomes and
names the focused rechecks rather than hiding the earlier failures.

| Component | Built/deployed commit |
|---|---|
| server | `b43af510891bec9af058e383b1ed01ebaaeb6411` |
| cloud_core | `7048af52a5a79163a745a18ee3f7ee1ec458f4ca` |
| desktop | `c8870f4991e487693c9522f52fd76c0b519b56a7` |
| desktop_core | `e0b4c5ff3ce6ee1e58732c27fde7d0e982f61855` |
| delivery_app | `24c66a779755bd5362556077b43759bc5cf61672` |

Cloud public health returns `ok b43af510891bec9af058e383b1ed01ebaaeb6411`.
All five application services use the same verified image; API and customer-app
health checks return 200. CORS preflight passed. Production environment bytes,
PostgreSQL container, and Redis container were preserved. Scoped runtime-setting
caches were refreshed. The dispatch worker runs the new loyalty maintenance.

A protected database/environment backup and six rollback image tags were retained
at `/root/alphapos-release-20260912-1.0.45` on the POS server. Additive migrations
permit application-image rollback without rewriting financial history.

The restaurant support tunnel still reports desktop **1.0.41**. Server deployment
does not install the local waiter fixes on that machine. Install desktop 1.0.45
using the existing Windows account after checkout stops, preferably after shift
close; verify the cashier frontend is at least Smart POS 0.0.11, then verify
cash/card checkout, waiter handoff, and sync queue drain.

### Desktop 1.0.45 publication

[Download the Windows installer](https://control.78.111.91.113.nip.io/updates/installers/AlphaPOS-1.0.45-Setup.exe).
The signed update feed also advertises 1.0.45. Target metadata, snapshot, and
timestamp are version 6 and expire on 2026-10-12 at 13:29:37 UTC. The trusted
root is unchanged. The target was staged and hash-checked before promotion;
timestamp metadata was promoted last. No signing keys or restaurant configuration
were uploaded. The temporary local signing-key link was removed.

| File | Bytes | SHA-256 |
|---|---:|---|
| `AlphaPOS-1.0.45-Setup.exe` | 79,962,570 | `a181f066417414c6754c2547699d759e00beca4dfc6778d5c48ef609eebae0ea` |
| `AlphaPOS-1.0.45.tar.gz` | 111,829,264 | `5e3bfc37f42cfd5b4a0636510d715196e91068f8b04939f2fb2a7e4a881e622c` |


Both complete public downloads passed: the Windows TUF client verified the signed
update from the bundled trust root, and clients retaining version-5 metadata
advanced to version 6. The HTTPS installer download matched its full byte length
and SHA-256; its public manifest and checksum matched as well. The canonical
local update repository now matches the published metadata.

Structured release evidence is retained under `.release-builds/1.0.45/`.
The original audit below describes pre-fix behavior, not the deployed release.

## Original audit assessment (before remediation)

The systems have strong transaction, locking, payment-evidence, refund, and synchronization foundations, but the loyalty and waiter domains are not ready for large feature expansion without a short hardening phase.

The most urgent confirmed defects are:

1. Smart Food checkout can debit more loyalty points than the discount needs.
2. The same Smart Food sale can qualify for two independent loyalty programs: Smart Food points and the legacy phone-based stamp program.
3. Reward redemption has no durable idempotency identity, so a sequential retry can issue a second reward.
4. Cashier settlement replaces the waiter stored in `Order.cashier_id`; waiter history and statistics also use `cashier_id`, so a settled sale disappears from the waiter who created it.
5. The waiter and shared POS `mark ready` actions accept illegal backward state transitions and omit the stock status transition.
6. A configured stock deduction point of `READY` can therefore leave waiter/POS orders without stock deduction. An all-instant order is especially clear: it is saved as READY but stock is told that the new status is PREPARING.
7. Table assignment is not locked or validated as a single active ownership operation. Two waiters can create active orders for the same table, and either order can later mark the table available while the other is still active.
8. `AppSettings.waiter_enabled` is informational only. Disabling it does not block login or waiter API operations.
9. A waiter session can call the shared POS payment endpoint and other broad staff endpoints even though the waiter permission template does not grant `order.pay` or `discount.apply`. This conflicts with the dedicated “request payment from cashier” workflow.
10. Waiter create/add operations are not safely retryable unless the client happens to provide a key on create; add-item has no idempotency protection at all.
11. A waiter can use the shared POS read APIs to enumerate every order and search customer names, phone numbers, Telegram IDs, spend totals, order histories, and favorite products. The dedicated waiter API otherwise presents itself as personal-order scoped.
12. The general status endpoint has no complete transition matrix. Except for CANCELED as a source state, it can move completed or already-paid work back to PREPARING or READY.
13. The stock configuration exposes a nonexistent CREATED transition and a product-level `deduct_on_status` value that the actual order deduction engine never consults.

These are software defects or unresolved authorization/product contracts. They are not evidence of restaurant staff misconduct.

## System map

### Smart Food loyalty

The customer delivery web app talks to the cloud server:

- `GET /api/smartfood/loyalty`
- `GET /api/smartfood/rewards`
- `POST /api/smartfood/rewards/{reward_id}/redeem`
- `GET /api/smartfood/redemptions`

The cloud backend owns:

- the cached integer point balance on `smartfood.Customer`;
- the append-only `LoyaltyTransaction` history;
- point reservations on order creation;
- point earning after verified POS settlement;
- reward inventory and `Redemption` codes;
- manager-facing member lookup, manual grant, scan award, and fulfillment APIs.

### Legacy stamp loyalty

The shared `notifications` domain contains a second loyalty engine keyed by normalized phone number:

- fixed stamps per completed paid order;
- a stamp balance in `LoyaltyAccount`;
- one `OrderLoyaltyCredit` per order;
- stamp redemption through the legacy notification/customer flow.

It is independent from Smart Food points. There is no shared policy or exclusion that prevents a Telegram/Smart Food order from earning both systems.

### Waiter system

Waiter operational APIs exist only in the restaurant desktop/local backend, mounted under `/api/waiters/`. The cloud server deliberately does not mount waiter routes. The cloud receives replicated orders, items, payments, shifts, users, tables, and places through the sync synchronization system.

Dedicated waiter routes include:

- login, logout, current identity, session list/revoke, and a disabled local password-change response;
- active menu products and categories;
- places and tables;
- personal order list/detail;
- create order, add/update/remove line, mark ready, request cashier payment, and cancel;
- apply/remove discount and secret-word validation;
- personal date-range statistics;
- venue configuration/capability flags.

Waiters can also reach many shared root POS routes because those accept the `WAITER` role. The effective capability is broader than `/api/waiters/` alone.

### Effective waiter capability matrix

| Area | Dedicated waiter surface | Additional effective shared-POS capability |
|---|---|---|
| Identity | Login/logout, current user, list/revoke own sessions | Shared session token is accepted by other role-gated POS routes |
| Menu | Read categories/products | Read the normal POS category/product catalog |
| Floor | Read places/tables; change a table served by the waiter | Table status is synchronized, but no locked occupancy invariant exists |
| Orders | Create and read own tickets; add/update/remove lines; mark ready; request payment; cancel | Read every order; change own order status/type/details/courier/items; kitchen/client displays |
| Customers | No dedicated waiter customer database | Search all customers and read customer identity, contact, spend, history, and favorite-product data |
| Discounts | Apply/remove discount; validate secret word | Same operations through shared POS routes despite missing permission key |
| Payments | Dedicated flow only requests cashier collection | Shared route can settle own order and write payment/drawer evidence with an active waiter shift |
| Shifts | No routes under `/api/waiters/` | Shared start/current/end shift endpoints admit WAITER |
| Reporting | Personal operational/sales summary | Shared live order/KDS views; no reliable waiter commission model |

There is no waiter web/mobile frontend in the inspected workspace. The implemented waiter product is currently a backend API plus tests and request examples; a future client would have to choose carefully between the dedicated and shared surfaces unless the backend boundary is tightened first.

## Smart Food loyalty findings

### Critical: excess point debit at checkout

`CheckoutView.vue` requests the customer’s full point balance when “use points” is enabled. `cart_service.py` caps the money discount at the merchandise subtotal but leaves `points_used` unchanged. Order creation then debits that full value.

Production configuration currently values one point at 50 UZS. Example: a customer with 500 points pays a 10,000 UZS merchandise subtotal. Only 200 points are needed, but the current calculation can debit all 500. The extra 300 points disappear without value.

Evidence:

- `alpha_pos_server/smartfood/services/cart_service.py`, calculation around lines 113-168
- `../alpha-pos-delivery-app/src/views/CheckoutView.vue`, `pointsUsed` calculation
- `alpha_pos_server/smartfood/services/order_service.py`, atomic point reservation/debit

Required direction: compute and persist the minimum points required for the capped discount, using integer/Decimal arithmetic and an explicit rounding rule.

### High: two loyalty engines can credit one sale

Smart Food points settle after a linked POS order has concrete payment evidence. The legacy notification loyalty service separately credits stamps for a completed paid order with a phone number. The legacy service does not exclude `order_origin=TELEGRAM` and does not require the same concrete tender/`paid_at` evidence as Smart Food settlement.

No duplicate credit has been observed in the current production rows, because the legacy stamp tables are still empty. The code path remains possible while both configurations are enabled.

Evidence:

- `alpha_pos_server/smartfood/services/loyalty_settlement_service.py`
- `alpha_pos_server/alpha_pos_core/notifications/services/loyalty_service.py`
- `alpha_pos_server/admins/services/order_service.py`

Required product decision: choose one loyalty ledger for Smart Food customers, then explicitly exclude those orders from the other engine or migrate to one unified ledger.

### High: reward redemption is not idempotent

Redemption correctly locks the reward and customer and is atomic under concurrency. However, neither the API nor the persistence model has a client/business idempotency key. A network timeout followed by a normal retry can issue another code and consume points/stock again if enough remain.

Evidence:

- `alpha_pos_server/smartfood/services/loyalty_service.py`
- `alpha_pos_server/smartfood/urls.py`
- `../alpha-pos-delivery-app/src/api/endpoints.js`

### High: loyalty ledger can drift after late cancellation

The ledger writer clamps the cached balance to zero but records the originally requested negative delta. If points earned by an order are spent before that order is later canceled, reversing the earn can exceed the remaining balance. The cached balance becomes zero while the signed transaction arithmetic implies a negative balance.

The settlement timestamp guards make ordinary earn/reverse replay idempotent, but they do not define debt/clawback behavior for already-spent points.

Evidence: `alpha_pos_server/smartfood/services/loyalty_settlement_service.py`.

Required product decision: allow negative point debt, cap the reversal and record a shortfall, or block/correct the dependent reward redemption.

### High: operational loyalty endpoints are too broad

Member lookup, scan award, manual grant, and reward fulfillment use a broad manager gate. They do not have narrow loyalty permissions, durable idempotency, or a complete business audit identity.

Specific issues:

- scan award has no receipt/order identity, so retries can award twice;
- manual grant accepts coercible values through `int()` and does not require a reason;
- invalid non-finite scan amounts can reach Decimal handling as server errors;
- grant/scan responses can contain a correct top-level balance but a stale nested member balance;
- the delivery admin frontend currently has no operational UI for these endpoints.

Evidence: `alpha_pos_server/smartfood/views/admin_loyalty_views.py` and `alpha_pos_server/smartfood/services/loyalty_service.py`.

### Medium: reward lifecycle is incomplete

- `Redemption.Status.CANCELED` existed, but the operational API had no cancel/refund operation. Django admin did have a cancellation action; that action bypassed the hardened service and was replaced by the audited API workflow.
- Reward stock is consumed when a code is issued and is not restored by a supported cancellation path.
- Active redemption codes have no expiry.
- Historical redemption snapshots preserve name, kind, and point cost, but not the promised product identity or discount amount. Editing a reward later can change or obscure what an old code represented.
- Free-delivery rewards are currently economically worthless in production because the delivery fee and free-delivery threshold are both zero.

### Medium: API and client fidelity issues

- `points_per_uzs` is named as if it were points earned per UZS, but its value means UZS required per point.
- Decimal configuration values are serialized as integers, which truncates fractional configuration.
- The app normalizer drops transaction kind, signed point delta, reason, and `balance_after`, so customers cannot audit their own history clearly.
- Loyalty/reward load failures are silently swallowed by the client.
- Point spending is all-or-nothing in the current checkout UI.
- The earn rule uses merchandise subtotal before point discount and excludes delivery/tip. Whether a points-funded purchase should earn new points is not documented as a product rule.
- Loyalty configuration and reward catalog are global singletons rather than branch/tenant scoped.

## Current production loyalty evidence

Read-only production inspection found:

- Smart Food loyalty enabled;
- currency UZS;
- earn rate: 1 point per 1,000 UZS;
- redemption value: 50 UZS per point, equivalent to a 5% base return before reward effects;
- delivery fee 0 and free-delivery threshold 0;
- 4 active unlimited rewards, including a 30-point free-delivery reward and discount/custom rewards at 50, 80, and 100 points;
- 11 customers, 1 customer with points, 500 cached points total;
- 3 Smart Food loyalty transactions and 0 reward redemptions;
- 3 bot orders: 2 dispatched and 1 rejected, with no point spending;
- no current cached-balance versus ledger mismatch;
- legacy stamps enabled at 1 stamp per order and 10 stamps per reward, with 0 accounts and 0 credits;
- no loyalty exceptions found in the reviewed seven-day production logs;
- Smart Food migrations 0001-0007 and notification migrations 0001-0013 applied.

Production evidence describes the current database state only; it does not make the code defects safe.

## Waiter identity, authentication, and lifecycle

### Existing strengths

- Only an active `WAITER` account can use the dedicated waiter login.
- Local branch authorization accepts global/cloud identities but rejects identities tied to a different concrete branch.
- Login rate limiting exists by both source IP and normalized email.
- Sessions use 256-bit random tokens; only SHA-256 token hashes are stored.
- Session expiry is seven days.
- Every authenticated request rechecks deletion, active status, expiry, user-agent binding, and courier-token audience separation.
- A waiter can inspect and revoke their own sessions.
- Credentials are cloud managed; the local password-change endpoint fails closed.
- Login/logout performs best-effort HR attendance check-in/check-out.

### High: waiter feature toggle is not enforced

`AppSettings.waiter_enabled` is returned by `GET /api/waiters/venue-config`, but it is not checked by waiter login, waiter authentication, or waiter service operations. A direct API client remains fully operational when the feature is disabled.

Evidence:

- `alpha_pos_local/waiters/services/waiter_service.py`
- `alpha_pos_local/waiters/services/auth_service.py`
- workspace-wide `waiter_enabled` usage search

### High: role gates bypass the permission model

The default waiter permission template grants only `order.create` and `order.update`. The waiter routes and shared POS routes mostly use hard-coded role checks and never call `permission_required`.

Consequences:

- a waiter can apply/remove discounts although `discount.apply` is absent;
- a waiter can call the shared `POST /orders/{id}/pay` endpoint although `order.pay` is absent;
- a waiter can cancel through shared order paths although `order.cancel` is absent;
- changing a waiter’s stored permission list does not reliably reduce these effective powers.

This is a confirmed enforcement mismatch. The final allowed capability still needs a product decision.

### High: shared POS reads expose branch-wide order and customer data

The dedicated waiter API scopes its order list and detail to the logged-in waiter. The shared POS API does not preserve that boundary:

- `GET /orders` treats WAITER as staff and applies no ownership filter unless the caller chooses one;
- `GET /orders/{id}` lets a WAITER read any order;
- `GET /clients?q=...`, `/clients/lookup`, and `/clients/{id}` admit WAITER by role;
- those client responses include name, phone, Telegram ID, lifetime spend, recent order history, and frequently purchased products;
- the shared client display, chef display, and courier list also admit WAITER.

Mutation services apply a waiter ownership check, but read services do not. This is a real privacy and least-privilege gap, not just a mismatch in documentation.

Evidence:

- `alpha_pos_local/customers/views/order_views.py`
- `alpha_pos_local/customers/services/order_service.py`, `get_all_orders` and `get_order_by_id`
- `alpha_pos_local/customers/views/client_views.py`
- `alpha_pos_local/customers/services/client_service.py`

### Medium: shift and attendance clocks are inconsistent

- Waiter login starts HR attendance but does not start a POS shift.
- Waiter logout ends HR attendance but deliberately leaves a POS shift active.
- Waiter order creation does not require an active waiter shift.
- The same waiter can therefore have orders outside a shift, an active shift after attendance checkout, or attendance without a shift.

This may be deliberate for availability, but it prevents shift reports and attendance from forming one dependable work-session record.

## Waiter order and table findings

### Critical: cashier settlement erases waiter attribution

Waiter order creation stores the waiter in both `Order.user_id` and `Order.cashier_id`. Personal waiter lists, ownership checks, stats, table counts, shift analytics, notifications, local sales reports, and much of cloud analytics use `cashier_id`.

The shared payment service changes `cashier_id` to the staff member who collected payment. This is correct for drawer settlement, but it destroys the only field that those waiter features use as waiter ownership.

After a cashier pays a waiter-created ticket:

- it disappears from the waiter’s dedicated order list;
- waiter stats lose the sale;
- waiter ownership checks fail on that order;
- analytics credit the cashier with service activity as well as money collection;
- cloud sync receives the reassigned cashier identity;
- the original waiter remains only indirectly in `Order.user_id`, which existing waiter reports do not use.

The two route families then disagree: the dedicated waiter ownership check uses only `cashier_id` and rejects the settled order, while the shared POS mutation check accepts either `cashier_id` or `user_id` and can still let the waiter change it if they know the order ID. The same session can therefore be denied by `/api/waiters/...` and accepted by `/orders/...` for the same ticket.

Evidence:

- `alpha_pos_local/waiters/services/order_service.py`, create/list/ownership logic
- `alpha_pos_local/waiters/services/waiter_service.py`
- `alpha_pos_local/customers/services/order_service.py`, payment around lines 1411-1661
- `alpha_pos_local/alpha_pos_core/core/shifts/service.py`

Required direction: preserve separate immutable service attribution (`created_by`/`waiter`) and settlement attribution (`collected_by`/cashier/shift). Do not overload one foreign key with both meanings.

### Critical: table ownership is race-prone and can become false

Order creation loads a table without a row lock and does not require it to be active, available, or in an active place. There is no database constraint limiting a table to one live order.

Two concurrent waiter requests can both create orders for the same table. Removing the final line or canceling either order unconditionally marks the table AVAILABLE, even if another active order still exists.

Additional contract gaps:

- HALL orders do not require a table;
- DELIVERY/PICKUP orders can be assigned a table/place;
- a RESERVED or OUT_OF_SERVICE table can be used by direct ID;
- `GET /tables?place_id=` includes inactive tables;
- table status updates do not lock the table row;
- an admin-role session admitted by the route cannot create through the waiter service because the service itself only accepts a WAITER.

Evidence: `alpha_pos_local/waiters/services/order_service.py` and `alpha_pos_local/alpha_pos_core/base/repositories/table.py`.

### High: illegal order-state transitions

The dedicated waiter `mark_ready` action rejects only CANCELED and treats READY as replay. It could move OPEN or COMPLETED back to READY. A paid PREPARING ticket moving forward to READY is valid, because a customer may pay before kitchen preparation finishes. The shared POS `mark_order_ready` implementation has the same missing terminal-state guard.

This can rewrite operational history, reset readiness time, and send duplicate kitchen/ready notifications after settlement.

The general `PATCH /orders/{id}/status` path does call the stock transition handler, but it also lacks a full transition graph. It accepts PREPARING, READY, or CANCELED from every source state except CANCELED. Consequently, a completed or paid order can be moved back to PREPARING/READY, and an OPEN order can skip directly to READY. CANCELED is the only terminal source explicitly protected. State validation must be centralized so the waiter-specific and shared endpoints cannot diverge.

### High: READY stock transition is missing

The waiter and shared POS `mark_ready` methods change database status but do not call `OrderStatusHandler.on_status_change`. Stock settings allow `deduct_on_order_status=READY`, so that supported configuration never deducts stock through these endpoints.

All-instant order creation has a related defect: it saves the real order as READY, then invokes stock transition with `new_status=PREPARING`. A READY-configured installation therefore skips deduction immediately as well.

The default setting is PREPARING, which masks this bug on installations that never changed it. READY remains an explicitly accepted setting and must work.

Two related configuration defects make the contract less trustworthy:

- the settings service accepts `deduct_on_order_status=CREATED`, but the actual `Order.Status` enum uses OPEN and no create path reports a CREATED transition;
- every product-stock link exposes its own `deduct_on_status`, and `ProductStockLinkService.should_deduct()` implements it, but the production order deduction engine uses only the global `StockSettings.deduct_on_order_status`; no non-test caller invokes that product-level decision helper.

The first option can suppress all sale deduction. The second makes an administrator believe a per-product timing policy is active when it is not.

### High: retry safety is incomplete

- Create is protected only when the client supplies `Idempotency-Key`; otherwise duplicate requests execute normally.
- Create does not persist a deterministic business action ID in the order. A process crash after order commit but before response-cache persistence can allow a later stale-claim retry to create another order.
- Add-item has no idempotency decorator or operation identity. A network retry can add the same quantity twice.
- Update quantity and request-payment are naturally same-result operations; mark-ready is explicitly replay safe only once the order is already READY.
- Cancel relies on an optional idempotency key; the service returns an error for an already canceled order, although stock reversal itself has a durable full-reversal marker.

### Medium: menu visibility can be bypassed by direct product ID

The menu product list filters soft-deleted products but does not exclude products whose category is inactive. Order creation/add-item uses `ProductRepository` directly and has no explicit visible-menu/category-active validation. A client can submit a known product ID even when it is absent from the intended active category list.

Prices are server authoritative and frozen onto order lines, which is correct.

### Medium: order shape and payload rules are incomplete

- HALL/table and DELIVERY/PICKUP/table consistency is not enforced.
- Order `description` and line `detail` are unbounded TextFields at the service boundary.
- Invalid stats dates silently become today, and reversed ranges are silently swapped, hiding client mistakes.
- Waiter list status accepts unknown values and silently ignores the filter through the shared repository rather than returning a field error.

## Payments, discounts, refunds, and drawers

### Existing strengths

- Payment locks the order and active collecting cashier shift.
- Payment input is normalized into concrete tender lines and rejects malformed, non-finite, non-positive, insufficient, and invalid non-cash overpayment.
- Split payment has a stable checkout action UUID and database uniqueness.
- Cash drawer credit uses only the physical cash portion, net of change.
- Payment, tender rows, drawer credit, stock transition, and order header commit atomically.
- Paid cancellation records an append-only refund rather than deleting the original sale.
- Refund and stock reversal failures roll the cancellation transaction back.
- Discount application locks the order and discount, checks eligibility/limits, clamps totals, and recalculates after item edits.
- Discount removal is blocked after payment.
- Secret-word checking has IP and per-order throttling.

### High: waiter can bypass cashier handoff and take payment directly

The dedicated waiter API offers only `request-payment`, whose documentation says it does not take payment. However, `customers.views.order_views.pay_order` includes `WAITER` in `STAFF_ROLES`, and ownership explicitly allows a waiter to settle their own order. With an active waiter shift, a waiter token can create payment evidence and affect the drawer directly.

The venue-config response also advertises payment methods and split-payment capability, although no waiter-specific pay route exists. This may have been intended, but it contradicts the current handoff wording and the default permission catalog. The product must choose one policy and the backend must enforce it consistently.

### Medium: discount audit and permission semantics are incomplete

Discount rows record `applied_by`, and usage rows exist, but waiter apply/remove actions do not create a dedicated high-level audit event. The waiter route authorizes by role rather than the `discount.apply` permission. Apply is naturally protected from the same discount being applied twice after the order lock, but removal retries return not-found rather than a stable replay response.

## Waiter statistics and reporting

Current personal stats provide:

- date range;
- orders created;
- paid count;
- active and canceled count;
- distinct tables served;
- gross settled sales;
- refund total;
- net sales.

They intentionally use creation time for operational counts, payment time for revenue, and refund time for reversals. Business-day cutover is respected.

Confirmed issues:

- all metrics are keyed to `cashier_id`, so cashier settlement removes the order from waiter reporting;
- `paid_count` subtracts current-window cancellation refunds from current-window paid orders. Canceling today an order paid in an earlier period can produce a negative paid count;
- the response mixes created-order and settled-money populations without explicitly describing the different grains to the client;
- invalid date text is silently replaced with today;
- the cloud “kitchen shift” report defaults to role WAITER even though a CHEF role now exists, and per-item chef attribution is not tracked. This can mislabel waiter performance as kitchen performance.

There is no commission or sales-percentage model tied to waiters. HR supports employee profiles, fixed base salary, bonuses, deductions, attendance, contracts, reviews, goals, leave, and salary payments. Any waiter commission feature would require an explicit new accounting rule and reliable waiter attribution first.

## Synchronization and observability

### Existing strengths

- Orders, items, concrete payment lines, shifts, users, places, tables, discounts, stock movements, and related evidence are registered in the shared sync system.
- User credentials/roles are cloud managed and protected from branch privilege forgery.
- Order items created in bulk are stamped with branch identity so they enter synchronization.
- Payment lines are append-only and have durable logical uniqueness.
- Cloud reconciliation can repair a stale unpaid header from immutable payment evidence.
- Financial shift evidence has lock ordering, manifests, and post-close mutation guards.
- Local order mutation middleware records before/after request evidence and hashes sensitive identifiers.

### Gaps

- `payment_requested_at` is intentionally local-only, so the cloud cannot observe waiter-to-cashier queue timing.
- Waiter service attribution is overwritten before synchronization, so cloud analytics cannot reliably reconstruct it from `cashier_id`.
- Table status is mutable synchronized state, but occupancy is not derived from a locked active-order invariant; competing updates can replicate an incorrect final state.
- The HTTP evidence middleware covers URLs containing `/orders` or `/order/` and login. It does not cover table-status mutations or shift endpoints.
- Evidence storage is deliberately fail-open. A disk/evidence write failure is logged but does not block the operational action.

## Current production waiter evidence

Two production layers were inspected read-only: the cloud server and the restaurant desktop through its active loopback-only support tunnel.

### Cloud server

- `waiter_enabled` is false;
- 0 active/non-deleted WAITER users;
- 0 waiter-created/handled orders;
- 0 waiter shifts;
- 0 places and 0 tables.

### Restaurant desktop

- the tunnel health endpoint reports `desktop-1.0.41`;
- the reviewed local release checkout is `desktop-1.0.44`, so the restaurant is three desktop releases behind the code audited here;
- `waiter_enabled` is false;
- 0 WAITER users, 0 waiter-attributed orders, and 0 waiter shifts;
- 0 places and 0 tables;
- there are no duplicate-live-order table assignments, available tables with live orders, or occupied tables without live orders, because the table catalog is empty;
- stock settings currently have `stock_enabled=false`, `auto_deduct_on_sale=true`, `deduct_on_order_status=PREPARING`, `reserve_on_order_create=false`, and no default stock location;
- the database has base migration 0056 but stops at stock migration 0009; the current audited checkout also contains stock migration 0010.

The waiter defects have therefore not corrupted current waiter records: waiter service has not been provisioned at either layer. They remain release blockers for enabling waiter operations. The current restaurant stock configuration also masks the READY-deduction defect because stock is disabled, but it does not make the code path correct for later activation.

## Test evidence

Executed against the current local desktop checkout using the server Python environment:

- waiter suite: 31 passed;
- customer payment/order suites: 162 passed, 2 skipped because their lock-order tests require PostgreSQL;
- stock inventory suite: 48 passed, 2 skipped because row-lock concurrency requires PostgreSQL;
- shift suites: 75 passed;
- discount suites: 22 passed.

Earlier loyalty validation in this review:

- delivery web app: 16 passed;
- focused Smart Food loyalty backend: 10 passed;
- complete Smart Food backend: 171 passed, 1 SQLite concurrency test skipped;
- legacy loyalty: 23 passed, 2 edition-specific tests skipped.

Passing tests show that covered behavior is stable. They do not cover the critical cross-domain failures listed above. Missing focused tests include:

- excess point debit after discount cap;
- sequential reward redemption retry;
- late earn reversal after earned points were spent;
- Smart Food/legacy double-credit exclusion;
- waiter attribution before and after cashier settlement;
- waiter permission-list revocation versus role-only endpoints;
- waiter isolation from branch-wide order and customer-history reads;
- disabled waiter feature blocking login and writes;
- two concurrent waiters claiming one table;
- table remaining occupied while any active order remains;
- completed/canceled order rejected by mark-ready; paid PREPARING may advance to READY;
- READY-configured stock deduction, including all-instant orders;
- rejection or correct implementation of CREATED and per-product deduction settings;
- add-item retry identity;
- cross-period cancellation producing a non-negative, meaningful paid count;
- full waiter flows on PostgreSQL with real row locks.

## Recommended repair order

### Phase 0: lock the intended contracts

Decide and document:

1. one Smart Food loyalty engine versus deliberate dual rewards;
2. whether point-funded merchandise earns new points;
3. cancellation behavior when earned points have already been spent;
4. whether waiters may take payment or may only request cashier collection;
5. whether a table may have multiple concurrent tickets;
6. whether waiter work requires an active shift;
7. whether waiters or chefs own kitchen preparation metrics.

### Phase 1: stop financial and inventory loss

1. Debit only points that actually fund the capped discount.
2. Add durable redemption and award idempotency identities.
3. Separate waiter/service attribution from cashier/settlement attribution and backfill safely where evidence permits.
4. Enforce a legal order state machine on every transition surface.
5. Run the stock transition for actual READY status, including all-instant creation.
6. Remove the nonexistent CREATED setting or map creation consistently, and either enforce or retire per-product deduction timing.
7. Add table row locks plus one authoritative active-ticket policy.

### Phase 2: enforce access consistently

1. Enforce `waiter_enabled` at login and authenticated waiter operations.
2. Replace broad role-only mutation access with explicit permissions and ownership rules.
3. Scope shared order and customer reads so a waiter cannot enumerate unrelated records.
4. Remove or authorize the shared direct-payment path according to the product decision.
5. Add narrow loyalty permissions and mandatory audit reasons/identities for manual adjustments.

### Phase 3: repair history, UI contracts, and observability

1. Add complete immutable reward/redemption snapshots and cancellation/stock restoration.
2. Return complete signed loyalty history to the customer app.
3. Define strict date/filter validation and stable errors.
4. Extend local audit coverage to table and shift mutations.
5. Add a production-safe drift audit for loyalty balances, duplicated loyalty awards, waiter attribution, table occupancy, missing stock deductions, and shift attribution.

### Phase 4: only then build major loyalty changes

Once the shared identities and accounting rules are stable, larger loyalty features can safely add tiers, campaigns, partial point spend, expirations, targeted rewards, branch policy, better history, and admin operational tooling without multiplying existing ambiguity.

## Repository and revision evidence

At audit time:

- local desktop repository: `c5aa7f883573e53d23a1992faa40d8d9f30b7c78`, branch `release/desktop-1.0.44`;
- local shared core submodule: `fbbc9ec05bb8f5cb394a64f79c8948c531cf08b6`;
- cloud root and production revision: `cc02e7b8122f1e6246fac7a41009a95866ed2a12`;
- cloud shared core and production revision: `3ca5db7237b4e61a5c5066a7a1d96b8245f1321c`;
- delivery customer app local/production revision: `fe8079294bc0cd64bd7d7138fb613bf696047ff7`;
- delivery admin local revision inspected: `e3cf4c5`.

The initial audit changed only this report. The remediation and deployment evidence at the top supersede that initial read-only status. Original findings and baseline revision evidence are retained for traceability.
