# Alpha POS code audit — 7 September 2026

**Outcome: 14 confirmed findings addressed and pushed to GitHub under MythicalCosmic.** The four final full suites pass 3,416 tests; selected PostgreSQL checks also pass. The initial 11 findings were recorded and committed before application fixes; three more were recorded before their fixes when broader PostgreSQL validation exposed them.

Two defects can make stored sales disagree with collected payments. Authentication, input handling, PostgreSQL compatibility, diagnostic coverage and maintenance issues were also addressed. Findings and before/after evidence are retained below.

This is a code and isolated database audit, not a reconciliation of the restaurant's physical counter. No dated cash counts or card/provider statements were supplied. A software reproduction proves a defect exists; it does not prove how frequently that defect occurred at the restaurant or assign responsibility to its staff.

## Scope and baseline

Audited the released desktop/server code and their respective shared-core generations. Original working directories contained existing work and were preserved; investigation and fixes use separate Git worktrees.

| Component | Reviewed baseline | Fix branch under MythicalCosmic |
| --- | --- | --- |
| Desktop app (`alpha_pos_local`) | `7b7215fc21156da835e5c45015f9874f410ca519` | `audit/reliability-2026-09-07` |
| Desktop core (`alpha_pos_core`) | `14f423ac7e997fa1bff8bcc274bcffa2ae4a2239` | `audit/desktop-core-2026-09-07` |
| Server (`alpha_pos_server`) | `4b30331f8dbcf580b79f9e9ec49c98bab19a6495` | `audit/reliability-2026-09-07` |
| Cloud core (`alpha_pos_core`) | `05718d43c843474b6c44e78ce30adf241d6af958` | `audit/cloud-core-2026-09-07` |

The September 4–6 work report was pushed first in server commit `0714777`. The desktop and cloud core histories differ, so each receives the applicable changes and its parent repository must pin the matching commit.

Coverage: checkout, order edits, tender attribution, report windows, staff/courier/customer authentication, sync queues and publication, selected courier and Smartfood lifecycle paths, input/media boundaries, desktop updater tests, and finance/stock/treasury regression suites. Static checks covered tracked application Python, excluding migrations/tests/vendor bundles. This is not an exhaustive penetration test, dependency vulnerability scan, or manual review of every line.

All reproductions use synthetic records. SQLite databases and a separate PostgreSQL 17 container were used; test processes block external network connections. No live restaurant records were changed during this audit. Test runtime: Python 3.13.14, Django 6.0.3, pytest 8.3.4. Reporting timezone: Asia/Tashkent.

## Baseline verification before fixes

| Suite | Passed | Failed | Skipped | Interpretation |
| --- | ---: | ---: | ---: | --- |
| Desktop app | 560 | 0 | 11 | Three PostgreSQL cases and eight Windows cases skipped in this Linux/SQLite run |
| Server | 621 | 0 | 1 | PostgreSQL order-creation concurrency case skipped |
| Desktop core | 1,026 | 0 | 2 | Server-edition loyalty integration cases skipped |
| Cloud core | 1,067 | 1 | 6 | Four PostgreSQL cases and two edition integration cases skipped; failure explained by AUD-009 |
| **Total** | **3,274** | **1** | **20** | Distinct suite executions; shared code is tested in both core generations |

Ruff checks `F401,F811,F821,F823,F841` found zero issues across 333 desktop-core, 345 cloud-core, 97 desktop-app and 153 server Python files. This does not establish the absence of dead code: unused private methods can pass these checks. An AST scan found no unconditional-exit unreachable-code candidates; manual call-site review identified AUD-010.

Targeted tests added before application edits produced **58 failing cases** demonstrating the findings below. Some failures are parameter variations of one defect, not 58 separate bugs. An unchanged expense test passed when run with a controlled noon clock, isolating the baseline failure's cause.

## Findings and intended fixes

Priorities: **P1** = payment consistency or access revocation; **P2** = input robustness or audit reliability; **P3** = test/build/maintenance correctness. At the initial audit checkpoint AUD-001–AUD-011 were confirmed and not yet fixed. AUD-012–AUD-014 were documented as validation addenda before their fixes. **All are now addressed; the completion section records implementation and checks.**

### AUD-001 — P1 — Cloud order edits race with checkout

