"""Delivery-location snapshot backfill tests."""

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection


pytestmark = pytest.mark.django_db


def test_backfill_copies_existing_address_coordinates(customer, address):
    from smartfood.models import BotOrder

    order = BotOrder.objects.create(
        customer=customer,
        address=address,
        address_text=address.line,
        phone_number=customer.phone_number,
        address_lat=None,
        address_lng=None,
    )
    migration = importlib.import_module(
        'smartfood.migrations.0007_customer_language_overridden_and_more',
    )

    migration.backfill_order_locations(
        apps,
        SimpleNamespace(connection=connection),
    )

    order.refresh_from_db()
    assert order.address_lat == address.lat
    assert order.address_lng == address.lng

    # Re-running the data migration is safe and leaves the snapshot stable.
    migration.backfill_order_locations(
        apps,
        SimpleNamespace(connection=connection),
    )
    order.refresh_from_db()
    assert order.address_lat == address.lat
    assert order.address_lng == address.lng


def test_customer_phone_backfill_canonicalizes_only_valid_legacy_values(customer):
    from smartfood.models import Customer

    migration = importlib.import_module(
        'smartfood.migrations.0007_customer_language_overridden_and_more',
    )
    customer.phone_number = '+998 (90) 123-45-67'
    customer.save(update_fields=['phone_number', 'updated_at'])
    invalid = Customer.objects.create(
        telegram_id=customer.telegram_id + 1,
        phone_number='internal-42',
    )

    migration.canonicalize_customer_phones(
        apps,
        SimpleNamespace(connection=connection),
    )

    customer.refresh_from_db()
    invalid.refresh_from_db()
    assert customer.phone_number == '998901234567'
    assert invalid.phone_number == 'internal-42'
