# Profitability API Contract

**Scope:** backend API for the existing frontend  
**Base path:** `/api/admins/finance/profitability`  
**Authorization:** authenticated Alpha POS administrator  
**Money format:** decimal strings (for example, `"125000.00"`)  
**Date format:** ISO `YYYY-MM-DD`; period-close input uses `YYYY-MM`

No frontend files are part of this implementation.

## Accounting contract

The API reports accounting profit and known account movement separately:

```text
net sales = paid order total − refunds
gross profit = net sales − COGS
net profit = gross profit − operating expenses + approved other income
known net movement = known settlement inflows − known payment outflows
```

- Sales use `Order.paid_at`; refunds use `OrderRefund.refunded_at`.
- Restaurant business dates use the existing Asia/Tashkent `07:00–03:00` reporting window.
- COGS prefers exact `StockTransaction` cost linked to an `OrderItem`.
- When exact stock cost is unavailable, the API uses a verified effective-dated standard cost or an explicit verified zero-COGS treatment.
- An inventory purchase is cash movement and inventory value, not an immediate P&L expense.
- Payroll is accrued to its work month and prorated by calendar day for partial periods.
- Recurring monthly expenses are prorated by calendar day.
- Owner draws, capital expenditure, inventory purchases, and non-business movements remain outside operating profit.
- A refund reverses COGS only when a structured physical inventory-return event exists.

## Report lifecycle

The `status` field has three possible values:

- `NOT_STARTED`: the entire requested range predates the configured launch date.
- `PROVISIONAL`: a live/open report calculated from current source rows.
- `FINAL`: an immutable closed-period snapshot.

`GET /api/admins/finance/profitability?from=2026-08-15&to=2026-08-31`

Optional query parameters:

- `branch_id`: required when the server cannot resolve one configured/default branch.
- `live=true`: recalculate a previously closed exact range from current evidence instead of returning its final snapshot.
- Existing canonical range aliases `date_from` and `date_to` are also accepted.

Successful response shape:

```json
{
  "success": true,
  "data": {
    "status": "PROVISIONAL",
    "branch_id": "branch1",
    "range": {
      "from": "2026-08-15",
      "to": "2026-08-31",
      "effective_from": "2026-08-15",
      "effective_to": "2026-08-31",
      "timezone": "Asia/Tashkent",
      "partial_launch_period": true
    },
    "summary": {
      "gross_sales": "0.00",
      "refunds": "0.00",
      "net_sales": "0.00",
      "cogs": "0.00",
      "gross_profit": "0.00",
      "payroll": "0.00",
      "rent": "0.00",
      "utilities": "0.00",
      "operating_expenses": "0.00",
      "waste_spoilage": "0.00",
      "finance_fees": "0.00",
      "depreciation": "0.00",
      "taxes": "0.00",
      "total_operating_expenses": "0.00",
      "other_income": "0.00",
      "net_profit": "0.00",
      "net_margin_pct": "0.00"
    },
    "cash_flow": {
      "known_inflows": "0.00",
      "known_outflows": "0.00",
      "known_net_movement": "0.00",
      "sales_by_tender": {},
      "refunds_by_tender": {}
    },
    "breakdown": {
      "expenses": [],
      "cost_sources": [],
      "actual_inventory_return_credit": "0.00",
      "product_revenue": "0.00",
      "product_refunds": "0.00",
      "non_product_revenue": "0.00",
      "orders": 0,
      "refund_events": 0
    },
    "coverage": {
      "costed_revenue_pct": "0.00",
      "missing_cost_products": [],
      "can_close": false,
      "blockers": []
    },
    "source_policy": {},
    "generated_at": "2026-08-15T12:00:00+05:00"
  }
}
```

The frontend must treat `coverage.can_close` and `coverage.blockers` as the authoritative close readiness result. It must not infer readiness from a displayed percentage.

## Setup read model

`GET /api/admins/finance/profitability/setup`

Returns the current configuration, reporting-group choices, product list, product-cost profiles, recurring costs, adjustments, HR/cashbox categories, candidate drawer payouts, and recent closed periods. This is the single bootstrap endpoint for an existing frontend settings page.

Drawer payouts are paginated so every historical blocker remains discoverable:

- `payout_page`: positive page number (default `1`).
- `payout_page_size`: `1`–`200` rows (default `100`).
- `payout_status`: `all` or `unresolved`.

The response includes `cashbox_expenses_pagination` with the page, total row and
page counts, and next/previous flags. Clients clearing close blockers should use
`payout_status=unresolved` and continue until `has_next` is false.

`PATCH /api/admins/finance/profitability/setup`

```json
{
  "branch_id": "branch1",
  "reporting_start_date": "2026-08-15",
  "payroll_confirmed_through": "2026-08-31",
  "fixed_costs_confirmed_through": "2026-08-31"
}
```