**Location:** server `admins/services/order_service.py`, `update_order`, `update_order_item`, `remove_item_from_order`.

Checkout locks the order, but these edit paths read an unlocked snapshot. An edit can read an unpaid order while checkout is running and save that old state after payment commits. Metadata edits save the whole model; quantity edits recalculate a total without sharing checkout's lock.

**Reproduction:** three PostgreSQL tests overlap a 20,000 UZS order's 10% discounted card checkout with increasing quantity, decreasing quantity or changing a note. All three fail on the baseline. Payment records contain 18,000 UZS, while a decrease leaves the order at 10,000 UZS; a note edit restores `is_paid=False` and 20,000 UZS. This requires neither deleting nor canceling an order.

**Fix:** serialize edits with checkout using the existing order row lock; limit metadata saves to fields actually edited; reject quantity changes after payment. This revalidates a previously prepared, unreleased patch.

**Evidence:** `admins/tests/checkout/test_checkout_edit_races.py`; `repro-checkout-server.xml` (3 failed).

### AUD-002 — P1 — Staff/courier authentication can retain revoked access

**Location:** both cores, `base/repositories/session.py`; invalidation wiring in `base/signals.py`.

Authentication caches a Session joined to its User for 300 seconds. Invalidation during an uncommitted role/status change can be followed by another request refilling the old committed row. A read already in progress can also refill stale data after revocation completes. Saving an earlier expiry does not invalidate the cached Session at all.

**Reproduction:** role change, deactivation and deletion with a PostgreSQL transaction held open; late cache fill after role change/deletion; and shortened expiry. All six fail on the cloud core baseline. The same repository implementation exists in desktop core.

**Fix:** use the indexed session hash lookup and joined current User on each authentication request, removing this cache as an authorization source. This adds one database query per authenticated lookup; it avoids relying on process-local cache invalidation or another timing-sensitive cache protocol. An already-running request is not retroactively canceled.

**Evidence:** `base/tests/security/test_session_cache_transactions.py`; six failures in `repro-core-core-cloud.xml`.

### AUD-003 — P1 — Customer sessions retain blocked/deleted/expired identities

**Location:** server `smartfood/repositories.py`, used by `smartfood/security.py::customer_required`.

The customer-session cache mirrors the staff cache, but Customer saves and CustomerSession saves/deletes have no corresponding invalidation signals. A cached customer can remain unblocked after the database marks it blocked; a deleted or shortened session can remain usable. Explicit logout deletion alone also cannot prevent an in-flight cache refill.

**Reproduction:** block, delete and expiry changes after a cached lookup; late fills following block/delete. All five fail.

**Fix:** authenticate against the indexed CustomerSession lookup and current joined Customer, using the same cache-removal approach as AUD-002.

**Evidence:** `smartfood/tests/api/test_auth_cache_revocation.py`; five failures in `repro-customer-auth-server.xml`.

### AUD-004 — P2 — Checkout rounds an undiscounted fractional bill differently

**Location:** desktop `customers/services/order_service.py`; server `admins/services/order_service.py`.

The payment calculation rounds to a whole UZS even when no new discount is requested, while the stored bill retains its fractional amount. Synthetic 10,000.25 and 10,000.75 UZS bills expose differences in cash/card/provider settlement paths. This is a legacy-data/input compatibility defect; this audit did not establish that the restaurant actually had fractional bills.

**Fix:** preserve the stored amount when the new discount is zero. Apply the existing whole-UZS discount policy only when a positive new discount is requested. This adopts a previously prepared, unreleased patch.

**Evidence:** desktop `customers/tests/payments/test_fractional_checkout.py` (6 failing cases); server `admins/tests/checkout/test_admin_pay_tender.py` (4 new failures, 44 existing tests passed).

### AUD-005 — P2 — Order item validation accepts malformed values

**Location:** desktop/server `customers/requests/order_requests.py` and `admins/requests/order_requests.py`; related add-item views.

Create-order validation assumes each item is a dictionary. Nulls, numbers and lists can raise exceptions. Boolean quantities pass an integer check. Product identifiers are not normalized or strictly validated: dictionaries and oversized IDs pass the request layer, and numeric strings remain strings despite the service indexing products by integer ID. Add-item paths have similar identifier coercion gaps, including boolean/float acceptance.

