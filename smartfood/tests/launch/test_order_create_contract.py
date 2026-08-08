import json
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction


pytestmark = pytest.mark.django_db

ORDERS_URL = '/api/smartfood/orders'


def _post(client, payload):
    return client.post(
        ORDERS_URL,
        data=json.dumps(payload),
        content_type='application/json',
    )


def _valid_payload(product, address, *, client_order_id=None):
    return {
        'client_order_id': str(client_order_id or uuid4()),
        'items': [{'product_id': product.id, 'quantity': 1}],
        'order_type': 'DELIVERY',
        'address_id': address.id,
        'payment_method': 'CASH',
        'tip': 0,
        'points_used': 0,
        'note': 'Leave at reception',
    }


def _mark_till_live(cashier, active_shift):
    from base.services import presence

    presence.mark_device_live(
        active_shift.device_id,
        active_shift.branch_id,
        cashier.id,
    )


def _assert_stable_client_error(first, second=None):
    assert 400 <= first.status_code < 500, first.content
    body = first.json()
    assert body.get('success') is False, body
    assert isinstance(body.get('message'), str) and body['message'].strip(), body
    if second is not None:
        assert second.status_code == first.status_code
        other = second.json()
        assert other.get('success') is False
        assert other.get('code') == body.get('code')
        assert other.get('message') == body.get('message')
    return body


class TestClientOrderIdContract:
    def test_missing_and_malformed_client_order_id_are_rejected_before_create(
        self,
        settings,
        auth_client,
        cfg,
        active_shift,
        cashier,
        product,
        address,
    ):
        from smartfood.models import BotOrder

        settings.SMARTFOOD_AUTO_DISPATCH = False
        _mark_till_live(cashier, active_shift)

        missing = _valid_payload(product, address)
        missing.pop('client_order_id')
        malformed = _valid_payload(product, address)
        malformed['client_order_id'] = 'not-a-uuid'

        for payload in (missing, malformed):
            response = _post(auth_client, payload)
            assert response.status_code == 422, response.content
            assert response.json()['code'] == 'invalid_client_order_id'

        assert BotOrder.objects.count() == 0

    def test_same_customer_same_key_and_payload_replays_original_order(
        self,
        settings,
        auth_client,
        customer,
        cfg,
        active_shift,
        cashier,
        product,
        address,
    ):
        from smartfood.models import BotOrder, BotOrderItem

        settings.SMARTFOOD_AUTO_DISPATCH = False
        _mark_till_live(cashier, active_shift)
        client_order_id = uuid4()
        payload = _valid_payload(
            product,
            address,
            client_order_id=client_order_id,
        )

        created = _post(auth_client, payload)
        # The terminal may disappear after the first response. Recovery of the
        # already accepted request must not depend on current availability.
        cache.clear()
        replayed = _post(auth_client, deepcopy(payload))

        assert created.status_code == 201, created.content
        assert replayed.status_code == 200, replayed.content
        assert replayed.json()['data']['id'] == created.json()['data']['id']
        assert replayed.json()['data']['client_order_id'] == str(client_order_id)
        assert BotOrder.objects.filter(
            customer=customer,
            client_order_id=client_order_id,
        ).count() == 1
        assert BotOrderItem.objects.count() == 1

    def test_same_customer_same_key_with_changed_payload_conflicts_without_mutation(
        self,
        settings,
        auth_client,
        customer,
        cfg,
        active_shift,
        cashier,
        product,
        address,
    ):
        from smartfood.models import BotOrder, BotOrderItem

        settings.SMARTFOOD_AUTO_DISPATCH = False
        _mark_till_live(cashier, active_shift)
        client_order_id = uuid4()
        original = _valid_payload(
            product,
            address,
            client_order_id=client_order_id,
        )
        changed = deepcopy(original)
        changed['note'] = 'A different request under the same key'

        created = _post(auth_client, original)
        cache.clear()
        conflict = _post(auth_client, changed)

        assert created.status_code == 201, created.content
        assert conflict.status_code == 409, conflict.content
        assert conflict.json()['code'] == 'idempotency_conflict'
        order = BotOrder.objects.get(
            customer=customer,
            client_order_id=client_order_id,
        )
        assert order.note == original['note']
        assert BotOrderItem.objects.filter(bot_order=order).count() == 1

    def test_database_constraint_is_last_line_of_duplicate_defense(
        self,
        settings,
        auth_client,
        customer,
        cfg,
        active_shift,
        cashier,
        product,
        address,
    ):
        from smartfood.models import BotOrder

        settings.SMARTFOOD_AUTO_DISPATCH = False
        _mark_till_live(cashier, active_shift)
        client_order_id = uuid4()
        response = _post(
            auth_client,
            _valid_payload(
                product,
                address,
                client_order_id=client_order_id,
            ),
        )
        assert response.status_code == 201, response.content

        with pytest.raises(IntegrityError), transaction.atomic():
            BotOrder.objects.create(
                customer=customer,
                client_order_id=client_order_id,
                request_fingerprint='different-request',
            )


