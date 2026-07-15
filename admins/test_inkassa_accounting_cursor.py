from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_inkassa_cursor_windows_are_half_open_and_late_safe(
    admin_user, regular_user, monkeypatch,
):
    from base.models import CashRegister, Inkassa, Order, OrderRefund
    from admins.services import inkassa_service

    CashRegister.objects.create(branch_id='branch-a', current_balance='0')
    cutoff = timezone.now().replace(microsecond=0)
    economic_time = cutoff - timedelta(days=2)
    order = Order.objects.create(
        user=regular_user,
        branch_id='branch-a',
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.PAYME,
        paid_at=economic_time,
        subtotal='100.00',
        total_amount='100.00',
    )
    refund = OrderRefund.objects.create(
        order=order,
        amount='40.00',
        cash_amount='0.00',
        drawer_cash_amount='0.00',
        card_amount='0.00',
        payme_amount='40.00',
        unknown_amount='0.00',
        refunded_at=economic_time + timedelta(hours=1),
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id='late-provider-refund',
        branch_id='branch-a',
    )
    Order.objects.filter(pk=order.pk).update(
        accounting_recorded_at=cutoff,
    )
    OrderRefund.objects.filter(pk=refund.pk).update(
        accounting_recorded_at=cutoff,
    )

    monkeypatch.setattr(inkassa_service.timezone, 'now', lambda: cutoff)
    first, status = inkassa_service.AdminInkassaService.perform(
        admin_user, {'payme': '1.00'}, branch_id='branch-a',
    )
    assert status == 200, first
    first_row = Inkassa.objects.get(pk=first['data']['inkassas'][0]['id'])
    assert first_row.total_orders == 0
    assert first_row.total_revenue == Decimal('0')

    second_cutoff = cutoff + timedelta(seconds=1)
    monkeypatch.setattr(
        inkassa_service.timezone, 'now', lambda: second_cutoff,
    )
    second, status = inkassa_service.AdminInkassaService.perform(
        admin_user, {'payme': '1.00'}, branch_id='branch-a',
    )
    assert status == 200, second
    second_row = Inkassa.objects.get(pk=second['data']['inkassas'][0]['id'])
    assert second_row.period_start == cutoff
    assert second_row.total_orders == 1
    assert second_row.total_revenue == Decimal('60.00')

    third_cutoff = cutoff + timedelta(seconds=2)
    monkeypatch.setattr(
        inkassa_service.timezone, 'now', lambda: third_cutoff,
    )
    third, status = inkassa_service.AdminInkassaService.perform(
        admin_user, {'payme': '1.00'}, branch_id='branch-a',
    )
    assert status == 200, third
    third_row = Inkassa.objects.get(pk=third['data']['inkassas'][0]['id'])
    assert third_row.period_start == second_cutoff
    assert third_row.total_orders == 0
    assert third_row.total_revenue == Decimal('0')
