# Historical financial repair runbook

Date: 2026-07-27
Audience: release operator and database reviewer
Scope: explicitly reviewed legacy register-expense and CASH-change defects only

## Safety rule

This procedure is a maintenance operation, not an anomaly scanner. It never
guesses which money is wrong. Use it only with a version-2 JSON plan whose
exact UUIDs, timestamps, before/after amounts, and raw payment evidence were
reviewed independently.

Do not run it during checkout. Do not edit production rows manually. Do not
apply a plan merely because the command can parse it.

The full sequence must run in one checkout-stopped window:

1. Stop cashier checkout, refunds, expenses, Inkassa, and every process that
   can create or apply a drawer movement.
2. Drain existing synchronization while monitoring both nodes. Prove there is
   no pending inbound financial command, then pause inbound pull.
3. Take and verify fresh local and cloud database backups after that quiescent
   state is reached.
4. Keep synchronization configured/enabled, but keep the worker/inbound pull
   paused while running the local dry-run and apply.
5. Resume only the controlled synchronization needed to send the corrected
   register generation and local repair audits. Treat any inbound financial
   movement as an abort requiring a new quiescent snapshot and review.
6. Wait for the exact corrected `CashRegister` generation and all local repair
   audit rows to reach the cloud.
7. Pause movement/pull again and apply the same plan on the cloud.
8. Verify both sides, then resume normal synchronization and reopen checkout.

The cloud gate intentionally requires the exact local `CashRegister`
`sync_version`, balance, UUID, and synchronized audit chain. A new sale,
expense, refund, or other register movement between local and cloud apply can
invalidate the cloud fingerprint. If that happens, stop and investigate; do
not bypass the gate.

## Required deployed version

Both local and cloud must run the release containing:

- migration `base/migrations/0055_alter_auditlog_action.py`
- management command `repair_financial_history`
- sync receiver settlement-immutability guard

Run the normal migration procedure on both nodes before this maintenance.
Confirm `showmigrations base` marks `0055` applied.

## Plan schema

Money must be a quoted decimal string. Timestamps must be timezone-aware
ISO-8601 strings. UUIDs must identify the exact rows already reviewed.
No extra keys are accepted.

```json
{
  "version": 2,
  "repair_id": "CHANGE-TICKET-UNIQUE-ID",
  "operator": "Named operator / change ticket",
  "branch_id": "EXACT_NON_CLOUD_BRANCH_ID",
  "register_repair": {
    "register_uuid": "00000000-0000-0000-0000-000000000000",
    "legacy_created_before": "2026-01-01T00:00:00.000000Z",
    "expenses": [
      {
        "uuid": "00000000-0000-0000-0000-000000000000",
        "amount": "1000.00",
        "shift_uuid": "00000000-0000-0000-0000-000000000000",
        "created_at": "2025-12-31T10:00:00.000000Z"
      }
    ],
    "expected_expense_total": "1000.00"
  },
  "shift_payment_total_repairs": [
    {
      "shift_uuid": "00000000-0000-0000-0000-000000000000",
      "payment_total_uuid": "00000000-0000-0000-0000-000000000000",
      "expected_before": "120000.00",
      "expected_after": "100000.00",
      "counted_amount": "100000.00",
      "difference_before": "-20000.00",
      "difference_after": "0.00",
      "customer_change_excluded": "20000.00"
    }
  ]
}
```

Before approving the plan, prove:

- Every expense is live/nondeleted, positive, not a remote
  `register_command`, and belongs to the exact stated branch shift.
- Its shift is live/nondeleted, `ENDED` or `COMPLETED`, and has a non-null
  `end_time`.
- The expense falls inside its shift window, and both the expense `created_at`
  and shift `end_time` are strictly before `legacy_created_before`.
- The expense UUID has not been consumed by any earlier financial repair.
- `expected_expense_total` is the exact expense sum.
- Exactly one live branch `CashRegister` exists, its UUID matches the plan, and
  its balance can cover the deduction when the repair is not already applied.
- Every payment-total row and its shift are live/nondeleted, owned by the exact
  branch, and bound to each other by the reviewed UUIDs.
- Every target shift is exactly `ENDED`, has a non-null `end_time`,
  `treasury_settlement_eligible == false`, an empty `settlement_manifest`, and
  no manager reconciliation.
- Every payment-total target is the CASH row, has
  `confirmed_amount == "0.00"`, and exactly matches the reviewed counted and
  before-or-after state.
- An already-corrected after-state row has its exact matching
  `FINANCIAL_REPAIR` audit marker; a before-state row does not.
- The row completed its original synchronization and has no outbound
  `shiftpaymenttotal` queue record.
- `expected_before - expected_after == customer_change_excluded`.
- `counted_amount - expected_before == difference_before`.
- `counted_amount - expected_after == difference_after`.
- Raw CASH tender minus the exact sum of `OrderRefund.drawer_cash_amount` and
  cashbox expenses equals
  `expected_before`.
