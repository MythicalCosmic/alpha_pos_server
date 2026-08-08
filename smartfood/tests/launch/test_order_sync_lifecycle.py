import json
from uuid import uuid4

import pytest
from django.utils import timezone


ORDERS_URL = '/api/smartfood/orders'


def _mark_till_live(cashier, active_shift):
    from base.services import presence

    presence.mark_device_live(
        active_shift.device_id,
        active_shift.branch_id,
        cashier.id,
    )


def _create_payload(product, address):
    return {
        'client_order_id': str(uuid4()),
        'items': [{'product_id': product.id, 'quantity': 2}],
        'order_type': 'DELIVERY',
        'address_id': address.id,
        'phone': '+998901234567',
        'payment_method': 'CARD',
        'note': 'Blue entrance',
        'tip': '2500.00',
        'points_used': 0,
    }


def _post_order(client, payload):
    return client.post(
        ORDERS_URL,
        data=json.dumps(payload),
        content_type='application/json',
    )


def _apply_local_chain(settings, customer_payload, order_payload, item_payload):
    from base.models import Customer as PosCustomer, Order, OrderItem
    from base.services.sync.service import SyncService

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = False
    results = []
    for model, payload in (
        (PosCustomer, customer_payload),
        (Order, order_payload),
        (OrderItem, item_payload),
    ):
        result = SyncService._apply_records(model, [payload])
        assert result['errors'] == []
        assert result['deferred'] == []
        assert result['created'] == 1
        results.append(result)
    return results


def _push_status_to_cloud(settings, order, status, previous_status):
    """Produce the branch payload, restore the simulated cloud copy, then ingest."""
    from base.models import Order
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    order.status = status
    update_fields = ['status']
    if status == Order.Status.READY:
        order.ready_at = timezone.now()
        update_fields.append('ready_at')
    order.save(update_fields=update_fields)
    payload = order.to_sync_dict()

    # This suite uses one database to stand in for two installations. Restore
    # the pre-push cloud state/version without signals, then run the real cloud
    # receiver against the branch payload.
    Order._base_manager.filter(pk=order.pk).update(
        status=previous_status,
        sync_version=max(0, int(payload['sync_version']) - 1),
    )
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    result = CloudReceiver.receive_batch(
        'order',
        'branch-a',
        [payload],
    )
    assert result['success'] is True, result
    assert result['acknowledged_uuids'] == [str(order.uuid)], result
    order.refresh_from_db()
    assert order.status == status
    return payload


@pytest.mark.django_db(transaction=True)
def test_customer_order_survives_dispatch_pull_and_branch_status_round_trip(
    monkeypatch,
    settings,
    auth_client,
    cfg,
    active_shift,
    cashier,
    product,
    customer,
    address,
):
    from base.models import Customer as PosCustomer, Order, OrderItem
    from smartfood.models import BotOrder, BotOrderDispatchJob
    from smartfood.services.dispatch_job_service import DispatchJobService

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    settings.SYNC_ENABLED = False
    settings.SMARTFOOD_AUTO_DISPATCH = True
    settings.CUSTOMER_BOT_TOKEN = ''
    events = []
    monkeypatch.setattr(
        'smartfood.realtime.publish_bot_order_event',
        lambda order_id, event: events.append((order_id, event)),
    )
    _mark_till_live(cashier, active_shift)

    response = _post_order(auth_client, _create_payload(product, address))

    assert response.status_code == 201, response.content
    assert DispatchJobService.process_due(limit=10) == {
        'claimed': 1,
        'completed': 1,
    }
    bot_order = BotOrder.objects.select_related('pos_order').get(
        id=response.json()['data']['id'],
    )
    job = BotOrderDispatchJob.objects.get(bot_order=bot_order)
    assert bot_order.status == BotOrder.Status.DISPATCHED
    assert job.status == BotOrderDispatchJob.Status.DONE
    assert job.finished_at is not None
    assert job.attempts == 1
    assert events.count((bot_order.id, 'dispatched')) == 1

    source_order = bot_order.pos_order
    source_item = OrderItem.objects.get(order=source_order)
    source_customer = PosCustomer.objects.get(pk=source_order.customer_id)
    assert source_order.order_origin == Order.Origin.TELEGRAM
    assert source_order.branch_id == active_shift.branch_id
    assert source_customer.branch_id == active_shift.branch_id
    assert source_item.branch_id == active_shift.branch_id
    assert source_order.delivery_address == address.line
    assert source_order.to_sync_dict()['delivery_address'] == address.line
    assert source_customer.synced_at is not None
    assert source_order.synced_at is not None
    assert source_item.synced_at is not None

    customer_payload = source_customer.to_sync_dict()
    order_payload = source_order.to_sync_dict()
    item_payload = source_item.to_sync_dict()
    customer_uuid = source_customer.uuid
    order_uuid = source_order.uuid
    item_uuid = source_item.uuid

    # Materialize the emitted cloud chain as a branch terminal. Removing the
    # source rows is only how a single test database represents the second DB.
    OrderItem._base_manager.filter(pk=source_item.pk).delete()
    Order._base_manager.filter(pk=source_order.pk).delete()
    PosCustomer._base_manager.filter(pk=source_customer.pk).delete()
    bot_order.refresh_from_db()
    assert bot_order.pos_order_id is None

    _apply_local_chain(
        settings,
        customer_payload,
        order_payload,
        item_payload,
    )
    pulled_customer = PosCustomer.objects.get(uuid=customer_uuid)
    pulled_order = Order.objects.get(uuid=order_uuid)
    pulled_item = OrderItem.objects.get(uuid=item_uuid)
    assert pulled_order.customer_id == pulled_customer.id
    assert pulled_item.order_id == pulled_order.id
    assert pulled_order.order_origin == Order.Origin.TELEGRAM
    assert pulled_order.delivery_address == address.line

    # In production the cloud's BotOrder link was never deleted. Restore that
    # link after the single-DB terminal simulation, then exercise the actual
    # branch receiver and customer tracking contract for both transitions.
    BotOrder.objects.filter(pk=bot_order.pk).update(pos_order=pulled_order)
    bot_order.refresh_from_db()

    status_events = events.count((bot_order.id, 'status'))
    _push_status_to_cloud(
        settings,
        pulled_order,
        Order.Status.READY,
        Order.Status.PREPARING,
    )
    assert events.count((bot_order.id, 'status')) > status_events
    ready_detail = auth_client.get(f'{ORDERS_URL}/{bot_order.id}')
    assert ready_detail.status_code == 200
    assert ready_detail.json()['data']['status'] == BotOrder.Status.DISPATCHED
    assert ready_detail.json()['data']['effective_status'] == Order.Status.READY
    active = auth_client.get(f'{ORDERS_URL}?status=active').json()['data']['items']
    assert [row['id'] for row in active] == [bot_order.id]

    status_events = events.count((bot_order.id, 'status'))
    _push_status_to_cloud(
        settings,
        pulled_order,
        Order.Status.COMPLETED,
        Order.Status.READY,
    )
    assert events.count((bot_order.id, 'status')) > status_events
    completed_detail = auth_client.get(f'{ORDERS_URL}/{bot_order.id}')
    assert completed_detail.status_code == 200
    assert completed_detail.json()['data']['effective_status'] == Order.Status.COMPLETED
    active = auth_client.get(f'{ORDERS_URL}?status=active').json()['data']['items']
    history = auth_client.get(f'{ORDERS_URL}?status=history').json()['data']['items']
    assert bot_order.id not in [row['id'] for row in active]
    assert bot_order.id in [row['id'] for row in history]
