# Warehouse Operator and Operational Audit API

Implemented 2026-08-28. All routes below use the normal Admin Panel session,
the standard `{success, message, data, errors}` response envelope, and backend
permission checks. Unauthenticated requests return `401`; authenticated users
without the named permission return `403`. Money values are numeric whole UZS
and all returned operational datetimes are ISO-8601. Attendance calculations use
`Asia/Tashkent`, which is also returned explicitly in attendance responses.

## Role and permission assignment

`WAREHOUSE` is an Admin Panel role and is excluded from the POS staff picker.
Its default permission template is:

`stock.catalog.view`, `stock.level.view`, `stock.batch.view`,
`stock.supplier.view`, `stock.purchase.view`, `stock.receiving.create`,
`stock.receiving.update_draft`, `stock.receiving.complete`,
`stock.transfer.view`, `stock.transfer.create`, `stock.count.view`,
`stock.count.create`, `stock.count.record`, and `stock.adjustment.request`.

Audit permissions and reserved expense-request permissions are individually
assignable in Admin user management. No expense-request permission is assigned
by the Warehouse template. Existing direct supplier-payment, cash, expense,
settings, and stock-adjustment routes remain forbidden to Warehouse users.

## Stock and purchasing

All routes are below `/api/admins/stock/`.

| Method | Route | Permission |
| --- | --- | --- |
| GET | `items/`, `items/{id}/`, `units/`, `locations/` | `stock.catalog.view` |
| GET | `levels/` and level detail routes | `stock.level.view` |
| GET | `batches/`, `transactions/` | `stock.batch.view` |
| GET | `suppliers/`, `suppliers/{id}/`, `suppliers/{id}/ledger/` | `stock.supplier.view` |
| GET | `purchase-orders/`, `purchase-orders/{id}/` | `stock.purchase.view` |
| GET/POST | `purchase-order/{po_id}/receiving/` | view / `stock.receiving.create` |
| GET/POST | `receiving/{id}/items/` | view / `stock.receiving.update_draft` |
| PATCH/DELETE | `receiving-items/{id}/` | `stock.receiving.update_draft` |
| POST | `receiving/{id}/complete/` | `stock.receiving.complete` |
| POST | `receiving/{id}/approve-over-receipt/` | `stock.receiving.approve_over` |
| POST | `receiving/{id}/corrections/` | `stock.receiving.create` |
| POST | `receiving-corrections/{id}/approve|reject/` | `stock.receiving.correct.approve` |
| GET/POST | `transfers/` | `stock.transfer.view` / `stock.transfer.create` |
| GET/POST | `counts/` | `stock.count.view` / `stock.count.create` |
| POST | `counts/{id}/record/`, `counts/{id}/submit/` | `stock.count.record` |
| GET/POST | `adjustment-requests/` | own requests / `stock.adjustment.request` |

Create a receiving item with the PO line and actual values:

```json
{
  "po_item_id": 42,
  "quantity_received": "5.0000",
  "unit_cost": 100000,
  "batch_number": "B-20260828",
  "expiry_date": "2027-08-28",
  "quality_status": "PASSED",
  "notes": ""
}
```

`quality_status` is `PASSED`, `FAILED`, or `PENDING`. Completion rejects pending
quality, missing required batch/expiry data, fractional UZS totals, and an
unapproved aggregate over-receipt. Failed lines are retained as evidence but do
not post usable inventory or supplier debt. The successful completion response
is replayed on a safe retry:

```json
{
  "success": true,
  "data": {
    "receiving_id": "receiving-uuid",
    "status": "COMPLETE",
    "supplier_id": "supplier-uuid",
    "supplier_balance_before_uzs": 0,
    "supplier_balance_after_uzs": 500000,
    "posted_at": "2026-08-28T17:30:00+05:00",
    "timezone": "Asia/Tashkent"
  }
}
```

