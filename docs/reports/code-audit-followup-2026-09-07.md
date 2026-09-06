# Alpha POS follow-up audit — 7 September 2026

**Six additional findings fixed and pushed to GitHub under MythicalCosmic. All five active repository folders now show zero pending changes.** The four final full suites pass **3,547 tests**, with **zero failures and 25 skips**. This report continues AUD-001–AUD-014 in `CODE_AUDIT_2026-09-07.md`. AUD-015–AUD-019 were recorded and committed before application fixes; AUD-020 was added before its fix when HTTP-level validation exposed an additional transaction boundary.

## The 24 outstanding changes

The original checkout folders still contained the earlier patches while the tested and pushed changes lived in isolated Git worktrees. The 24 entries were 22 changed/untracked files plus two dirty submodule markers across three project repositories and their two core submodules. They were not 24 new defects.

Every tracked code patch was verified against the published audit tree with a reverse-apply check. Untracked code tests were already present there byte for byte. Before switching branches, all original changes were saved in local Git stashes and exact recovery copies, including binary patches and a SHA-256 manifest. No original work was discarded.

The active desktop, server, root core, and two nested core folders were then updated to the published audit commits and verified clean. Two older reports absent from GitHub were retained under `reports/historical-local-reports/`. They contain operational information and were not copied to the public repository. Recovery copies are under `reports/workspace-recovery-2026-09-07/`; those stashes should not be reapplied because their code is already included.

Evidence is retained locally in `.audit-work/2026-09-07-followup/evidence/remaining-changes-inventory.json`, `workspace-recovery.json`, and the recovery manifest. After the new fixes, all active working folders must again point at the final tested commits and show no pending changes.

## Scope and method

| Component | Starting commit | Follow-up branch |
| --- | --- | --- |
| Desktop core | `c741b026fc9028f429c3fa4ae16915546b775fc8` | `audit/core-desktop-followup-2026-09-07` |
| Cloud core | `eb1ce3af94e58577453f3e8e4621eaf7f8e5422f` | `audit/core-cloud-followup-2026-09-07` |
| Desktop app | `9af7e54` | `audit/reliability-followup-2026-09-07` |
| Server | `029f5c2a04e65d8162c33c39b946958dd9b93c43` | `audit/reliability-followup-2026-09-07` |

This pass reviews discount eligibility/calculation, order creation and editing, remaining reference-ID validation, and idempotency response persistence. It is a bounded follow-up, not a claim that every possible defect has been eliminated. Code is tested in isolated worktrees using synthetic records, SQLite, and a separate PostgreSQL 17 container. Test processes block external network connections and do not inherit production configuration. Runtime: Python 3.13.14, Django 6.0.3, pytest 8.3.4; reporting timezone Asia/Tashkent.

## Confirmed findings and fixes

All six findings below are addressed. Their original reproductions and intended fixes are retained to show the evidence available before implementation; completion details follow.

### AUD-015 — P2 — Coupon application checks the wrong subtotal and can use obsolete eligibility

**Location:** both cores, `discounts/services/discount_service.py`, `apply_to_order` and `validate_code`.

Application first calls code validation with a subtotal of zero. Every coupon with a positive minimum therefore fails even when the actual order qualifies. After that unlocked validation, the service locks the order and coupon but only rechecks usage/minimum constraints; a coupon deactivated or moved outside its validity dates during that window can still apply.

**Reproduction:** a subtotal of 10 with a 10% coupon requiring 5 or 10 is rejected instead of reducing the total to 9. Deterministic database state transitions before acquiring the order lock show inactive, expired, and not-yet-started coupons still being applied. Existing under-minimum, deleted, and exhausted-coupon rejection controls pass.

**Intended fix:** acquire the order and coupon locks in that order, then validate all eligibility rules against the locked coupon and actual subtotal. Share the eligibility routine with public code validation to prevent drift. A deterministic transition test verifies the former read window; it is not a threaded production-concurrency trace.

### AUD-016 — P2 — Buy/get calculation crashes with zero free units and allocates memory per unit

**Location:** both cores, `DiscountService.calculate_discount`, `BUY_X_GET_Y`.

For buy two/get one, a basket of two units enters the calculation but contains no complete three-unit set. `sum([])` produces integer zero, and the final `.quantize()` raises `AttributeError`. Larger baskets expand every unit into a list of prices before sorting, making memory proportional to quantity instead of order lines.

**Reproduction:** the two-unit basket raises the exception. A two-line basket containing 250,000 units allocates 3,200,452 traced bytes while calculating a correct 149,999.00 discount. Much larger accepted quantities would require correspondingly larger allocations; no dangerous allocation was attempted.

