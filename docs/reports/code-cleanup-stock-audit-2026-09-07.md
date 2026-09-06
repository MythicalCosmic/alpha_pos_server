# Alpha POS structure, performance, and stock audit — 7 September 2026

**Completed: AUD-021–AUD-027 fixed in both core editions.** This pass follows AUD-001–AUD-020 and covers stock correctness, query performance, unused helpers, and the recipe module structure. The initial audit checkpoint was committed as `8fdbb7b` before application edits. Additional endpoint findings were also recorded before their fixes. This is a tested cleanup, not a guarantee of perfect software or a production counter reconciliation.

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

**Implemented:** normalize requirements to base units once, aggregate demand by stock item, and use the same units in availability and reservation. Preserve transactional reservation rollback and location scoping. Actual deductions retain their existing unit-aware write path.

### AUD-022 — Inventory filter arguments are ignored and low-stock totals include deleted balances

The item list accepts `low_stock_only` but checks only `low_stock`, while its HTTP view supplies `low_stock_only`. The level list accepts `search` but never filters on it. Low-stock aggregation does not exclude deleted stock levels or limit balances to the requested location. This can hide a shortage using stock from another location or an obsolete balance.

**Reproduction:** ignored low-stock alias/location, deleted level inflation, and ignored level search all fail. Keep the accepted filter spellings but apply a single implementation, with live levels and requested location scope.

### AUD-023 — Inventory and recipe reads grow database work with every row

Inventory list serialization performs a level query and a second aggregate for each item. Recipe availability repeatedly reloads stock items, unit conversions, base units, and available balances per ingredient.

| Synthetic operation | One row | Twenty rows | Target |
| --- | ---: | ---: | --- |
| Inventory page with location balances | 4 queries | 42 queries | At most 3 queries |
| Recipe availability | 10 queries | 124 queries | At most 7 queries, including transaction savepoints |

**Implemented:** prefetch levels for the selected page, derive totals from the returned rows, batch unit metadata/overrides, and aggregate available quantities once. Reuse loaded recipe children/cost data within a read operation. No process-wide stock or price cache: subsequent requests must see changed inventory and conversion data.

### AUD-024 — Recipe edits and creation do not preserve their stated lifecycle

A failed child insertion during recipe creation returns an error without rolling back the recipe and previously inserted ingredients. An approved recipe edit attempts to reuse its globally unique code for a new version and raises a database integrity error. Draft output-quantity edits are silently ignored. Parent traversal has no cycle guard; malformed ancestry can repeatedly query forever. Version cloning also uses unfiltered child relations, which can bring deleted ingredients back.

**Reproduction:** partial-create rollback, approved-version creation, draft output update, and a bounded ancestry-cycle reproduction fail. The cycle test deliberately stops after repeated parent reads to avoid hanging a worker.

**Implemented:** roll back failed recipe creation, validate/apply supported draft changes, serialize new-version allocation on the root recipe, generate a distinct bounded code, clone live children, and reject cyclic/excessively deep ancestry with a normal error. Retain the approved recipe until its new version is explicitly activated/approved.

### AUD-025 — Unit factors accept invalid or unstorable values

Unit creation/update accepts zero, negative, boolean, malformed, non-finite, or out-of-capacity conversion factors. Some return success with invalid metadata; others raise database/decimal exceptions. Very small factors can round to zero at the six-decimal storage precision.

**Reproduction:** nine invalid factors across create/update produce 18 failures; a valid fractional-factor control passes.

**Implemented:** validate a positive finite factor using the model's precision/capacity before writes. Return field-level validation errors and preserve existing rows after rejection. Share conversion arithmetic between single and batched reads without changing the existing missing-metadata fallback contract.

### AUD-026 — Unwired service methods and a monolithic recipe service obscure maintained paths

Tracked-source identifier scans and route/dispatcher inspection found obsolete, unwired stock helpers: recipe scaling; purchase/production creation from low stock; bulk addition of pending receiving items; unused production schedule/actual-quantity helpers; duplicate item-unit add/remove services; and a supplier-balance setter that bypasses the maintained ledger flow. Remove only the methods whose entry points are confirmed absent, and record their exact names/line counts in the completion evidence.

