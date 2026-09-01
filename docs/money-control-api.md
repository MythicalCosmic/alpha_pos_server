# Money Control backend contract

Date: 2026-09-01
Timezone: `Asia/Tashkent`
Currency: whole UZS

## Owner reads

### `GET /api/admins/money-control/overview`

Requires `money.control.view`.

Query parameters are `date_from`, `date_to`, and optional `location_id`.
Dates are inclusive business dates, default to the current business date, and
may span at most 366 days. An unauthorized location returns
`403 LOCATION_FORBIDDEN`.

The response contains Treasury, unsettled physical drawer cash, supplier
payable and credit, raw-material weighted-average inventory, canonical expense
status totals, completeness issues, and reconciliation state. The snapshot is:

```text
SAFE + BANK + DRAWER_UNRECONCILED + RAW_INVENTORY
- SUPPLIER_PAYABLE + SUPPLIER_CREDIT
```

It is named working capital and is not profit, revenue, net worth, or spending
authority. Any unsafe component is `null`; the API does not replace missing or
ambiguous accounting evidence with zero.

### `GET /api/admins/stock/inventory-control/`

Requires `stock.inventory_control.view`.

Supported filters are `item_type`, `location_id`, `category_id`,
`include_descendants`, `search`, `low_stock`, `page`, and `per_page`. Summary
values always cover the complete filtered result before pagination. With no
location filter, one row is returned per item across authorized locations.

Inventory value uses perpetual weighted-average cost:

```text
inventory_value_uzs = quantity * avg_cost_price
available_value_uzs = (quantity - reserved_quantity) * avg_cost_price
```

Quantities and unit costs are canonical four-place decimal strings. New UZS
properties are JSON integers. Totals aggregate unrounded `Decimal` values and
round only at serialization with `ROUND_HALF_UP`.

## Canonical expense workflow

The canonical category routes are:

- `GET|POST /api/admins/expense-categories`
- `PATCH /api/admins/expense-categories/{id}`
- `POST /api/admins/expense-categories/{id}/deactivate`

The canonical workflow routes are:

- `GET|POST /api/admins/expenses`
- `GET /api/admins/expenses/{id}`
- `POST /api/admins/expenses/{id}/approve`
- `POST /api/admins/expenses/{id}/reject`
- `POST /api/admins/expenses/{id}/pay`
- `POST /api/admins/expenses/{id}/cancel`
- `POST /api/admins/expenses/{id}/void`

Example create request:

```json
{
  "category_id": 17,
  "amount_uzs": 350000,
  "requested_source": "BANK",
  "expense_date": "2026-08-31",
  "description": "August internet invoice",
  "receipt_number": "INV-2026-08-991"
}
```

Creation moves no money. Another authorized reviewer approves it, then payment
atomically debits its approved source and links exactly one money posting.

```http
POST /api/admins/expenses/441/pay
Idempotency-Key: expense-441-bank-v1
Content-Type: application/json
```

```json
{
  "source_account": "BANK",
  "fee_percent": "1.5",
  "note": "Paid from business bank account"
}
```

`fee_percent` uses base-10 `Decimal` and `ROUND_HALF_UP`. Bank fees are included
in the canonical operating expense total (`amount_uzs + fee_uzs`). SAFE and
DRAWER reject non-zero fees with `FEE_BANK_ONLY`.

`POST /api/admins/treasury/expense` remains only as an idempotent adapter for
`expense.direct.pay`. It requires a canonical `category_id` and uses the same
workflow and source validation. Desktop cashbox category routes are aliases to
the canonical catalog; they do not create a second category identity.

## Supplier receiving and payment

Receiving completion posts only `PASSED` lines. It converts every accepted line
to the item's base unit, snapshots the conversion and base-unit cost, and
atomically posts stock, moving-average cost, PO received quantity, supplier
ledger debt, and the authoritative supplier balance. `PENDING` quality blocks
completion; `FAILED` stays out of available stock and payable value.

Funded supplier payments use:

```http
POST /api/admins/stock/suppliers/12/payments/
Idempotency-Key: supplier-12-two-receivings-v1
Content-Type: application/json
```

```json
{
  "amount_uzs": 1200000,
  "source_account": "BANK",
  "fee_uzs": 12000,
  "allocation_mode": "EXPLICIT",
  "allocations": [
    {"purchase_order_id": 130, "amount_uzs": 700000},
    {"purchase_order_id": 145, "amount_uzs": 500000}
  ],
  "note": "Supplier transfer for two deliveries"
}
```

Principal reduces payable; the fee only increases the BANK debit. Allocations
cannot exceed proven completed-receiving principal. The compatibility
`/suppliers/{id}/pay/` URL uses this same service. The old unfunded PO payment
method returns `410 UNFUNDED_PAYMENT_ROUTE_RETIRED`.

## Shift settlement

Manager reconciliation is the only Treasury recognition boundary. CASH posts
to SAFE. CARD, UZCARD, HUMO, and PAYME post to BANK. Configured provider methods
post only after an explicit BANK classification. Unknown methods block
the complete posting, and zero values remain only in the immutable manifest.

Each response contains one posting per non-zero tender with its destination and
Treasury transaction ID. A retry returns the same posting; changed settlement
evidence returns `409 SETTLEMENT_POSTING_CONFLICT`. Inkassa remains a physical
collection action and never recognizes revenue again.

## Filtered histories

`GET /api/admins/treasury/history` accepts `account`, `type`, `date_from`,
`date_to`, `category_id`, `reference_type`, `reference_id`, `performed_by_id`,
`search`, `page`, and `per_page`. Totals cover the complete filtered ledger.

`GET /api/admins/stock/suppliers/{id}/ledger/` accepts `type`,
`source_account`, `date_from`, `date_to`, `reference_type`, `reference_id`,
`search`, `page`, and `per_page`. Filtered principal totals are separate from
the supplier's current authoritative balance.

Malformed filters return field-level `422` errors and are never ignored.

## Error and idempotency contract

New endpoints use this envelope:

```json
{
  "success": false,
  "code": "EXPENSE_ALREADY_PAID",
  "message": "This expense has already been paid.",
  "errors": {"status": ["Expected APPROVED but found PAID."]},
  "details": {"expense_id": 441}
}
```

`Idempotency-Key` is mandatory for settlement, receiving completion, supplier
payment/reversal, expense pay/void, Treasury transfer, and direct-pay adapters.
Keys are scoped by actor, branch, operation, and target. The same canonical
request replays its stored response; changed input returns
`409 IDEMPOTENCY_KEY_REUSED`.

The machine-readable examples are in
`docs/openapi-money-control.yaml` and
`postman/Alpha_POS_API.postman_collection.json`.