**Intended fix:** retain Decimal zero and sort order lines by price, consuming the required cheapest units in quantity chunks. Preserve exact cheapest-item selection, rounding, and cumulative caps with small/large basket checks.

### AUD-017 — P2 — Valid individual inputs can exceed order storage capacity

**Location:** desktop customer/waiter order services, server admin order service, and shared order storage validation.

`Order.subtotal` and `total_amount` store at most 99,999,999.99; `OrderItem.quantity` stores at most 2,147,483,647. Existing input checks do not validate a projected order subtotal or the combined quantity when incrementing an existing line. Some service update paths also accept boolean quantities.

**Reproduction:** changing a 60,000,000-priced line from one to two units raises PostgreSQL numeric overflow; adding one to an existing quantity of 2,147,483,647 raises integer overflow. New-order paths also calculate unchecked totals. These are failure/invalid-storage cases, not evidence of a silent ordinary-sized sale being miscounted. The original desktop create fixture omitted the required terminal binding; using the real shift-start service corrected it. Both desktop creation paths then reached stock processing with an oversized order on SQLite. The first fixture failures are retained but excluded from defect evidence.

**Intended fix:** check the actual model capacity and projected live-line total under the existing order lock, before writing lines, totals, stock, or allocating order numbers. Check creation totals before allocating identifiers or resolving a new cloud customer. Test unchanged persisted state on rejection and acceptance at the exact monetary limit. Keep the database schema and normal-sized order behavior unchanged.

### AUD-018 — P2 — Remaining order reference IDs bypass strict validation

**Location:** desktop `customers/requests/order_requests.py`, server `admins/requests/order_requests.py`, desktop waiter create service.

Product validation added in AUD-005 did not cover customer/courier IDs on desktop or user/cashier/courier IDs on the server. Booleans, fractions, structures, and oversized integers pass request validation; numeric strings are not normalized. The waiter create service uses `int()` for product/place/table IDs, accepting booleans and truncating fractional identifiers.

**Reproduction:** parameterized parser tests accept malformed references. Waiter creation accepts/truncates malformed product IDs, and an oversized product ID raises SQLite integer conversion overflow. Invalid place/table IDs can reach lookups instead of receiving validation errors.

**Intended fix:** use the existing positive-ID helper for these used fields, normalize numeric strings, reject booleans/fractions/out-of-range values, and preserve omitted/null/empty optional references. Do not interpret a malformed reference as a different product or actor.

### AUD-019 — P2 — A JSON null response cannot be persisted for retry

**Location:** both cores, `base/security/idempotency.py`.

JSON `null` parses as Python `None`. Assigning that directly to the non-nullable `IdempotencyKey.response_body` JSONField generates SQL NULL. The persistence failure is logged and swallowed, leaving the claim marked in progress even though the endpoint returned success.

**Reproduction:** an idempotent synthetic endpoint returning HTTP 201 with body `null` executes once; the immediate retry incorrectly returns 409. PostgreSQL records a not-null constraint error. Object, array, false, zero, and string controls pass.

**Intended fix:** store an explicitly typed JSON value so JSON null remains distinct from SQL NULL. Keep the schema, first-response cookies/headers, and existing retry action identity unchanged. This tests the generic decorator; it does not demonstrate that a live payment endpoint returns a null body.

### AUD-020 — P3 — A rejected HTTP order leaves a newly created customer behind

**Location:** desktop `customers/views/order_views.py`, `create_order`.

The HTTP view resolves/creates a customer before entering the order service's transaction. If order validation rejects the request, the customer write has already succeeded. The service-level storage checks therefore cannot make the whole HTTP operation atomic.

**Reproduction:** an HTTP request with a new customer and an oversized basket returns 422 but leaves that customer in the database; a successful-order control passes. Evidence: `reproduce-customer-rollback-local.xml` (one failure, one pass), using synthetic records.

**Intended fix:** encompass customer resolution and order creation in one transaction, roll back that operation on an unsuccessful service result, and retain the customer association when creation succeeds. Keep idempotency response persistence outside this rollback boundary.

## Evidence before application fixes

Regression tests were added before application edits. Test files include `discounts/tests/test_discount_eligibility_and_scaling.py`, `base/tests/security/test_idempotency_json_response.py`, and each app's `test_order_storage_limits.py` and `test_order_payload_validation.py`.

Local evidence directory: `.audit-work/2026-09-07-followup/evidence/`. Baseline logs and JUnit XML are named `reproduce-discount-retry-*`, `reproduce-order-limits-*`, `reproduce-reference-ids-*`, and `reproduce-waiter-ids-*`. Failures include parameter variants of the same defect and must not be counted as separate bugs.

## Completion and verification

