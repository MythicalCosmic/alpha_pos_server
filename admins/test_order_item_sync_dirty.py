import pytest
from types import SimpleNamespace
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


def test_cloud_mark_ready_publishes_each_changed_line(
    settings, order_factory, django_capture_on_commit_callbacks,
):
    from admins.services.order_service import AdminOrderService

    settings.DEPLOYMENT_MODE = 'cloud'
    order = order_factory()
    item = order.items.get()
    type(item).objects.filter(pk=item.pk).update(synced_at=timezone.now())
    item.refresh_from_db()
    previous_version = item.sync_version

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        result, status = AdminOrderService.mark_order_ready(order.id)
        item.refresh_from_db()
        assert item.synced_at is None

    assert status == 200, result
    item.refresh_from_db()
    assert item.ready_at is not None
    assert item.sync_version == previous_version + 1
    assert callbacks
    assert item.synced_at is not None


def test_cloud_unmark_ready_publishes_changed_line(
    settings, order_factory, django_capture_on_commit_callbacks,
):
    from admins.services.order_service import AdminOrderService

    settings.DEPLOYMENT_MODE = 'cloud'
    order = order_factory(status='READY')
    item = order.items.get()
    item.ready_at = timezone.now()
    item.save(update_fields=['ready_at'])
    type(item).objects.filter(pk=item.pk).update(synced_at=timezone.now())
    item.refresh_from_db()
    previous_version = item.sync_version

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        result, status = AdminOrderService.unmark_item_ready(order.id, item.id)
        item.refresh_from_db()
        assert item.synced_at is None

    assert status == 200, result
    item.refresh_from_db()
    assert item.ready_at is None
    assert item.sync_version == previous_version + 1
    assert callbacks
    assert item.synced_at is not None


def test_cloud_bulk_created_lines_publish_only_after_order_commit(
    settings, regular_user, cashier_user, product,
    django_capture_on_commit_callbacks,
):
    from base.models import OrderItem, Shift
    from admins.services.order_service import AdminOrderService

    settings.DEPLOYMENT_MODE = 'cloud'
    Shift.objects.create(
        user=cashier_user,
        status=Shift.Status.ACTIVE,
        start_time=timezone.now(),
        branch_id='branch1',
    )
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        result, status = AdminOrderService.create_order(
            user_id=regular_user.id,
            cashier_id=cashier_user.id,
            items=[{'product_id': product.id, 'quantity': 2}],
        )
        assert status == 201, result
        item = OrderItem.objects.get(order_id=result['data']['order_id'])
        # The row must remain in the changes endpoint's NULL safety lane until
        # the surrounding transaction is durable.
        assert item.synced_at is None

    item.refresh_from_db()
    assert callbacks
    assert item.synced_at is not None


def test_cloud_remove_item_keeps_sync_visible_tombstone(
    settings, order_factory, django_capture_on_commit_callbacks,
):
    from admins.services.order_service import AdminOrderService

    settings.DEPLOYMENT_MODE = 'cloud'
    order = order_factory(items=2)
    removed, live = list(order.items.order_by('id'))
    type(removed).objects.filter(pk=removed.pk).update(synced_at=timezone.now())

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        result, status = AdminOrderService.remove_item_from_order(
            order.id, removed.id,
        )
        removed.refresh_from_db()
        assert removed.is_deleted is True
        assert removed.synced_at is None

    assert status == 200, result
    removed.refresh_from_db()
    order.refresh_from_db()
    assert callbacks
    assert removed.synced_at is not None
    assert type(removed).objects.filter(pk=removed.pk).exists()
    assert order.items.filter(pk=live.pk, is_deleted=False).exists()
    assert order.total_amount == live.price * live.quantity


def test_soft_deleted_line_does_not_block_order_becoming_ready(order_factory):
    from admins.services.order_service import AdminOrderService

    order = order_factory(items=2)
    removed, live = list(order.items.order_by('id'))
    removed.delete()

    result, status = AdminOrderService.mark_item_ready(order.id, live.id)

    assert status == 200, result
    order.refresh_from_db()
    assert order.status == 'READY'
    assert result['data']['order']['all_items_ready'] is True


def test_removing_discounted_line_recalculates_discount_from_live_items(
    order_factory,
):
    from decimal import Decimal
    from admins.services.order_service import AdminOrderService
    from discounts.models import Discount, DiscountType, OrderDiscount

    order = order_factory(items=2)
    removed, _live = list(order.items.order_by('id'))
    order.subtotal = Decimal('20')
    order.discount_amount = Decimal('10')
    order.total_amount = Decimal('10')
    order.save(update_fields=['subtotal', 'discount_amount', 'total_amount'])
    discount_type = DiscountType.objects.create(
        name='Half', code='half',
        discount_method=DiscountType.Method.PERCENTAGE,
    )
    discount = Discount.objects.create(
        discount_type=discount_type, name='Half off', code='HALF',
        value=Decimal('50'),
    )
    OrderDiscount.objects.create(
        order=order, discount=discount, discount_code=discount.code,
        discount_amount=Decimal('10'),
    )

    result, status = AdminOrderService.remove_item_from_order(
        order.id, removed.id,
    )

    assert status == 200, result
    order.refresh_from_db()
    assert order.subtotal == Decimal('10')
    assert order.discount_amount == Decimal('5')
    assert order.total_amount == Decimal('5')


def test_stock_exception_rolls_back_cloud_order_item_edit(
    monkeypatch, order_factory,
):
    from admins.services.order_service import AdminOrderService
    from stock.services import OrderStockService, StockSettingsService

    order = order_factory()
    item = order.items.get()
    order.refresh_from_db()
    original_quantity = item.quantity
    original_subtotal = order.subtotal
    original_total = order.total_amount

    monkeypatch.setattr(
        StockSettingsService,
        'get_default_location_id',
        classmethod(lambda cls: 1),
    )

    def fail_adjustment(cls, *args, **kwargs):
        raise RuntimeError('simulated inventory database failure')

    monkeypatch.setattr(
        OrderStockService,
        'adjust_for_item_change',
        classmethod(fail_adjustment),
    )

    result, status = AdminOrderService.update_order_item(
        order.id, item.id, original_quantity + 2,
    )

    assert status == 400, result
    item.refresh_from_db()
    order.refresh_from_db()
    assert item.quantity == original_quantity
    assert order.subtotal == original_subtotal
    assert order.total_amount == original_total


def test_stock_failure_rolls_back_new_cloud_order(
    monkeypatch, regular_user, product,
):
    from base.helpers.response import ServiceResponse
    from base.models import Order
    from admins.services.order_service import AdminOrderService
    from stock.services import OrderStatusHandler, StockSettingsService

    monkeypatch.setattr(
        StockSettingsService,
        'load',
        classmethod(lambda cls: SimpleNamespace(
            stock_enabled=True,
            reserve_on_order_create=False,
            deduct_on_order_status='PREPARING',
        )),
    )
    monkeypatch.setattr(
        StockSettingsService,
        'get_default_location_id',
        classmethod(lambda cls: 1),
    )
    monkeypatch.setattr(
        OrderStatusHandler,
        'on_status_change',
        classmethod(lambda cls, *args, **kwargs: ServiceResponse.error(
            'forced deduction failure'
        )),
    )
    before = Order.objects.count()

    result, status = AdminOrderService.create_order(
        user_id=regular_user.id,
        items=[{'product_id': product.id, 'quantity': 1}],
    )

    assert status == 400, result
    assert Order.objects.count() == before