Supplier ledger filters are `date_from`, `date_to`, `transaction_type`,
`source_reference` (or `reference`), `page`, and `per_page`. Ledger rows expose
numeric `debit_uzs`, `credit_uzs`, `change_uzs`, `balance_after_uzs`, source,
date, and creator.

## Attendance, schedules, excuses, and discipline

All routes are below `/api/admins/hr/`.

| Method | Route | Permission |
| --- | --- | --- |
| GET | `work-schedules/` | `attendance.view` |
| POST/PATCH | `work-schedules/`, `work-schedules/{id}/` | `attendance.schedule.manage` or `discipline.rule.manage` |
| GET | `attendance/`, `attendance/summary/` | `attendance.view` |
| POST | `attendance/manual-entry/` | `attendance.record` |
| POST | `attendance/{id}/adjustment-requests/` | `attendance.adjust.request` |
| POST | `attendance-adjustments/{id}/approve|reject/` | `attendance.adjust.approve` |
| POST | `attendance/{id}/excuses/` | `attendance.adjust.request` |
| POST | `attendance-excuses/{id}/approve|reject/` | `attendance.adjust.approve` |
| GET/POST | `discipline-rules/` | view / manage permissions |
| PATCH | `discipline-rules/{id}/` | `discipline.rule.manage` |
| GET/POST | `discipline-cases/` | view / create permissions |
| POST | `discipline-cases/{id}/approve|reject/` | `discipline.case.approve` |
| POST | `discipline-cases/{id}/void/` | `discipline.case.void` |

Manual attendance accepts the payload in the specification. The server rejects
non-`Asia/Tashkent` offsets, duplicate daily entries, invalid overnight times,
and unauthorized future entries. It snapshots the applicable schedule and
returns integer `scheduled_minutes`, `worked_minutes`, `overtime_minutes`,
`late_minutes`, and `early_leave_minutes`.

The summary requires `date_from` and `date_to`, supports `employee_id`,
`branch_id`/`location_id`, attendance status, discipline category, penalty
status, `page`, and `per_page`, and includes totals for the entire filtered set.
Approved excuses stay separate from the measured variance and block an
overlapping final attendance penalty unless a manager resolves the conflict.

Disciplinary cases preserve rule code/title/category/amount snapshots. Approval
is atomic and idempotently creates or links one `SalaryDeduction`; if the payroll
period does not yet exist, the case remains `APPROVED_PENDING_PAYROLL` and is
attached once during generation. Paid payroll is never silently modified.

## Preparation audit

| Method | Route | Permission |
| --- | --- | --- |
| GET | `preparation-audits/`, `preparation-audits/{id}/` | `prep.audit.view` |
| GET | `preparation-audit-categories/` | `prep.audit.view` |
| POST | `preparation-audits/{id}/review/` | `prep.audit.review` |
| POST | `preparation-audits/{id}/reopen/` | `prep.audit.reopen` |
| GET | `audit-dashboard/` | `prep.audit.view` or `attendance.view` |
| POST | `audit-periods/close/` | `prep.audit.reopen` |

The central READY handler creates one immutable audit snapshot alongside the
existing idempotent Telegram update. Green records are `NOT_REQUIRED`; yellow
and red records remain `PENDING` until reviewed. Review comments are 10–1,000
characters. A repeated review returns the existing current review; a manager
must reopen it before replacement. Period close returns `409` with stable code
`pending_preparation_reviews` and `pending_review_count` while yellow/red items
remain pending.

List/dashboard filters include `date_from`, `date_to`, `branch_id` or
`location_id`, performance status, review status, category, creator/cashier,
responsible employee, `page`, and `per_page`. Dashboard output includes green,
yellow, red, pending-yellow, pending-red, categories, and completion totals.

## Mutation and conflict behavior

Mutating completion/review routes accept `Idempotency-Key`. A completed,
approved, or reviewed immutable record returns its original safe success where
possible, otherwise `409` with a stable error code. Self-approval is rejected
with `self_approval_forbidden`. Validation uses field-oriented `errors`; raw
exceptions are never returned.