class TestConnectedCashierGate:
    def test_open_shift_without_live_terminal_does_not_accept_order(
        self,
        settings,
        auth_client,
        cfg,
        active_shift,
        product,
        address,
    ):
        from smartfood.models import BotOrder

        settings.SMARTFOOD_AUTO_DISPATCH = False
        cache.clear()
        response = _post(auth_client, _valid_payload(product, address))

        assert response.status_code == 200, response.content
        assert response.json()['closed'] is True
        assert response.json()['reason'] == 'no_cashier'
        assert BotOrder.objects.count() == 0

    def test_live_terminal_with_matching_active_shift_accepts_order(
        self,
        settings,
        auth_client,
        cfg,
        active_shift,
        cashier,
        product,
        address,
    ):
        settings.SMARTFOOD_AUTO_DISPATCH = False
        _mark_till_live(cashier, active_shift)

        response = _post(auth_client, _valid_payload(product, address))

        assert response.status_code == 201, response.content


class TestMalformedCreatePayloads:
    @pytest.mark.parametrize(
        'mutation',
        [
            lambda p: p.update(items='not-a-list'),
            lambda p: p.update(items=[None]),
            lambda p: p.update(items=[{'product_id': True, 'quantity': 1}]),
            lambda p: p['items'][0].update(quantity=True),
            lambda p: p['items'][0].update(quantity=1.5),
            lambda p: p['items'][0].update(quantity=1000000),
            lambda p: p['items'][0].update(topping_ids='not-a-list'),
            lambda p: p.update(tip='NaN'),
            lambda p: p.update(tip='Infinity'),
            lambda p: p.update(tip={}),
            lambda p: p.update(tip='100000000000000000000000'),
            lambda p: p.update(points_used='not-an-integer'),
            lambda p: p.update(points_used=1.5),
            lambda p: p.update(points_used=-1),
            lambda p: p.update(points_used=True),
            lambda p: p.update(order_type='HALL'),
            lambda p: p.update(payment_method='PAYME'),
            lambda p: p.update(phone='9' * 21),
            lambda p: p.update(note='n' * 5001),
            lambda p: p.update(items=p['items'] * 101),
        ],
        ids=[
            'items-object-type',
            'item-object-type',
            'boolean-product-id',
            'boolean-quantity',
            'fractional-quantity',
            'unbounded-quantity',
            'topping-ids-type',
            'nan-tip',
            'infinite-tip',
            'object-tip',
            'unbounded-tip',
            'text-points',
            'fractional-points',
            'negative-points',
            'boolean-points',
            'unknown-order-type',
            'unknown-payment-method',
            'overlong-phone',
            'overlong-note',
            'too-many-lines',
        ],
    )
    def test_malformed_domain_payload_is_a_repeatable_4xx_and_never_creates(
        self,
        mutation,
        settings,
        auth_client,
        cfg,
        active_shift,
        cashier,
        product,
        address,
    ):
        from smartfood.models import BotOrder

        settings.SMARTFOOD_AUTO_DISPATCH = False
        _mark_till_live(cashier, active_shift)
        payload = _valid_payload(product, address)
        mutation(payload)

        first = _post(auth_client, payload)
        second = _post(auth_client, payload)

        _assert_stable_client_error(first, second)
        assert BotOrder.objects.count() == 0

    def test_malformed_json_and_non_object_json_are_stable_400s(
        self,
        auth_client,
        cfg,
        active_shift,
        cashier,
    ):
        _mark_till_live(cashier, active_shift)
        for raw in ('{"broken"', '[]', 'null'):
            first = auth_client.post(
                ORDERS_URL,
                data=raw,
                content_type='application/json',
            )
            second = auth_client.post(
                ORDERS_URL,
                data=raw,
                content_type='application/json',
            )
            _assert_stable_client_error(first, second)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor == 'sqlite',
    reason=(
        'SQLite serializes writers with a database-wide lock; production '
        'concurrency is exercised on PostgreSQL.'
    ),
)
def test_concurrent_identical_create_requests_converge_on_one_order(
    settings,
    raw_token,
    customer,
    cfg,
    active_shift,
    cashier,
    product,
    address,
):
    from django.db import close_old_connections
    from django.test import Client
    from smartfood.models import BotOrder, BotOrderItem

    settings.SMARTFOOD_AUTO_DISPATCH = False
    _mark_till_live(cashier, active_shift)
    payload = _valid_payload(product, address)
    barrier = Barrier(2)

    def create_once():
        close_old_connections()
        try:
            client = Client(HTTP_AUTHORIZATION='Bearer ' + raw_token)
            barrier.wait(timeout=5)
            response = _post(client, payload)
            return response.status_code, response.json()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: create_once(), range(2)))

    assert sorted(status for status, _body in responses) == [200, 201]
    assert len({body['data']['id'] for _status, body in responses}) == 1
    assert BotOrder.objects.filter(
        customer=customer,
        client_order_id=payload['client_order_id'],
    ).count() == 1
    assert BotOrderItem.objects.count() == 1