- Canonical retained drawer CASH equals `expected_after`.

## Commands

Use the same immutable plan file and the same operator text on both nodes.
Replace the placeholders; do not paste the angle brackets literally.

### 1. Local dry-run

```bash
python manage.py repair_financial_history \
  --plan <absolute-path-to-reviewed-plan.json> \
  --branch <exact-branch-id> \
  --operator "<exact-plan-operator>"
```

Save the printed:

- plan SHA-256
- evidence fingerprint
- register before/after preview
- each CASH row before/after preview

The final line must say `Dry-run only; no rows changed.`

### 2. Local apply

Immediately use the fingerprint from that fresh dry-run:

```bash
python manage.py repair_financial_history \
  --plan <absolute-path-to-reviewed-plan.json> \
  --branch <exact-branch-id> \
  --operator "<exact-plan-operator>" \
  --apply \
  --confirm-repair-id <exact-repair-id> \
  --expect-fingerprint <local-dry-run-fingerprint>
```

Success is reported only after the database transaction commits. Any error
means the transaction rolls back.

### 3. Controlled synchronization and predecessor proof

Keep every checkout/drawer writer stopped. Resume controlled outbound
synchronization so it can send:

- the corrected local `CashRegister`
- every local `FINANCIAL_REPAIR` audit event

Wait until local outbound queue records for those UUIDs are gone. Confirm the
cloud stores exactly one live branch register with the same UUID, corrected
balance, exact local post-repair `sync_version`, and non-null `synced_at`.

Before and during this step, prove no inbound `CashboxExpense.register_command`,
refund/register command, Inkassa, or other financial instruction is pending.
If the installation cannot isolate push from pull, monitor the register
generation and treat any inbound financial movement as an abort. Do not continue
using the old fingerprint or backup snapshot.

Once the exact corrected cloud register generation and local audit predecessor
are present, pause the controlled sync worker/inbound pull again before the
cloud dry-run.

The repaired `ShiftPaymentTotal` rows deliberately do not travel through
ordinary sync. They are append-only evidence and are applied independently on
the cloud in the next step.

### 4. Cloud dry-run

```bash
python manage.py repair_financial_history \
  --plan <absolute-path-to-the-same-reviewed-plan.json> \
  --branch <exact-branch-id> \
  --operator "<exact-plan-operator>"
```

The preview must say:

```text
Cloud prerequisite — synchronized local repair audit: READY (ready).
```

If it says `NOT READY`, do not apply. Read the reported reason and restore the
exact sync/audit prerequisite.

### 5. Cloud apply

Use the fresh cloud fingerprint, not the local fingerprint:

```bash
python manage.py repair_financial_history \
  --plan <absolute-path-to-the-same-reviewed-plan.json> \
  --branch <exact-branch-id> \
  --operator "<exact-plan-operator>" \
  --apply \
  --confirm-repair-id <exact-repair-id> \
  --expect-fingerprint <cloud-dry-run-fingerprint>
```

The cloud never changes the till-owned register balance. It corrects only the
reviewed cloud copies of the legacy CASH settlement rows and writes its own
audit markers.

## Post-apply verification

Before reopening checkout:

1. Run the command again without `--apply` on local and cloud. Every component
   must say `already applied`.
2. Confirm no target `shiftpaymenttotal` queue record exists.
3. On the cloud, confirm the complete synchronized **local-mode predecessor
   chain**: one local register marker plus every local payment-total marker,
   with the reviewed `repair_id`, `plan_sha256`, operator, component UUIDs, and
   money transitions.
4. Separately confirm the cloud-mode payment-total markers written by cloud
   apply. There is no cloud-mode register marker, and cloud AuditLogs are not
   expected to pull back to the till.
5. Confirm local and cloud settlement rows have identical reviewed
   `expected_after` and `difference_after`.
6. Confirm the register correction exists locally and at the synchronized
   cloud copy; cloud apply itself must not subtract it again.
7. Load the manager shift detail and verify canonical CASH, raw frozen evidence,
   and audit provenance are distinguishable.
8. Verify sync queues have no new failed, rejected, or quarantined financial
   records.
9. Preserve the plan, command output, database backup identifiers, and
   post-apply verification in the change ticket.

Only after all checks pass may checkout resume.

## Abort conditions

Abort without workarounds if:

- checkout/drawer movement was not quiesced before the backups;
- either post-quiescence database backup is missing or unverified;
- checkout cannot be stopped;
- an inbound financial command is pending or moves the register during the
  controlled synchronization window;
- the plan or operator differs between nodes;
- a fingerprint changes;
- the cloud prerequisite is not `READY`;
- a target row is reconciled, unsynchronized, queued, missing, or no longer in
  the exact reviewed before/after state;
- raw tender evidence does not prove customer change;
- a prior audit already consumed an expense UUID;
- the command reports any mismatch or rollback.

The correct response to an abort is a new forensic review and, if justified, a
new repair plan—not a manual SQL update.
