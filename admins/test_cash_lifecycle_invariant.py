"""One paid sale may reach treasury exactly once.

The real server payment path credits branch-owned CashRegister. Shift close and
manager reconciliation freezes the evidence and recognizes every confirmed
tender in SAFE. Inkassa is only the later physical drawer command/audit event;
it cannot recognize the proceeds a second time.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_mark_paid_reconcile_then_inkassa_books_sale_once(
    admin_user, cashier_user, regular_user,
):
    from admins.services.inkassa_service import AdminInkassaService
    from admins.services.order_service import AdminOrderService
    from admins.services.shift_service import ShiftService
    from base.models import (
        CashRegister,
        Inkassa,
        Order,
        Shift,
        TreasuryAccount,
        TreasuryTransaction,
    )

    branch = 'branch-a'
    admin_user.branch_id = 'cloud'
    admin_user.save(update_fields=['branch_id'])
    cashier_user.branch_id = branch
    cashier_user.save(update_fields=['branch_id'])
    shift = Shift.objects.create(
        user=cashier_user,
        status='ACTIVE',
        start_time=timezone.now() - timedelta(minutes=5),
        branch_id=branch,
        treasury_settlement_eligible=True,
    )
    order = Order.objects.create(
        user=regular_user,
        cashier=cashier_user,
        branch_id=branch,
        status='COMPLETED',
        is_paid=False,
        subtotal='100.00',
        total_amount='100.00',
    )

    # Physical cash is accepted only on the owning branch node; the remainder
    # of this test exercises the cloud reconciliation/inkassa half.
    with override_settings(DEPLOYMENT_MODE='local', BRANCH_ID=branch):
        result, status = AdminOrderService.mark_as_paid(
            order.id, payment_method='CASH', cashier_id=cashier_user.id,
        )
    assert status == 200, result
    register = CashRegister.objects.get(branch_id=branch, is_deleted=False)
    assert register.current_balance == Decimal('100.00')

    result, status = ShiftService.end_shift(
        shift.id,
        cashier_user.id,
        'close',
        actor=cashier_user,
        counted={'CASH': '100.00'},
    )
    assert status == 200, result
    result, status = ShiftService.reconcile(
        shift.id,
        actual_cash='100.00',
        notes='verified',
        reconciled_by_id=admin_user.id,
        actor=admin_user,
        confirmed={'CASH': '100.00'},
    )
    assert status == 201, result
    assert result['data']['treasury_posting']['total'] == '100.00'
    assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('100.00')
    assert TreasuryTransaction.objects.filter(
        type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
        reference_type='ShiftSettlement',
    ).count() == 1

    result, status = AdminInkassaService.perform(
        admin_user,
        {'cash': '100.00'},
        branch_id=branch,
        batch_key='cash-lifecycle-main',
    )
    assert status == 200, result
    assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('100.00')
    assert not TreasuryTransaction.objects.filter(
        type=TreasuryTransaction.Type.INKASSA,
    ).exists()
    assert TreasuryTransaction.objects.filter(
        type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
    ).count() == 1
    assert result['data']['treasury_posting']['status'] == 'not_posted'

    # Cloud does not overwrite a branch-owned raw register. The durable command
    # offsets it immediately and therefore blocks a repeat collection offline.
    register.refresh_from_db()
    assert register.current_balance == Decimal('100.00')
    assert Inkassa.pending_register_amount(register) == Decimal('100.00')
    result, status = AdminInkassaService.perform(
        admin_user,
        {'cash': '1.00'},
        branch_id=branch,
        batch_key='cash-lifecycle-repeat',
    )
    assert status == 422, result
    assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('100.00')
