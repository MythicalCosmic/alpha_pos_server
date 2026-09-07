# Supplier opening debt and payments

The payment allocator previously accepted only receipt-backed purchase debt. A positive supplier balance entered directly in Django admin had no allocatable debt document, so the payment failed with `SUPPLIER_PAYMENT_ALLOCATION_INCOMPLETE` even when the displayed payable was sufficient.

The server now supports a reviewed opening debt in the existing append-only supplier ledger. Payments can allocate to that debt, received purchase orders, or both. An opening entry creates no inventory, purchase order, expense, Treasury movement, or cash movement. Paying remains a separate permission-gated command.

## Production review decision

On 2026-09-07, read-only investigation found eight positive supplier balances with no supporting supplier transaction, payment, purchase order, or receiving history. Django admin history showed direct balance edits. The owner instructed that these balances be left unchanged for review. This release does **not** approve them, create opening entries for them, or execute payments. Their payment attempts return `409 SUPPLIER_OPENING_BALANCE_REVIEW_REQUIRED` until an authorized reviewer registers verified debt.

`Supplier.current_balance` is now read-only in Django admin, including creation forms. Ordinary catalog updates must not be used to establish debt.

## Review and registration API

`GET /api/admins/stock/suppliers/{supplier_id}/opening-balance/`

Requires `stock.supplier.balance.view` in the supplier branch. Returns stored/ledger balances, reconciliation state, registered opening ID, unpaid opening principal, and allowed actions. Other-branch targets return 404. A supplier with existing inconsistent history requires reconciliation; the opening command cannot rewrite that history.

`POST /api/admins/stock/suppliers/{supplier_id}/opening-balance/`

Requires an active Manager or Admin with `stock.supplier.opening.manage` and an `Idempotency-Key`. The Manager permission template and existing non-deleted Manager accounts receive this permission. Warehouse and Cashier cannot register openings, even if mistakenly given the permission. Warehouse financial-payment permissions remain unchanged.

Example only — replace all values with reviewed evidence; this request was **not** executed against a restaurant supplier:

```json
{
  "amount_uzs": 5000000,
  "as_of_date": "2026-09-01",
  "mode": "RECONCILE_EXISTING",
  "reason": "Opening debt verified against supplier statement",
  "source_reference": "Statement 2026-09-01",
  "confirmed": true
}
```

`RECONCILE_EXISTING` requires the displayed balance to equal the reviewed amount exactly. It adds the missing initial ledger entry, starting at zero, while preserving the displayed balance. It never adds the amount twice. `CREATE` requires a zero balance and records newly reviewed opening debt. Both modes require no previous supplier-ledger or payment history, including deleted records. An existing opening cannot be replaced. Amounts must be positive whole UZS; dates must be valid and not future dates in Asia/Tashkent. Source reference, reason, reviewer, posting time, and canonical request hash are retained in immutable evidence.

A successful 201 response includes `opening_balance_id`, `supplier_transaction_id`, the reviewed amount/date, mode, before/after balance, source, reason, and reviewer. Same-key retries return the original status/body and IDs; changed data conflicts with `IDEMPOTENCY_KEY_REUSED`. Registration is atomic with its audit entry. Branch sync cannot author or rewrite protected opening entries.

## Paying reviewed debt

Existing `POST /api/admins/stock/suppliers/{supplier_id}/pay/` requests using `AUTO_OLDEST_DUE` work after registration. The allocator combines opening dates and purchase-order due dates, preserving the existing undated-order ordering. It cannot spend unsupported balances or overpay a debt. Received principal excludes reversed invoices and approved supplier returns.

Explicit allocations may identify exactly one `purchase_order_id` or `opening_balance_id` per row:

```json
{
  "amount_uzs": 3000000,
  "source_account": "BANK",
  "allocation_mode": "EXPLICIT",
  "allocations": [{"opening_balance_id": 123, "amount_uzs": 3000000}]
}
```

The normal payment idempotency key is still mandatory. Opening allocation responses add `allocation_type: "OPENING_BALANCE"` and `opening_balance_id`; their `purchase_order_id` is null. Existing purchase-order allocation responses retain their previous shape. Full payment reversal creates compensating supplier/Treasury entries and restores unpaid opening principal; it preserves the original opening entry.

