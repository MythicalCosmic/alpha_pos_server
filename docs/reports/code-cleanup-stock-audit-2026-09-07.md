# Alpha POS structure, performance, and stock audit — 7 September 2026

**Audit checkpoint: findings recorded before application changes.** This pass follows AUD-001–AUD-020 and focuses on stock correctness, database work, unused service code, and maintainable module boundaries. It is not a claim of perfect software or a full production reconciliation.

All work uses isolated Git worktrees and synthetic data. Test processes block external network connections and do not inherit production configuration. The original active repositories start clean and will be updated to the final tested commits and left clean again. No production deployment or installer publication is part of this pass.

## Baseline and scope

| Component | Starting commit | Working branch |
| --- | --- | --- |
| Desktop core | `8451faa3361ca1ca8cce9db2ad0a1dfac094b9ba` | `audit/core-desktop-cleanup-2026-09-07` |
| Cloud core | `30be48bb7489d04c6cc8f97ae0cda09cd2843d73` | `audit/core-cloud-cleanup-2026-09-07` |
| Desktop app | `35f88c9a1af00760785cc7ae10ac62f7891976e8` | `audit/cleanup-2026-09-07` |
| Server | `0e0519e22f75c808b1104606ceba459982c021de` | `audit/cleanup-2026-09-07` |

Reviewed stock availability/reservations, recipes and versioning, unit conversion, inventory lists/filters, stock insight reads, and service entry points. Structural inventory also covered the three application repositories. The stock models are already grouped by domain; moving Django model declarations or historical migrations is unnecessary for these improvements. Keep public service imports and edition-specific financial/branch behavior compatible.

The desktop and cloud core variants differ in stock item, level, purchasing, and supplier code. Apply targeted edits to those modules independently; do not overwrite one edition with the other. Recipe, unit, production, and order-stock modules match at the baseline.

## Confirmed findings

### AUD-021 — Stock availability and reservations use inconsistent units and independent demands

Order deduction correctly passes the selected unit into stock adjustment, but availability compares the raw selected-unit quantity against a base-unit balance, and reservation passes the selected-unit quantity to a base-unit-only operation. A recipe requiring 0.1 kg per sold product can therefore appear available with only 50 g and reserve 0.1 g instead of 100 g.

Order availability also checks products independently, and recipe availability checks repeated ingredient lines independently. Two demands for the same stock item can each pass against the same available balance despite their combined shortage.

**Reproduction:** kilogram/gram availability, reservation/release, insufficient-reservation rollback, shared ingredients across products, and repeated recipe ingredients. Five failures and one location-isolation control pass in `test_order_requirements.py`.

**Intended fix:** normalize requirements to base units once, aggregate demand by stock item, and use the same units in availability and reservation. Preserve transactional reservation rollback and location scoping. Actual deductions retain their existing unit-aware write path.

### AUD-022 — Inventory filter arguments are ignored and low-stock totals include deleted balances

The item list accepts `low_stock_only` but checks only `low_stock`, while its HTTP view supplies `low_stock_only`. The level list accepts `search` but never filters on it. Low-stock aggregation does not exclude deleted stock levels or limit balances to the requested location. This can hide a shortage using stock from another location or an obsolete balance.

**Reproduction:** ignored low-stock alias/location, deleted level inflation, and ignored level search all fail. Keep the accepted filter spellings but apply a single implementation, with live levels and requested location scope.

### AUD-023 — Inventory and recipe reads grow database work with every row

Inventory list serialization performs a level query and a second aggregate for each item. Recipe availability repeatedly reloads stock items, unit conversions, base units, and available balances per ingredient.

| Synthetic operation | One row | Twenty rows | Target |
| --- | ---: | ---: | --- |
| Inventory page with location balances | 4 queries | 42 queries | At most 3 queries |
| Recipe availability | 10 queries | 124 queries | At most 7 queries, including transaction savepoints |

**Intended fix:** prefetch levels for the selected page, derive totals from the returned rows, batch unit metadata/overrides, and aggregate available quantities once. Reuse loaded recipe children/cost data within a read operation. No process-wide stock or price cache: subsequent requests must see changed inventory and conversion data.

### AUD-024 — Recipe edits and creation do not preserve their stated lifecycle

A failed child insertion during recipe creation returns an error without rolling back the recipe and previously inserted ingredients. An approved recipe edit attempts to reuse its globally unique code for a new version and raises a database integrity error. Draft output-quantity edits are silently ignored. Parent traversal has no cycle guard; malformed ancestry can repeatedly query forever. Version cloning also uses unfiltered child relations, which can bring deleted ingredients back.

**Reproduction:** partial-create rollback, approved-version creation, draft output update, and a bounded ancestry-cycle reproduction fail. The cycle test deliberately stops after repeated parent reads to avoid hanging a worker.

**Intended fix:** roll back failed recipe creation, validate/apply supported draft changes, serialize new-version allocation on the root recipe, generate a distinct bounded code, clone live children, and reject cyclic/excessively deep ancestry with a normal error. Retain the approved recipe until its new version is explicitly activated/approved.

### AUD-025 — Unit factors accept invalid or unstorable values

Unit creation/update accepts zero, negative, boolean, malformed, non-finite, or out-of-capacity conversion factors. Some return success with invalid metadata; others raise database/decimal exceptions. Very small factors can round to zero at the six-decimal storage precision.

**Reproduction:** nine invalid factors across create/update produce 18 failures; a valid fractional-factor control passes.

**Intended fix:** validate a positive finite factor using the model's precision/capacity before writes. Return field-level validation errors and preserve existing rows after rejection. Share conversion arithmetic between single and batched reads without changing the existing missing-metadata fallback contract.

### AUD-026 — Unwired service methods and a monolithic recipe service obscure maintained paths

Tracked-source identifier scans and route/dispatcher inspection found obsolete, unwired stock helpers: recipe scaling; purchase/production creation from low stock; bulk addition of pending receiving items; unused production schedule/actual-quantity helpers; duplicate item-unit add/remove services; and a supplier-balance setter that bypasses the maintained ledger flow. Remove only the methods whose entry points are confirmed absent, and record their exact names/line counts in the completion evidence.

The 1,137-line recipe service contains catalog/version lifecycle, costing/availability, ingredient/substitute editing, by-products, and steps. Split these responsibilities into a `stock/services/recipes/` package, keeping a small compatibility module for existing imports. Remove unused bookkeeping left behind by older save paths and imports made obsolete by deletion.

Framework callbacks, registered Telegram handlers, HTTP server methods, desktop JavaScript bridge methods, and pytest fixtures can have no direct Python reference while still being live. Those static-scan candidates are explicitly excluded. Comments explaining payment, sync, and inventory invariants are useful maintained documentation, not dead code.

## Evidence before fixes

The initial stock regression run contains **16 failures and one passing control**; the unit-factor run contains **18 failures and one passing control**. These are parameterized cases of the findings above, not 34 independent defects. XML records the baseline SQL counts.

Local evidence directory: `.audit-work/2026-09-07-cleanup/evidence/`. Key files: `baseline-commits.json`, `dead-code-candidates.json`, `unreferenced-method-candidates.json`, `reproduce-stock-core-desktop.xml`, and `reproduce-unit-factors-core-desktop.xml`.

## Completion

Pending: implement and verify the findings, record final query counts and removed code, run both editions' stock/payment/sync suites, publish source/report under MythicalCosmic, and synchronize the original active folders to the final clean commits. Runtime baseline is Python 3.13.14, Django 6.0.3, with SQLite and isolated PostgreSQL 17 testing.
