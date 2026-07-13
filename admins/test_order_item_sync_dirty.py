import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_cloud_increment_publishes_existing_line(
    settings, order_factory, product, django_capture_on_commit_callbacks,
):
    from admins.services.order_service import AdminOrderService

    settings.DEPLOYMENT_MODE = 'cloud'
    order = order_factory()
    item = order.items.get()
    type(item).objects.filter(pk=item.pk).update(synced_at=None)
    item.refresh_from_db()
    previous_version = item.sync_version

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        result, status = AdminOrderService.add_item_to_order(
            order.id, product.id, 2,
        )

        # Cloud change-feed publication is intentionally deferred until commit.
        # The pytest database fixture wraps the test in an outer transaction,
        # so capture and execute that callback explicitly.
        item.refresh_from_db()
        assert item.synced_at is None

    assert status == 200, result
    item.refresh_from_db()
    assert item.quantity == 3
    assert item.sync_version == previous_version + 1
    assert callbacks
    assert item.synced_at is not None
    assert item.synced_at <= timezone.now()
