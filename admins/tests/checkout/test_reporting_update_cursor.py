"""Cloud payments must publish a changed order to incremental report readers."""
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from admins.services.order_service import AdminOrderService
from admins.tests.checkout.test_admin_pay_tender import _cashier, _unpaid_order
from base.models import Order


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('method', ['UZCARD', 'PAYME'])
def test_cloud_checkout_advances_report_timestamp_and_retry_preserves_payment(method):
    order = _unpaid_order(_cashier(), '10000')
    cached_at = timezone.now() - timedelta(days=2)
    Order.objects.filter(pk=order.pk).update(created_at=cached_at, updated_at=cached_at)
    action = uuid4()
    result, status = AdminOrderService.mark_as_paid(
        order.id, payment_method=method, payment_action_id=action,
    )
    assert status == 200, result
    order.refresh_from_db()
    assert order.updated_at > cached_at
    assert parse_datetime(order.to_sync_dict()['updated_at']) == order.updated_at
    assert order.is_paid
    assert order.payments.count() == 1
    updated_at, paid_at = order.updated_at, order.paid_at

    result, status = AdminOrderService.mark_as_paid(
        order.id, payment_method=method, payment_action_id=action,
    )
    assert status == 200, result
    order.refresh_from_db()
    assert order.updated_at == updated_at
    assert order.paid_at == paid_at
    assert order.payments.count() == 1


def test_rejected_cloud_payment_keeps_the_report_state():
    order = _unpaid_order(_cashier(), '10000')
    previous = order.updated_at
    result, status = AdminOrderService.mark_as_paid(
        order.id, payments=[{'method': 'UZCARD', 'amount': '5000'}],
    )
    assert status == 422, result
    order.refresh_from_db()
    assert order.updated_at == previous
    assert not order.is_paid
    assert not order.payments.exists()
