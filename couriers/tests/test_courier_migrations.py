from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import connection
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _schema_editor_stub():
    return SimpleNamespace(connection=connection)


def test_legacy_refunded_payment_repair_receives_accounting_cursor():
    """0005 closes the 0046 -> couriers.0004 combined-upgrade gap."""
    from base.models import Order, User
    from couriers.models import CourierPayment

    user = User.objects.create(
        email=f'courier-migration-{uuid4().hex}@test.local',
        first_name='Migration',
        last_name='Courier',
        role='CASHIER',
        status='ACTIVE',
        password='!',
        branch_id='branch-a',
    )
    order = Order.objects.create(
        user=user,
        cashier=user,
        order_type='DELIVERY',
        status='COMPLETED',
        branch_id='branch-a',
        subtotal='100000',
        total_amount='100000',
        is_paid=False,
    )
    economic_time = timezone.now() - timedelta(days=2)
    CourierPayment.objects.create(
        order=order,
        courier=None,
        provider='CASH',
        amount=100000,
        status='REFUNDED',
        external_id=f'legacy-refund-{uuid4().hex}',
        branch_id='branch-a',
        paid_at=economic_time,
        refunded_at=economic_time + timedelta(minutes=5),
    )

    legacy = import_module(
        'couriers.migrations.0004_migrate_refunds_to_order_ledger'
    )
    legacy.migrate_refunded_payments(apps, _schema_editor_stub())

    order.refresh_from_db()
    assert order.is_paid is True
    assert order.paid_at == economic_time
    assert order.accounting_recorded_at is None

    cursor = import_module(
        'couriers.migrations.0005_backfill_paid_order_accounting_cursor'
    )
    assert ('base', '0046_accounting_recorded_cursor') \
        in cursor.Migration.dependencies
    assert ('couriers', '0004_migrate_refunds_to_order_ledger') \
        in cursor.Migration.dependencies
    cursor.backfill_paid_order_accounting_cursor(
        apps, _schema_editor_stub(),
    )

    order.refresh_from_db()
    assert order.accounting_recorded_at is not None
    assert order.accounting_recorded_at > economic_time
