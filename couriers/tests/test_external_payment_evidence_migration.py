from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import connection
from django.utils import timezone

from base.models import ExternalOrderPayment, Order, OrderRefund, User
from couriers.models import CourierPayment


pytestmark = pytest.mark.django_db


def _order():
    user = User.objects.create(
        email=f'courier-evidence-migration-{uuid4().hex}@test.local',
        first_name='Migration', last_name='Courier', role='COURIER',
        status='ACTIVE', password='!', branch_id='branch-a',
    )
    return Order.objects.create(
        user=user, cashier=user, order_type='DELIVERY', status='COMPLETED',
        branch_id='branch-a', subtotal='100000', total_amount='100000',
    )


def test_0009_backfill_is_idempotent_and_pairs_legacy_refund():
    paid_order = _order()
    refunded_order = _order()
    economic_time = timezone.now() - timedelta(days=2)
    paid = CourierPayment.objects.create(
        order=paid_order, provider='QR', amount=100000, status='PAID',
        external_id=f'paid-{uuid4().hex}', branch_id='branch-a',
        paid_at=economic_time,
    )
    refunded = CourierPayment.objects.create(
        order=refunded_order, provider='CASH', amount=100000,
        status='REFUNDED', external_id=f'refunded-{uuid4().hex}',
        branch_id='branch-a', paid_at=economic_time,
        refunded_at=economic_time + timedelta(minutes=5),
    )
    migration = import_module(
        'couriers.migrations.0009_backfill_external_payment_evidence',
    )
    schema_editor = SimpleNamespace(connection=connection)

    migration.backfill_external_payment_evidence(apps, schema_editor)
    migration.backfill_external_payment_evidence(apps, schema_editor)

    assert ExternalOrderPayment.objects.filter(
        source='COURIER', source_id__in=[paid.external_id, refunded.external_id],
    ).count() == 2
    refund = OrderRefund.objects.get(
        source='COURIER_PAYMENT', source_id=refunded.external_id,
    )
    assert refund.order_id == refunded_order.pk
    assert refund.amount == refund.cash_amount == 100000
    assert refund.drawer_cash_amount == 0
    assert refund.refunded_at == refunded.refunded_at
    assert OrderRefund.objects.filter(
        source='COURIER_PAYMENT', source_id=refunded.external_id,
    ).count() == 1
