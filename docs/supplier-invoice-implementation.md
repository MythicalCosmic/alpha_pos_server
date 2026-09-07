# Supplier invoice backend delivery — 2026-09-07

The backend implements the 2026-09-06 supplier-invoice contract through one atomic
receiving command. Stock and supplier payable are posted together using the
existing receiving, stock, batch, supplier, audit, and idempotency records.
Production rollout is pending the isolated image verification; the completed
rollout revision and health result will be appended below.

## Source and scope

- Core commit: `6393830abfd9d19298a688fd45122b9d358216bf` on `feature/core-cloud-direct-supplier-invoices`.
- Server integration branch: `feature/direct-supplier-invoices`.
- Frontend repository is unchanged. The admin frontend can consume the endpoints
  below; it must use the aggregate receive command rather than chain legacy calls.
- This delivery targets the cloud admin backend. Desktop 1.0.44 remains the current
  desktop release; the new invoice editor belongs in the admin frontend.

## What was corrected

Legacy PO/receiving paths included soft-deleted child rows, accepted unparsed date
strings before arithmetic, dropped nested supplier-item identity, and could return
an error after creating earlier aggregate rows. The shared services now exclude
those rows explicitly, parse dates first, retain and validate supplier links,
reject invalid values and catalog relationships, and roll back aggregate failures.

Direct invoices use an internal `DIRECT_INVOICE` PO and the existing receiving
completion engine. Internal POs stay out of ordinary planning lists. Posted
receivings, line snapshots, and stock/supplier ledger evidence are immutable.
Older terminal copies cannot rewrite direct invoice aggregates through sync.

## Routes and permissions

All paths below start with `/api/admins/stock`.

| Method | Path | Required permission |
|---|---|---|
| GET | `/suppliers/{supplier_id}/receivable-items/` | `stock.supplier.view` and `stock.catalog.view` |
| GET | `/purchase-invoices/` | `stock.purchase_invoice.view` |
| POST | `/purchase-invoices/receive/` | `stock.purchase_invoice.receive` |
| GET | `/purchase-invoices/{invoice_id}/` | `stock.purchase_invoice.view` |
| POST | `/purchase-invoices/{invoice_id}/reverse/` | `stock.purchase_invoice.correct`, Manager/Admin only |

Both POST commands require `Idempotency-Key`. Warehouse receives view/receive;
Manager receives view/receive/correct. Admin retains its existing permission
bypass within the resolved operational branch. No broad `stock.manage` grant is
required. Warehouse receives no supplier-payment, Treasury, expense, direct stock
adjustment, catalog-edit, or permission-management grant from this delivery.
Balances are included only with `stock.supplier.balance.view`.

StockUnit is an existing global catalog. Active global units are usable by all
branches; supplier links, item-specific conversions, stock items, destinations,
and invoices must belong to the actor's operational branch. This installation's
location permission model is branch-wide; no location-assignment table exists.

## Accounting and validation

Only UZS is accepted. Normal purchase prices are positive whole UZS integers;
free goods require zero price, `is_free=true`, and a reason. Quantities obey unit
precision and model limits. Boolean, empty, exponent, non-finite, negative, and
overflowing numeric inputs produce field-level errors. Batch and expiry rules,
active catalog membership, future invoice dates, duplicate lots, normalized
supplier invoice numbers, and declared totals are validated under locks.

Each line is `quantity × invoice unit price`, rounded half-up to one UZS using
Decimal. The invoice is the sum of these rounded lines. Base quantity uses a real
unit conversion; `pack_size` never changes stock implicitly. Stock movement value
is the exact rounded line value even when its four-decimal base-unit cost would
multiply back to a slightly different value.

For the requested example, 10 kg at 80,000 plus 5 kg at 100,000 becomes 15 kg at
86,666.6667 average cost. Latest item cost and supplier price become 100,000, and
supplier payable increases by exactly 500,000. Manual `cost_price` stays unchanged;
`avg_cost_price` and `current_inventory_cost_uzs` expose current inventory cost.
Recipe costing already uses the average-cost basis.

Costs are updated once per grouped item. Positive existing stock with an
unproven cost basis fails with `STOCK_COST_BASIS_MISSING`. A verified zero-cost
basis from a prior free invoice is supported. No costs, prices, or balances are
invented to repair missing history.

Supplier prices are selected from non-reversed paid invoice lines ordered by
invoice date, actual posting time, invoice ID, and lot ID. Backdated invoices
preserve newer suggestions; free lines preserve known prices. Changes of 30% or
more require explicit confirmation and a reason. The supplier price source uses
the receiving UUID to avoid a circular catalog/receiving sync dependency.

Posting creates one PURCHASE_IN per lot and one supplier PURCHASE row, including
an all-free invoice with zero payable. It creates no Expense or Treasury movement.
All document, stock, batch, cost, price, payable, audit, and replay writes share one
transaction. Injected final-line, supplier-ledger, price-update, and audit failures
leave the database unchanged.