Important errors include `SUPPLIER_OPENING_BALANCE_REVIEW_REQUIRED`, `SUPPLIER_LEDGER_RECONCILIATION_REQUIRED`, `SUPPLIER_BALANCE_CHANGED`, `SUPPLIER_OPENING_BALANCE_ALREADY_REGISTERED`, and `IDEMPOTENCY_KEY_REUSED`. The original allocation-incomplete error remains correct when verified debts cannot cover the requested principal.

## Upgrade and validation

Migration: `stock.0020_supplier_opening_balances`. It adds opening evidence to `SupplierTransaction`, an optional opening-debt target to `SupplierPaymentAllocation`, database uniqueness/exclusive-target constraints, and the Manager permission. Existing financial history and balances are unchanged. No parallel ledger is introduced.

Read-only migration impact and drift checks:

```bash
python manage.py audit_supplier_opening_migration
python manage.py makemigrations --check --dry-run
```

Focused tests cover registration, strict validation, approval/branch boundaries, partial/mixed payment, exact retries, rollback, invoice returns, synchronization protection, six real PostgreSQL races, and upgrade preservation. Run with isolated PostgreSQL settings:

```bash
pytest -q stock/tests/supplier_opening_balances stock/tests/purchase_invoices stock/tests/test_money_control_contract.py stock/tests/test_warehouse_receiving.py
```

This is a server API release; it does not require a desktop installer update. An admin-panel review form may call the documented registration endpoint after the amounts have been verified.

Validated core commit: `ec9769f7daf9f7ea873268ec3b38261d43622144`. Full suites: **1,337 core passed** (23 skipped) and **695 server passed** (4 skipped). Dedicated PostgreSQL runs: **55 opening**, **106 invoice**, **7 legacy money/receiving**, and **17 final integration/concurrency** tests passed; the last run repeats 11 integration cases after adding the read-only migration report. Schema drift and runtime lint checks passed. See [machine-readable validation](supplier-opening-validation.json).

## Deployed release evidence

Deployed **2026-09-07 21:57:41 Asia/Tashkent**. The public health endpoint returned HTTP 200 with the expected runtime revision. All five application services (`web`, `smartfood_dispatch`, `smartfood_messages`, `staff_notifications`, `bot`) run the verified image with zero restarts and zero startup tracebacks. Database/Redis containers and environment configuration were preserved; a fresh database backup and rollback images were retained.

- Runtime server commit: `88f518e1196db4309af25d09985e17e8c76ed2f5`.
- Core commit: `ec9769f7daf9f7ea873268ec3b38261d43622144`.
- Runtime image: `sha256:e3496043a4e729873ff7dfb59652fbec86750367103ae9314d834825dc5503f6`.
- Applied migration: `stock.0020_supplier_opening_balances`; no pending schema migrations.
- All existing columns across **17 accounting, stock, document, and audit tables** matched before and after the migration.
- **Eight supplier balances remain for review; zero live opening entries were registered.** No restaurant payment was executed for testing.
- The isolated HTTP check registered a synthetic 5,000,000 UZS opening without doubling its displayed balance, paid 3,000,000 UZS with a 15,000 UZS bank fee, replayed each request exactly, and reversed the payment to restore debt and bank balance. Warehouse registration was denied with HTTP 403. Invoice posting/reversal and checkout checks also passed.

See [deployment evidence](supplier-opening-deployment.json). Subsequent documentation commits do not change the runtime revision recorded above.

### Subsequent live activity — 22:01 Asia/Tashkent

A follow-up read-only check found a new direct invoice posted after deployment by an existing application user. Its purchase ledger entry exactly explains one supplier's balance increase, preserving the original opening amount as its `balance_before`. Seven original balances remain exactly unchanged; the eighth now includes that new invoice. No opening debt was registered and no balance correction or payment was executed by this deployment.

All eight original opening debts still require review. Seven suppliers have no ledger history and can use the documented registration command after approval. The supplier with the later invoice now has existing history and requires a reviewed ledger reconciliation; the opening command deliberately refuses to insert an opening into existing history. Its payment remains blocked with `SUPPLIER_LEDGER_RECONCILIATION_REQUIRED`. Do not erase the new invoice or rewrite its ledger to work around that check.