The 1,137-line recipe service contains catalog/version lifecycle, costing/availability, ingredient/substitute editing, by-products, and steps. Split these responsibilities into a `stock/services/recipes/` package, keeping a small compatibility module for existing imports. Remove unused bookkeeping left behind by older save paths and imports made obsolete by deletion.

Framework callbacks, registered Telegram handlers, HTTP server methods, desktop JavaScript bridge methods, and pytest fixtures can have no direct Python reference while still being live. Those static-scan candidates are explicitly excluded. Comments explaining payment, sync, and inventory invariants are useful maintained documentation, not dead code.

## Evidence before fixes

The initial stock regression run contains **16 failures and one passing control**; the unit-factor run contains **18 failures and one passing control**. These are parameterized cases of the findings above, not 34 independent defects. XML records the baseline SQL counts.

Local evidence directory: `.audit-work/2026-09-07-cleanup/evidence/`. Key files: `baseline-commits.json`, `dead-code-candidates.json`, `unreferenced-method-candidates.json`, `reproduce-stock-core-desktop.xml`, and `reproduce-unit-factors-core-desktop.xml`.

### Additional read-path evidence before the remaining fixes

Six further cases reproduce five failures and one optional-ingredient control pass. Recipe detail serialization includes deleted substitutes and queries each ingredient separately. The stock snapshot still checks repeated mandatory ingredients independently; another location must not satisfy the shortage. Its recipe read uses 24 SQL queries for one ingredient and 43 for twenty after the first conversion optimization. These are additional read surfaces of AUD-021, AUD-023, and AUD-024. Batch live children/conversion metadata and reuse the already scoped stock balances before publishing the final snapshot.

### Catalog and dead-code follow-through

Before the final catalog changes, direct inspection shows that recipe list/search/item/version/detail/active-selection paths omit `is_deleted=False`. A deleted recipe can remain visible or be chosen for production. The six catalog-read regressions exercise these paths as part of AUD-024. Add consistent live-recipe filters; retain deleted versions only when allocating the next version number to avoid reusing numbers.

Removal of the unwired item-unit editing services also leaves `StockItemUnitRepository.unit_exists_for_item` and `clear_default` unused. `RecipeRepository.get_versions` and `get_next_code_seq` have no callers; the maintained service owns traversal/versioning. Confirm qualified references across the application repositories, then remove those four obsolete repository helpers under AUD-026.

### AUD-027 — Recipe read endpoints ignore location and do not validate multipliers

Before changing the HTTP views, inspection and endpoint tests show that recipe availability never forwards `location_id`, so a location-specific request can count stock from another kitchen. Cost and availability parse arbitrary text directly with `Decimal`, with no positive/finite/capacity checks: malformed, non-finite, negative, or extreme values can produce errors or meaningless requirements.

**Implemented:** validate a positive finite batch multiplier bounded by the existing maximum quantity, retain valid fractional batches, reject invalid explicit location IDs, and forward the selected location to availability. No location still means the existing aggregate view. HTTP regression cases cover both endpoints and the cross-location shortage.

## Completion


All seven findings are implemented. Availability and reservations normalize to base units and aggregate shared demand; filters use live, scoped data; recipe writes validate and roll back failed creation; version allocation locks the root; deleted recipe records are excluded from catalog reads; unit factors and recipe query parameters return validation errors before invalid calculations.

### Measured database work

| Synthetic read | Before | After | Rows |
| --- | ---: | ---: | ---: |
| Inventory page with location balances | 42 | 3 | 20 items |
| Recipe availability, including savepoints | 124 | 6 | 20 ingredients |
| Recipe details with children and cost | 27 | 6 | 20 ingredients |
| Stock insight snapshot | 43 | 11 | 20 ingredients |
| Stock insight snapshot with supplier catalog | — | 12 | 20 suppliers, 10 returned items each |

The first two baselines are from the original code. The detail/snapshot baselines were captured after the first unit-conversion batching change and before their own read fixes. One-row and twenty-row cases have the same final query counts. These measurements prove bounded database work for the tested reads; they do not measure live restaurant latency.

### Removed code and structure

Removed **13 unwired helpers per core edition**, comprising **636 lines of unused method bodies/decorators across both editions**. This count excludes the recipe implementation moved into the new package. The removed helpers are:

