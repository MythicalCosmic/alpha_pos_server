from decimal import Decimal
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.django_db


def test_dispatch_lines_publish_only_after_transaction_commit(
    settings, active_shift, cashier, product, customer,
    django_capture_on_commit_callbacks,
):
    from base.models import Customer as PosCustomer, Order, OrderItem
    from smartfood.models import BotOrder, BotOrderItem
    from smartfood.services.dispatch_service import DispatchService

    settings.DEPLOYMENT_MODE = 'cloud'
    bot_order = BotOrder.objects.create(
        customer=customer,
        status=BotOrder.Status.PENDING,
        order_type='DELIVERY',
        phone_number='+998900000000',
        subtotal=Decimal('39000'),
        total=Decimal('39000'),
    )
    BotOrderItem.objects.create(
        bot_order=bot_order,
        product=product,
        quantity=1,
        unit_price=Decimal('39000'),
        line_total=Decimal('39000'),
    )

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        result, status = DispatchService.dispatch(bot_order.id, cashier.id)
        assert status == 200, result
        bot_order.refresh_from_db()
        order = Order.objects.get(pk=bot_order.pos_order_id)
        item = OrderItem.objects.get(order_id=bot_order.pos_order_id)
        pos_customer = PosCustomer.objects.get(pk=order.customer_id)
        assert order.branch_id == active_shift.branch_id
        assert item.branch_id == active_shift.branch_id
        assert pos_customer.branch_id == active_shift.branch_id
        assert item.synced_at is None

    item.refresh_from_db()
    pos_customer.refresh_from_db()
    assert callbacks
    assert item.synced_at is not None
    assert pos_customer.synced_at is not None


def test_dispatch_customer_order_item_chain_applies_on_owning_terminal(
    settings, active_shift, cashier, product, customer,
    django_capture_on_commit_callbacks,
):
    """Regression: a cloud-owned Customer made the branch Order an orphan.

    Reproduce the old login placeholder, dispatch to another branch, then apply
    the emitted Customer -> Order -> OrderItem payloads as that terminal. Every
    FK must resolve on the first dependency-ordered pass.
    """
    from base.models import Customer as PosCustomer, Order, OrderItem
    from base.services.sync.service import SyncService
    from smartfood.models import BotOrder, BotOrderItem
    from smartfood.services.dispatch_service import DispatchService

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    settings.SYNC_ENABLED = False
    cashier.branch_id = 'cloud'  # User is a global company identity.
    cashier.save(update_fields=['branch_id'])
    active_shift.branch_id = 'branch-a'
    active_shift.save(update_fields=['branch_id'])

    legacy_placeholder = PosCustomer.objects.create(
        name='Legacy bot customer',
        phone_number=customer.phone_number,
        branch_id='cloud',
    )
    bot_order = BotOrder.objects.create(
        customer=customer,
        status=BotOrder.Status.PENDING,
        order_type='DELIVERY',
        phone_number=customer.phone_number,
        subtotal=Decimal('39000'),
        total=Decimal('39000'),
    )
    BotOrderItem.objects.create(
        bot_order=bot_order,
        product=product,
        quantity=1,
        unit_price=Decimal('39000'),
        line_total=Decimal('39000'),
    )

    with django_capture_on_commit_callbacks(execute=True):
        result, status = DispatchService.dispatch(bot_order.id, cashier.id)
        assert status == 200, result

    bot_order.refresh_from_db()
    source_order = Order.objects.get(pk=bot_order.pos_order_id)
    source_item = OrderItem.objects.get(order=source_order)
    legacy_placeholder.refresh_from_db()
    assert source_order.customer_id == legacy_placeholder.id
    assert {
        source_order.branch_id,
        source_item.branch_id,
        legacy_placeholder.branch_id,
    } == {'branch-a'}

    customer_payload = legacy_placeholder.to_sync_dict()
    order_payload = source_order.to_sync_dict()
    item_payload = source_item.to_sync_dict()
    source_customer_uuid = legacy_placeholder.uuid
    source_order_uuid = source_order.uuid
    source_item_uuid = source_item.uuid

    # Remove only this emitted chain, leaving global User/Product parents in
    # place, then become the branch terminal and replay the cloud payloads.
    OrderItem._base_manager.filter(pk=source_item.pk).delete()
    Order._base_manager.filter(pk=source_order.pk).delete()
    PosCustomer._base_manager.filter(pk=legacy_placeholder.pk).delete()
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'

    for model, payload in (
        (PosCustomer, customer_payload),
        (Order, order_payload),
        (OrderItem, item_payload),
    ):
        applied = SyncService._apply_records(model, [payload])
        assert applied['errors'] == []
        assert applied['deferred'] == []
        assert applied['created'] == 1

    pulled_customer = PosCustomer.objects.get(uuid=source_customer_uuid)
    pulled_order = Order.objects.get(uuid=source_order_uuid)
    pulled_item = OrderItem.objects.get(uuid=source_item_uuid)
    assert pulled_order.customer_id == pulled_customer.id
    assert pulled_item.order_id == pulled_order.id
    assert pulled_order.branch_id == pulled_item.branch_id == 'branch-a'


def test_dispatch_stock_failure_keeps_bot_order_pending_and_rolls_back_pos_order(
    monkeypatch, active_shift, cashier, product, customer,
):
    from base.helpers.response import ServiceResponse
    from base.models import Order
    from smartfood.models import BotOrder, BotOrderItem
    from smartfood.services.dispatch_service import DispatchService
    from stock.services import OrderStatusHandler, StockSettingsService

    bot_order = BotOrder.objects.create(
        customer=customer,
        status=BotOrder.Status.PENDING,
        order_type='DELIVERY',
        phone_number='+998900000000',
        subtotal=Decimal('39000'),
        total=Decimal('39000'),
    )
    BotOrderItem.objects.create(
        bot_order=bot_order,
        product=product,
        quantity=1,
        unit_price=Decimal('39000'),
        line_total=Decimal('39000'),
    )
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

    result, status = DispatchService.dispatch(bot_order.id, cashier.id)

    assert status == 400, result
    bot_order.refresh_from_db()
    assert bot_order.status == BotOrder.Status.PENDING
    assert bot_order.pos_order_id is None
    assert Order.objects.count() == before