**Fix:** reject non-object items; accept only supported positive integral IDs within database capacity; normalize numeric-string IDs before product lookup; reject boolean or unstorable quantities; apply the ID helper consistently to add-item views.

**Evidence:** each edition's `test_order_payload_validation.py` produces 14 baseline failures (28 total). View validation boundaries were also inspected in source.

### AUD-006 — P2 — Quantity parsing can exceed storage limits or raise an exception

**Location:** both cores, `base/helpers/request.py::coerce_quantity`; `OrderItem.quantity` is a PositiveIntegerField.

The helper accepts quantities at or above 2³¹ despite PostgreSQL's column limit of 2³¹−1. A 5,000-digit ASCII numeric string raises Python's integer-conversion ValueError instead of producing a validation response.

**Fix:** bound the positive integer before accepting it; reject oversized text safely; retain existing supported whole-number float/string input behavior. This is a quantity boundary fix, not a claim that every conceivable monetary overflow has been audited.

**Evidence:** `base/tests/runtime/test_quantity_limits.py`; four failures and two valid-boundary passes in `repro-core-core-cloud.xml`.

### AUD-007 — P2 — Tender audit hides unpaid headers with collected money

**Location:** both cores, `base/management/commands/audit_tender_attribution.py`.

The command filters `is_paid=True` before auditing. An unpaid header with till payment rows—the state AUD-001 can create—disappears from the audit. The baseline command returns successfully even with `--fail` for a synthetic unpaid order with a card collection.

**Fix:** include unpaid headers with till payment evidence, missing payment timestamps and negative paid totals; support branch-scoped JSON output and one read-only PostgreSQL snapshot; include recent payment evidence for older orders. Preserve legitimate partial external collections and explicitly distinguish internal consistency from physical reconciliation. This extends a previously prepared diagnostic patch and does not repair history automatically.

**Evidence:** `base/tests/management/test_audit_unpaid_payment.py`; `repro-audit-gap-core-cloud.xml` (1 failed). Additional audit tests cover branch/date scope and absence of writes.

### AUD-008 — P2 — Malformed Telegram login hash raises TypeError

**Location:** server `smartfood/security.py::verify_init_data`.

`hmac.compare_digest` rejects a non-ASCII string argument. An untrusted `hash` field containing non-ASCII characters reaches this call and raises TypeError instead of returning authentication failure. This demonstrates error handling failure, not authentication bypass.

**Fix:** validate the supplied SHA-256 hex digest format before constant-time comparison; retain tests for valid and tampered signatures.

**Evidence:** `test_non_ascii_init_data_hash_is_rejected_without_server_error`; one failure in `repro-customer-auth-server.xml`.

### AUD-009 — P3 — Expense regression depends on the wall-clock hour

**Location:** cloud core `base/tests/finance/test_money_control_contract.py::test_voided_expense_without_reversal_nulls_paid_total`.

The fixture records a payment at `timezone.now()` but queries the current calendar date's 07:00–next-day-03:00 business window. Before 07:00 the fixture falls outside the selected current-date window, including after-midnight hours that belong to the previous business day, so the expected missing-reversal condition is absent. The unchanged test passes at local noon.

**Fix:** set the fixture payment and void timestamps explicitly inside the selected business day. Keep the production reporting window unchanged.

**Evidence:** sole failure in `baseline-core-cloud.xml`; `expense-noon-core-cloud.xml` (1 passed with a diagnostic noon-clock fixture).

### AUD-010 — P3 — Unused private helpers and misleading legacy sync controls

**Location:** both cores `SyncMixin._is_sync_on_save`, `_queue_for_sync`, `_is_sync_denylisted`; server `admins/services/dashboard_service.py::_range_window`; core `base/management/commands/sync.py`.

Manual call-site review found these private helpers unreferenced by application code. The dashboard uses the central reporting-window resolver. The sync engine now queues durably within the write transaction, but `--on-save`/`--off-save` and status/help output still imply an old flag controls that behavior. Stale comments also describe the old best-effort queue path.

**Fix:** remove the confirmed unused private helpers and obsolete comments. Retain accepted CLI flags with explicit deprecation messaging and report actual durable queue behavior. Do not let a compatibility flag disable durable queue writes.

**Retained intentionally:** Django signal handlers, decorated Telegram handlers, test fixtures, migration code and the public `check_tender_attribution` command alias. Lack of direct calls is not sufficient evidence to delete framework-registered or compatibility entry points.