- `RecipeService.scale_recipe`
- `PurchaseOrderService.create_from_low_stock`
- `PurchaseReceivingItemService.add_all_pending`
- `ProductionOrderService.get_schedule` and `create_from_low_stock`
- `ProductionOrderIngredientService.record_actual`
- `StockItemUnitService.add_unit` and `remove_unit`
- `SupplierService.update_balance`
- `StockItemUnitRepository.unit_exists_for_item` and `clear_default`
- `RecipeRepository.get_versions` and `get_next_code_seq`

The old 1,137-line recipe service is now a compatibility module. Catalog/lifecycle, costing, relation loading, ingredients/substitutes, steps, and by-products have separate modules under `stock/services/recipes/`. Shared conversion arithmetic lives in `stock/services/conversions.py`. `stock/README.md` documents the folder boundaries, base-unit ledger rules, request-scoped loading, transaction requirements, and query budgets. Unused bookkeeping/imports and deletion leftovers were removed. New validation, regression tests, and formatting add lines of maintained code; moved code is not counted as dead-code removal.

### Verification

Seventy regression cases were added per edition; two per edition require PostgreSQL. The new cases cover units and overrides, fresh conversion data on subsequent reads, combined shortages, reservation/release, rollback after a later ingredient fails, competing reservations, concurrent recipe versions, cyclic ancestry, live/deleted records, query budgets, supplier limits, and HTTP parameter handling.

| Complete suite | Database | Passed | Skipped | Failed |
| --- | --- | ---: | ---: | ---: |
| core-desktop | SQLite | 1,146 | 7 | 0 |
| core-cloud | SQLite | 1,188 | 11 | 0 |
| local | SQLite | 650 | 11 | 0 |
| server | PostgreSQL 17 | 699 | 0 | 0 |

**Complete suites: 3,683 passed, 0 failed, 29 skipped.** Additional PostgreSQL stock runs: core-desktop 177 passed, core-cloud 188 passed. The additional runs include both stock concurrency tests in each edition.

Skipped full-suite cases identify Windows-specific, edition-specific, or PostgreSQL-only checks. Native Windows updater/ACL behavior was not exercised in this Linux environment. Test warnings concern the absent collected staticfiles directory. Django system checks and migration-drift checks pass for all four components, with no schema changes. Targeted unused-import, redefinition, undefined-name, and unused-local checks return zero findings across 946 tracked application Python files. Offline wheel builds include the new recipe package and the compatibility module in both editions.

### Source delivery

| Component | Tested source commit | GitHub branch |
| --- | --- | --- |
| core-desktop | [`fbbc9ec0`](https://github.com/MythicalCosmic/alpha_pos_core/commit/fbbc9ec05bb8f5cb394a64f79c8948c531cf08b6) | `audit/core-desktop-cleanup-2026-09-07` |
| core-cloud | [`21d54a74`](https://github.com/MythicalCosmic/alpha_pos_core/commit/21d54a747057826bd95da40e885957122620d6a3) | `audit/core-cloud-cleanup-2026-09-07` |
| local | [`222938d4`](https://github.com/MythicalCosmic/alpha_pos_local/commit/222938d4a47c5e9ee5b080c5474d8ddfe79172ac) | `audit/cleanup-2026-09-07` |
| server | [`6c4a5176`](https://github.com/MythicalCosmic/alpha_pos_server/commit/6c4a51760251520b509c50b7e40be3c0475ec490) | `audit/cleanup-2026-09-07` |

The server's final report commit follows its tested core-pin commit. The validation JSON records source hashes, exact suite totals, query counts, removed methods, and package checks. The root MD and `alpha_pos_server/docs/reports/code-cleanup-stock-audit-2026-09-07.md` contain the same report. Final original-folder and remote-head checks are saved locally as `originals-final.json` and `publication-verification.json` in the evidence directory.

Source changes and core pins are ready for the release process. This pass performs no production deployment, historical stock rewrite, or installer publication. The previously established sales discrepancy still requires dated actual cash/card closing amounts for physical reconciliation; these stock fixes do not supply that missing evidence. Recovery archives and historical stashes are retained.
