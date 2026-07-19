import importlib
from datetime import timedelta

import pytest
from django.apps import apps
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_backfill_publishes_linked_orders_and_is_idempotent(cashier, customer):
    from base.models import Order
    from smartfood.models import BotOrder

    linked = Order.objects.create(
        user=cashier,
        branch_id='branch-a',
        order_origin=Order.Origin.POS,
        sync_version=9,
        subtotal='31000.00',
        total_amount='31000.00',
    )
    old_published_at = timezone.now()
    old_updated_at = old_published_at - timedelta(days=1)
    Order.objects.filter(pk=linked.pk).update(
        synced_at=old_published_at,
        updated_at=old_updated_at,
    )
    BotOrder.objects.create(
        customer=customer,
        status=BotOrder.Status.DISPATCHED,
        pos_order=linked,
        dispatched_cashier=cashier,
        subtotal='31000.00',
        total='31000.00',
    )

    unlinked = Order.objects.create(
        user=cashier,
        branch_id='branch-a',
        order_origin=Order.Origin.POS,
        sync_version=4,
        subtotal='12000.00',
        total_amount='12000.00',
    )

    migration = importlib.import_module(
        'smartfood.migrations.0003_backfill_telegram_order_origin',
    )
    migration.backfill_dispatched_bot_orders(apps, schema_editor=None)

    linked.refresh_from_db()
    unlinked.refresh_from_db()
    assert linked.order_origin == Order.Origin.TELEGRAM
    assert linked.sync_version == 10
    assert linked.synced_at is not None
    assert linked.updated_at > old_updated_at
    assert not Order.objects.filter(pk=linked.pk, synced_at__isnull=True).exists()
    assert unlinked.order_origin == Order.Origin.POS
    assert unlinked.sync_version == 4

    migration.backfill_dispatched_bot_orders(apps, schema_editor=None)
    linked.refresh_from_db()
    assert linked.sync_version == 10