The reporting start date cannot be changed after a period has been closed.

## Product cost profiles

- `GET /api/admins/finance/profitability/product-costs`
- `POST /api/admins/finance/profitability/product-costs`
- `PATCH /api/admins/finance/profitability/product-costs/{id}`

Verified standard cost:

```json
{
  "product_id": 42,
  "treatment": "STANDARD",
  "standard_unit_cost": "18500.0000",
  "effective_from": "2026-08-15",
  "effective_to": null,
  "note": "Recipe cost checked by manager",
  "verified": true
}
```

Explicit zero COGS:

```json
{
  "product_id": 99,
  "treatment": "ZERO",
  "effective_from": "2026-08-15",
  "verified": true
}
```

Effective periods for the same branch/product may not overlap. `STANDARD` requires a positive four-decimal unit cost. `ZERO` is an explicit accounting decision, not a silent fallback.

## Recurring costs

- `GET /api/admins/finance/profitability/recurring-costs`
- `POST /api/admins/finance/profitability/recurring-costs`
- `PATCH /api/admins/finance/profitability/recurring-costs/{id}`

```json
{
  "name": "Restaurant rent",
  "reporting_group": "RENT",
  "monthly_amount": "12000000.00",
  "start_date": "2026-08-01",
  "end_date": null,
  "is_active": true,
  "note": "Current lease"
}
```

Only P&L expense groups are accepted for recurring costs.

## One-off adjustments

- `GET /api/admins/finance/profitability/adjustments`
- `POST /api/admins/finance/profitability/adjustments`
- `DELETE /api/admins/finance/profitability/adjustments/{id}` (drafts only)
- `POST /api/admins/finance/profitability/adjustments/{id}/approve`

```json
{
  "effective_date": "2026-08-20",
  "direction": "EXPENSE",
  "reporting_group": "OPERATING",
  "amount": "250000.00",
  "description": "Verified invoice missing from source ledger"
}
```

New adjustments are `DRAFT` and do not affect profit until approved. Income adjustments are automatically assigned to `OTHER_INCOME`.
Drafts may be deleted; approved adjustments are immutable.

## Expense classification

`PUT /api/admins/finance/profitability/categories/{cashbox|hr}/{category_id}`

```json
{"reporting_group": "UTILITIES"}
```

Valid expense classifications include P&L groups, `INVENTORY_PURCHASE`, `CAPITAL_EXPENDITURE`, `OWNER_DRAW`, `NON_BUSINESS`, and `REVIEW`. `OTHER_INCOME` is accepted only through an income adjustment, not through an expense category.

`PUT /api/admins/finance/profitability/cashbox-expenses/{expense_id}/classification`

```json
{
  "reporting_group": "PAYROLL",
  "represented_elsewhere": true,
  "cash_movement_represented_elsewhere": false,
  "note": "Payroll accrual is in HR; this row is the actual drawer payout"
}
```

- `represented_elsewhere=true` excludes this row from P&L because the accrual already exists in HR, recurring costs, or another ledger.
- `cash_movement_represented_elsewhere=true` excludes this row from known movement because the same payment exists in another payment ledger.
- These controls are independent so an accrual can be deduplicated without losing the real drawer movement.
- A non-empty audit note is required when either deduplication flag is true.

## Period close

`POST /api/admins/finance/profitability/periods/close`

```json
{
  "branch_id": "branch1",
  "period": "2026-09",
  "correction_reason": ""
}
```

Only completed calendar months may close. The activation month closes from the configured reporting start through month-end. A close fails with `422` while any evidence blocker remains.

Repeating an unchanged close is idempotent and returns the existing revision with HTTP `200`. Changed evidence after a close requires a non-empty `correction_reason`; the API appends a new immutable revision and returns HTTP `201`.

## Error contract

Validation and accounting-readiness failures return HTTP `422`:

```json
{
  "success": false,
  "message": "The period still has unresolved accounting evidence",
  "errors": {
    "blockers": [
      {
        "code": "MISSING_PRODUCT_COSTS",
        "message": "Every sold product needs actual cost, a verified standard cost, or explicit zero COGS.",
        "count": 3,
        "amount": "850000.00"
      }
    ]
  },
  "data": {"report": {}}
}
```

Authentication failures follow the existing admin API contract (`401`/`403`). Unsupported methods use Django's standard `405` response.

## Recommended frontend call sequence

1. Load `GET .../setup`.
2. Save activation and confirmation dates with `PATCH .../setup`.
3. Resolve sold products without cost coverage.
4. Classify expense categories and individual drawer exceptions.
5. Add recurring costs and approve legitimate one-off adjustments.
6. Render `GET ...?from=...&to=...` using server-returned money strings.
7. Enable close only when `coverage.can_close` is true.
8. Close the completed month and retain the returned revision metadata.
