# Money Control rollout and reconciliation

This release changes accounting write paths. Deploy schema and code first, then
review dry-run evidence. Never rewrite or auto-classify ambiguous legacy money.

## Migrations

Apply in dependency order through:

- `base.0058_money_control_foundation`
- `base.0059_alter_idempotencykey_scope`
- `hr.0010_money_control_foundation`
- `hr.0011_expense_category_allowed_sources_snapshot_and_more`
- `stock.0013_money_control_contract`
- `stock.0014_purchasereceiving_completion_action_id_and_more`
- `cashbox.0004_money_control_contract`
- `cashbox.0005_cashboxexpense_actor_display_snapshot_and_more`
- `hr.0012_backfill_legacy_treasury_expenses`
- `base.0060_money_control_permissions`
- `base.0061_remove_treasurytransaction_uniq_treasury_command_account_type_and_more`
- `base.0062_alter_auditlog_action`
- `base.0063_paymentmethodconfig_provider_codes`
- `stock.0015_supplierpayment_reversal_action_id_and_more`
- `stock.0016_receiving_base_unit_snapshots`

Migration `base.0058` records the settlement cutover timestamp. The category
backfill creates isolated, clearly named migrated categories rather than
guessing equivalence from names. Supplier payments without a source become
`LEGACY_UNFUNDED` evidence; no SAFE/BANK source is invented.

## Safe deployment sequence

1. Record current server/core revisions and verify a clean tracked worktree.
2. Take a timestamped PostgreSQL custom-format backup and verify `pg_restore -l`.
3. Restore that backup to an isolated database, apply migrations, run Django
   checks, and generate both reports below.
4. Review category mappings, expense links, unfunded supplier payments, and
   legacy shift candidates. Do not approve ambiguous corrections.
5. Deploy the reviewed revisions, migrate, restart workers/web, and verify
   `/healthz` plus both owner GET endpoints.
6. Run the same reports against production and retain their JSON with the
   deployment evidence.

```bash
python manage.py audit_money_control \
  --branch <branch-id> \
  --as-of <timezone-aware-iso> \
  --output <audit-report.json>

python manage.py reclassify_legacy_shift_tenders \
  --branch <branch-id> \
  --cutoff <timezone-aware-iso> \
  --dry-run
```

The audit report includes category mapping, legacy supplier payment, and
expense de-duplication sections. The shift report lists the original deposit,
tender, amount, possible related transfers, and `SAFE`, `AMBIGUOUS`, or
`ALREADY_RECLASSIFIED` classification.

An approved safe reclassification is explicit and append-only:

```bash
python manage.py reclassify_legacy_shift_tenders \
  --branch <branch-id> \
  --cutoff <timezone-aware-iso> \
  --approve-transaction <treasury-transaction-id> \
  --actor-id <authorized-user-id> \
  --reason "<review ticket and reason>"
```

The actor must have `money.control.reconcile`. The command creates a linked SAFE
debit and BANK credit. It refuses already-classified rows, possible related
transfers, missing funds, or unapproved IDs.

## Rollback and reversal

Do not roll migrations backward after canonical writes begin: older code does
not understand the new ledger links. Roll back the application by deploying a
forward-compatible hotfix while leaving schema and evidence in place.

Financial corrections are append-only:

- void a paid expense through its authorized `/void` endpoint;
- reverse a supplier payment through its protected reversal endpoint;
- reverse an approved legacy reclassification with reviewed paired Treasury
  entries, never by editing or deleting the original rows;
- restore a database backup only if deployment failed before accepting new
  writes, or during a coordinated full outage with explicit data-loss review.

## PostgreSQL performance evidence

Evidence was collected on PostgreSQL 16 with 2,000 raw items and stock levels,
10,000 annual canonical expenses, and 10,002 Treasury ledger rows. Each result
is from 30 authenticated HTTP calls after one warm-up.

| Endpoint | Median | p95 | Target |
|---|---:|---:|---:|
| `/api/admins/stock/inventory-control/?item_type=RAW&page=1&per_page=25` | 132.38 ms | 187.28 ms | 500 ms |
| `/api/admins/money-control/overview` over 365 days | 260.56 ms | 290.95 ms | 800 ms |

`EXPLAIN (ANALYZE, BUFFERS)` evidence for the inventory aggregate returned
2,000 rows through a one-batch `HashAggregate`, used 77 shared-buffer hits, and
completed in 12.122 ms. The overview's 10,000-row canonical expense evidence
query used the expense primary-key index plus one-to-one left joins, 364 shared
buffer hits, and completed in 5.250 ms. Its 10,001-row BANK ledger continuity
scan completed in 4.944 ms with 292 shared-buffer hits. PostgreSQL chose a
sequential ledger scan because the fixture intentionally placed nearly every
Treasury row in that account; no temporary spill occurred.

Re-run this evidence after materially larger production growth or changed
query plans. Cache is not required for the measured targets.

## Post-deploy gates

- `python manage.py check` reports no issues.
- `python manage.py makemigrations --check --dry-run` reports no changes.
- Administrator receives `200` from both owner endpoints.
- Warehouse receives `403` from Money Control, Treasury, supplier payment, and
  inventory control; its existing supplier-balance reads remain available.
- No overview is declared `COMPLETE` while its report has an unsafe issue.
- Health checks pass after migrations and process restart.