**Evidence:** AST candidate scan plus repository call-site searches; `SyncMixin.save` and sync command behavior inspected directly. No production behavior fix is inferred merely from a lint count.

### AUD-011 — P3 — Package advertises an unsupported Python minimum

**Location:** both cores, `pyproject.toml`.

The package declares Python ≥3.11 while pinning Django 6.0.3, whose installed distribution metadata requires Python ≥3.12. Installation on the advertised minimum cannot satisfy the dependency set.

**Fix:** declare Python ≥3.12. No dependency upgrade is required. Tests here run on Python 3.13; this does not constitute a full supported-version matrix.

**Evidence:** project metadata compared with the installed Django distribution's `Requires-Python` field.

### AUD-012 — P2 — PostgreSQL JSON storage changes retry response serialization

**Added after the initial audit, before this item's fix.** The existing server test `test_headerless_admin_pay_exact_retry_is_safe_and_byte_stable` failed when moved from SQLite to PostgreSQL: the initial and replayed responses contain the same payment data but different object key ordering. Payment is still recorded only once; this is a response-contract defect, not evidence of a second charge.

**Location:** both cores, `base/security/idempotency.py`. The first response uses insertion order while a retry serializes the JSONField value read back from PostgreSQL JSONB, which does not preserve that order. The replay path also assumes a dictionary, so valid JSON-array responses raise TypeError on retry.

**Evidence:** `fixed-server-server.xml` (84 passed, 1 failed); `repro-json-replay-core-cloud.xml` (nested-object byte comparison and array replay both fail). The idempotency implementation was unchanged from each released baseline when reproduced: cloud SHA-256 `3dcf852859ce43904d3249e0fc8fb59cefd0cbd6d4cb841c8125ed7e1ff21116`; desktop SHA-256 `5b028192781ba529b13a569e461aae2f1523d0a2d9be0f40fc30331816250aea`.

**Fix:** use consistent key sorting for initial and replayed JSON response bodies and allow array bodies on replay. Preserve the first response's headers and cookies. Regression checks cover both database round-trip cases, one execution only, status, content length and first-response header/cookie preservation. No payment identity or request fingerprint rules need to change.

### AUD-013 — P2 — Broadcast edits/media/queueing fail on PostgreSQL

**Added during the full PostgreSQL server run, before this item's fix.** Seven existing Smartfood broadcast cases fail because the mutation query applies `FOR UPDATE` to all selected tables while joining nullable `created_by`. PostgreSQL rejects locking the nullable side of that outer join. Draft editing, image upload/validation and queueing return HTTP 500; dependent worker tests then find no queued message.

**Location:** server `smartfood/services/broadcast_service.py` (`update`, `send`) and `smartfood/services/media_service.py` (`BroadcastMediaService._get`). Both source files were identical to released baseline `4b30331` when reproduced.

**Fix:** restrict each mutation's lock to the broadcast row using `select_for_update(of=('self',))`. Keep draft-version review, immutable queued content and outbound idempotency guards. Use the existing broadcast integration suite against PostgreSQL, including media, changed-draft rejection, opt-out and duplicate delivery checks. Test messages use synthetic recipients and mocked delivery; no restaurant customers are contacted.

**Evidence:** seven broadcast failures in `verified-full-server.xml` / `.log`; exception `FOR UPDATE cannot be applied to the nullable side of an outer join`.

### AUD-014 — P3 — Three sales-report tests assume SQLite decimal formatting

**Added during the full PostgreSQL server run, before this item's fix.** Three report assertions require exact strings such as `200`, `20000` and `-80`. PostgreSQL returns numerically identical strings `200.00`, `20000.00` and `-80.00`. Requests succeed and the measured amounts agree; this is a test portability issue, not another money-calculation defect.

**Location:** server tests `admins/tests/analytics/test_dashboard_tod_hours.py`, `admins/tests/analytics/test_reporting_window_contract.py`, and `admins/tests/treasury/test_inkassa_branch_revenue.py`.

**Fix:** compare monetary values with Decimal while retaining exact order membership, counts, cashier identity and reporting-window assertions. Keep production arithmetic and formatting unchanged.

**Evidence:** three non-broadcast failures in `verified-full-server.xml`; together with AUD-013 this first full PostgreSQL server run has 646 passes and 10 failures.