## Retry and correction behavior

Idempotency is scoped by actor, branch, operation, and target/create identity.
Canonical JSON sorts keys and normalizes numeric representation; a stored payload
hash detects changed requests. The response and completed claim commit with the
business writes. Same-key replay returns the original status/body and IDs. The
only security exception is removal of fields/actions whose permission has since
been revoked. Database uniqueness protects posting identity, normalized supplier
numbers, and invoice supplier-ledger references.

Reversal requires a reason and its own key. It appends compensating stock and
supplier transactions, preserves the original manifest, and recomputes the latest
non-reversed supplier price. Reservations, insufficient lots, missing evidence,
and later outbound/deleted movements block reversal with affected item/batch
information. Replenishment after consumption alone does not prove reversibility.
This is intentionally conservative for inventory without lot attribution.
A replacement invoice may use `replaces_invoice_id` to link the reversed original.

## List/detail and API evidence

Filters run before pagination: number search, supplier, location, status, invoice
and posting date ranges, stock item, creator, and poster. List totals cover the
filtered set. Ordering is stable. Query tests bound both catalog and invoice lists
to five database queries at one and twelve rows. Detail uses immutable snapshots
and includes movement IDs, action history, reversal/replacement links, and allowed
actions. New invoice APIs return unformatted numeric JSON and Tashkent timestamps.

These examples were executed against synthetic test data, not restaurant accounts:

- [Successful request](supplier-invoices/evidence/example-request.json)
- [Posted response](supplier-invoices/evidence/posted-response.json)
- [Exact replay response](supplier-invoices/evidence/exact-replay-response.json), byte-identical to the posted response
- [Warehouse supplier-payment rejection](supplier-invoices/evidence/warehouse-payment-forbidden.json), HTTP 403
- [Validation results](supplier-invoices/evidence/validation.json)

## Migrations

1. `base.0065_direct_supplier_invoices`: audit action choices.
2. `stock.0018_direct_supplier_invoices`: document metadata, immutable snapshots,
   source/known-price state, actor/reference fields, indexes, and uniqueness.
3. `stock.0019_direct_invoice_backfill`: historical PO supplier identities,
   supplier-item identities, explicit price-state backfill, and additive role grants.

The exact imported unknown-price marker was confirmed by a read-only production
query on 103 September links:

> Purchase price was not provided; 0 is a temporary unknown-price placeholder to replace on the first purchase.

The backfill changes no numeric price, stock quantity, cost, supplier balance,
ledger amount, or supplier invoice number. A later paid invoice removes only that
exact marker from notes while preserving other text. Zero without a known normal
price is never suggested as a normal price.

Run `python manage.py audit_direct_invoice_migration` for the read-only impact
report; it also works before the new columns exist. Run
`python manage.py makemigrations --check --dry-run` for model/migration drift.
The PostgreSQL upgrade test compares financial values before/after and confirms
that the report itself makes no accounting changes.

## Verification

| Check | Result |
|---|---|
| Cloud core full suite | 1,288 passed; 17 skipped |
| Server full suite | 695 passed; 4 skipped |
| Focused PostgreSQL contract and receiving regressions | 113 passed |
| Final posted-record immutability follow-up on PostgreSQL | 99 passed |
| System checks and migration drift, both editions | Passed; no changes detected |
| Runtime lint | Zero findings |

Core skipped tests require PostgreSQL or the server edition. The focused
PostgreSQL run includes six tests with independent database connections: same-key
posting, different actors sharing a supplier number, opposite item order across
suppliers, same/different-key reversal races, and reversal racing a sale.

The 18 requested test groups are covered in `stock/tests/purchase_invoices/`, plus
existing `stock/tests/test_warehouse_receiving.py` and
`stock/tests/test_money_control_contract.py`. They cover one/multiple lines,
weighted and converted costs, price knowledge/free goods, confirmation, lots,
invalid/scoped input, PASSED-only posting, ledger uniqueness, no financial-account
mutation, rollback, replay/concurrency, permissions, soft deletion, safe/blocked
reversal, legacy receiving, migrations, and bounded list queries.

Commands use an isolated environment without production credentials:

```sh
python3 .implementation-work/supplier-invoices-2026-09-07/run_checks.py core-cloud tests
python3 .implementation-work/supplier-invoices-2026-09-07/run_checks.py server tests
python3 .implementation-work/supplier-invoices-2026-09-07/run_checks.py core-cloud targeted invoices-final-pg stock/tests/purchase_invoices stock/tests/test_warehouse_receiving.py stock/tests/test_money_control_contract.py --postgres
```

In a configured checkout, the equivalent focused command is:

```sh
python -m pytest -q stock/tests/purchase_invoices stock/tests/test_warehouse_receiving.py stock/tests/test_money_control_contract.py
```

Use a disposable PostgreSQL test database for the concurrency tests. The private
production keys and database backups are never included in these artifacts.