| Finding | Implemented result |
| --- | --- |
| AUD-015 | One eligibility routine checks the locked coupon and actual order subtotal; qualifying minimums work and intervening eligibility changes are rejected. |
| AUD-016 | Buy/get calculations preserve Decimal zero and select cheapest units by sorted order lines. The large-basket regression stays below its 1 MB allocation budget. |
| AUD-017 | Shared model-capacity checks validate creation totals, projected live-line totals, and combined quantities before writes. Invalid changes preserve order/line/counter state and do not reach stock writes. Exact-limit totals remain accepted. |
| AUD-018 | Used customer/user/cashier/courier and waiter product/place/table references receive strict positive-ID validation; numeric strings normalize, and blank optional references normalize to null. |
| AUD-019 | Explicit JSON typing persists JSON null without attempting SQL NULL. First responses and retries retain the same status/body and execute the endpoint once. |
| AUD-020 | Customer resolution and order creation share a transaction. Unsuccessful service results roll it back; successful orders retain their linked customer. |

| Final full suite | Database | Passed | Failed | Skipped |
| --- | --- | ---: | ---: | ---: |
| Desktop app | SQLite | 650 | 0 | 11 |
| Server | PostgreSQL 17 | 699 | 0 | 0 |
| Desktop core | SQLite | 1,078 | 0 | 5 |
| Cloud core | SQLite | 1,120 | 0 | 9 |
| **Total** | | **3,547** | **0** | **25** |

These are four suite executions, including shared-code tests in both core editions, not 3,547 unique business scenarios. Skips comprise 13 PostgreSQL-specific cases in SQLite suites, eight Windows-only cases, and four core-only server-integration cases. Focused PostgreSQL checks additionally pass **195** desktop order/payment/waiter tests, **82** cloud discount/security/finance tests, and **18** desktop-core discount/retry tests. They cover nine of the 13 PostgreSQL skips in this pass; the remaining four passed in the earlier audit and were not repeated here. Windows-only and core-only server-integration skips remain unexecuted in this environment.

Selected Ruff checks (`F401,F811,F821,F823,F841`) pass across 930 tracked application Python files and all changed Python files including tests. No additional confirmed dead-code removals were identified in the reviewed paths. All four Django system checks and migration-drift checks pass; no migrations are introduced.

An intermediate cloud-core run exposed an implementation mistake when the desktop idempotency module was initially copied over the cloud variant. Before publication, the cloud's required-key, branch-scoping, canonical request-fingerprint, and error-code behavior was restored; only JSON-null storage was changed there. The final cloud suite passes 1,120 tests and its focused PostgreSQL run passes 82. The earlier failed log is retained as intermediate validation evidence, not counted as another pre-existing defect or as a final result.

| Component | Tested source commit | Published branch |
| --- | --- | --- |
| Desktop app | [`35f88c9`](https://github.com/MythicalCosmic/alpha_pos_local/commit/35f88c9a1af00760785cc7ae10ac62f7891976e8) | `audit/reliability-followup-2026-09-07` |
| Server | [`d53d861`](https://github.com/MythicalCosmic/alpha_pos_server/commit/d53d861e386b919112e567059e277307dd6c72b7) | `audit/reliability-followup-2026-09-07` |
| Desktop core | [`8451faa`](https://github.com/MythicalCosmic/alpha_pos_core/commit/8451faa3361ca1ca8cce9db2ad0a1dfac094b9ba) | `audit/core-desktop-followup-2026-09-07` |
| Cloud core | [`30be48b`](https://github.com/MythicalCosmic/alpha_pos_core/commit/30be48bb7489d04c6cc8f97ae0cda09cd2843d73) | `audit/core-cloud-followup-2026-09-07` |

The server branch also contains the documentation checkpoints and final report. The desktop/server parent repositories pin their matching core commits. Active original folders are on these follow-up branches, nested cores are at the correct detached commits, and all five folders were checked clean after synchronization. Existing `main` branches were not merged or force-pushed. Recovery archives and stashes remain available locally.

Public validation summary: `docs/reports/code-audit-followup-2026-09-07.validation.json`. Full local logs/XML remain in `.audit-work/2026-09-07-followup/evidence/`; definitive full-suite files are `final-full-local.xml`, `final-full-server.xml`, `final-full-core-desktop.xml`, and `final-verified-core-cloud.xml`.

## Operational limits

These source changes are available on the published follow-up branches. The earlier desktop 1.0.43 release and production timestamp fix are separate completed work; this pass did not build/publish another installer or deploy these additional changes to production. Test infrastructure was isolated from the restaurant, and no live restaurant records were modified. The temporary PostgreSQL container was removed after verification.

There are still no dated physical cash counts and card/provider statements to reconcile. Neither these tests nor a clean Git status prove the restaurant's historical counter balance, assign responsibility to staff, or guarantee that future software calculations can never be wrong.