## Evidence and reproducibility

Local evidence directory: `.audit-work/2026-09-07/evidence/` in the project root. Baseline XML/log files are retained separately from later runs. Synthetic regression tests will be committed with the fixes; raw live restaurant evidence and credentials are not part of this GitHub audit report.

| Reproduction run | Passed | Failed | Findings |
| --- | ---: | ---: | --- |
| `repro-checkout-server.xml` (PostgreSQL) | 0 | 3 | AUD-001 |
| `repro-core-core-cloud.xml` (PostgreSQL) | 2 | 10 | AUD-002, AUD-006 |
| `repro-customer-auth-server.xml` (PostgreSQL) | 0 | 6 | AUD-003, AUD-008 |
| `repro-input-money-local.xml` | 0 | 20 | AUD-004, AUD-005 |
| `repro-input-money-server.xml` | 44 | 18 | AUD-004, AUD-005 |
| `repro-audit-gap-core-cloud.xml` | 0 | 1 | AUD-007 |

Example after setting up each repository's test dependencies and a disposable database:

```bash
python -m pytest -q admins/tests/checkout/test_checkout_edit_races.py
python -m pytest -q base/tests/security/test_session_cache_transactions.py
python -m pytest -q smartfood/tests/api/test_auth_cache_revocation.py
```

Concurrency cases need PostgreSQL and independent connections. A passing SQLite suite does not prove row-lock behavior. The audit harness additionally isolates configuration and blocks non-loopback network access.

## Completion and GitHub delivery

All 14 findings below are addressed in source. The audit-before-fixes checkpoint is server commit `f379e96`; additional findings were recorded in `7bfcd06` and `da894aa` before their respective fixes. The original checkpoint's SHA-256 remains `cce52496b04eb06691f7691d41292ede08084720e9d893daa8eb058ba0e4a31a`.

| Finding | Area | Completed change / evidence |
| --- | --- | --- |
| AUD-001 | Cloud checkout/edit race | Order locks and partial metadata saves; three concurrent checkout cases pass |
| AUD-002 | Staff/courier session revocation | Current database identity on every lookup; transaction, late-read, expiry and rollback checks pass |
| AUD-003 | Customer session revocation | Current joined customer/session lookup; block/delete/expiry and late-read checks pass |
| AUD-004 | Fractional undiscounted bills | Stored amount preserved; cash/card/provider and retry cases pass |
| AUD-005 | Malformed order items/IDs | Object validation and bounded ID normalization in create/add-item paths |
| AUD-006 | Oversized quantities | Bounded integer parsing with long-input and database-boundary checks |
| AUD-007 | Incomplete tender audit | Branch/date-scoped JSON and header anomaly checks; PostgreSQL snapshot is read-only |
| AUD-008 | Non-ASCII Telegram digest | Invalid digest format returns authentication failure; valid login tests pass |
| AUD-009 | Clock-dependent expense test | Fixture uses a timestamp inside its selected business day |
| AUD-010 | Dead helpers and obsolete sync controls | Four private helpers removed across applicable editions; deprecated flags report actual queue behavior |
| AUD-011 | Python minimum metadata | Both core packages declare Python ≥3.12 to match pinned Django |
| AUD-012 | JSON replay serialization | Consistent JSON key ordering and array replay; one-execution and response checks pass |
| AUD-013 | PostgreSQL broadcast locking | Lock targets the broadcast row; full PostgreSQL server suite passes |
| AUD-014 | Decimal formatting test assumptions | Exact monetary comparisons use Decimal; identity/count/window assertions retained |

### Pushed source commits

Remote branch tips and parent core pointers were verified against GitHub after pushing. Documentation-only commits may follow these code commits on the server branch.

| Component | Source commit | GitHub branch |
| --- | --- | --- |
| Desktop app | [9af7e5447bec](https://github.com/MythicalCosmic/alpha_pos_local/commit/9af7e5447beca46d15408e4fbe8bbb83cf9f9395) | [audit/reliability-2026-09-07](https://github.com/MythicalCosmic/alpha_pos_local/tree/audit/reliability-2026-09-07) |
| Server | [bc9267ddd3db](https://github.com/MythicalCosmic/alpha_pos_server/commit/bc9267ddd3db3adf33e4ab844279787cf5a524f5) | [audit/reliability-2026-09-07](https://github.com/MythicalCosmic/alpha_pos_server/tree/audit/reliability-2026-09-07) |
| Desktop core | [c741b026fc90](https://github.com/MythicalCosmic/alpha_pos_core/commit/c741b026fc9028f429c3fa4ae16915546b775fc8) | [audit/desktop-core-2026-09-07](https://github.com/MythicalCosmic/alpha_pos_core/tree/audit/desktop-core-2026-09-07) |
| Cloud core | [eb1ce3af94e5](https://github.com/MythicalCosmic/alpha_pos_core/commit/eb1ce3af94e58577453f3e8e4621eaf7f8e5422f) | [audit/cloud-core-2026-09-07](https://github.com/MythicalCosmic/alpha_pos_core/tree/audit/cloud-core-2026-09-07) |

Local implementation worktrees are `.audit-work/2026-09-07/local`, `server`, `core-desktop` and `core-cloud` beneath the project root. The original working directories and their pre-existing uncommitted changes were preserved. These changes were pushed on audit branches; `main` was not merged or reset.

### Final validation

| Full suite | Database | Passed | Failed | Skipped | Evidence |
| --- | --- | ---: | ---: | ---: | --- |
| Desktop app | SQLite | 594 | 0 | 11 | `verified-full-local.xml` |
| Server | PostgreSQL 17 | 656 | 0 | 0 | `accepted-full-server.xml` |
| Desktop core | SQLite | 1,062 | 0 | 5 | `verified-full-core-desktop.xml` |
| Cloud core | SQLite | 1,104 | 0 | 9 | `verified-full-core-cloud.xml` |
| **Total** | | **3,416** | **0** | **25** | Shared core behavior is tested in both generations |

| Supplemental PostgreSQL checks | Passed | Evidence |
| --- | ---: | --- |
| Desktop payment, shift and courier cutoff checks | 49 | `verified-postgres-local.xml` |
| Desktop core authentication, replay and tender audit | 27 | `verified-postgres-core-desktop.xml` |
| Cloud core authentication, input boundaries and tender audit | 56 | `fixed-core-core-cloud.xml` |
| Cloud core JSON replay and related regressions | 21 | `fixed-json-replay-core-cloud.xml` |
| Cloud treasury/expense/receiving concurrency | 4 | `postgres-finance-core-cloud.xml` |

These supplemental executions overlap full suites; they are not additional distinct feature guarantees. They exercise all 13 PostgreSQL cases skipped across the final SQLite runs. Eight native Windows checks and four server-edition loyalty integration cases remain unexecuted in their respective suites. The latter are explicitly skipped because those core-only environments do not load the server integration. The full server suite has zero skips.

Django system checks pass in all four worktrees, and `makemigrations --check --dry-run` reports no changes. No schema migration is needed for these fixes. Selected Ruff checks pass across the application Python and all changed/new Python test files. Test-only missing-static-directory warnings remain in the SQLite suites and do not establish a packaged UI validation.

The first broad post-fix core run also exposed cache state leaking from the new legacy-sync-flag test into later tests. Its cleanup now restores the prior cache setting, and the complete core suites pass. Earlier failing outputs are retained separately; the final rows above identify the accepted runs. The first full PostgreSQL server run's 10 failures are explained by AUD-013 and AUD-014 and are resolved in the accepted full run.

Machine-readable final evidence summary: `docs/reports/code-audit-2026-09-07-validation.json` in the server repository; local original XML/logs remain in `.audit-work/2026-09-07/evidence/`.

### Deployment status and practical limits

The previously published desktop 1.0.43 addressed the original order report timestamp defect. This audit delivers additional source fixes on the branches above. Applying them in production requires deploying the updated server and building/releasing a desktop installer with the updated desktop core. This audit did not publish another installer or deploy its new code to the live restaurant/server.

Authentication now adds one indexed, joined database query per lookup instead of trusting a cached identity. A query-count regression verifies that lookup shape; this audit does not include production load testing or retroactively cancel requests already executing when an account is revoked.

Financial regressions, locking checks and a read-only tender audit support the fixes. They do not measure physical counter money, reconcile card/acquirer statements, automatically repair old records, or guarantee that future totals can never be wrong. Dated cash/card/provider evidence and restaurant installation/monitoring remain necessary to measure the actual end-of-day result.
