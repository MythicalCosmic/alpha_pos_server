"""Money recompute, the cashier-dispatch flow (attribution + price integrity),
tracking and IDOR."""
import json
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

C = '/api/smartfood'
A = '/api/admins/smartfood'


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type='application/json')


def _line(product, size=None, toppings=(), qty=1):
    item = {'product_id': product.id, 'quantity': qty}
    if size is not None:
        item['size_id'] = size.id
    if toppings:
        item['topping_ids'] = [t.id for t in toppings]
    return item


# --------------------------------------------------------------------------- #
#  Money recompute                                                            #
# --------------------------------------------------------------------------- #
class TestMoney:
    def test_quote_sums_base_size_toppings(self, auth_client, cfg, product):
        # 39000 + 10000(large) + 6000(cheese) + 8000(bacon) = 63000, x2 = 126000
        items = [_line(product, product._large, [product._cheese, product._bacon], qty=2)]
        r = _post(auth_client, f'{C}/cart/quote', {'items': items, 'order_type': 'DELIVERY'})
        assert r.status_code == 200, r.content
        d = r.json()['data']
        assert d['lines'][0]['unit_price'] == 63000
        assert d['subtotal'] == 126000
        assert d['free_delivery_applied'] is True and d['delivery_fee'] == 0
        assert d['total'] == 126000

    def test_quote_charges_delivery_under_threshold(self, auth_client, cfg, product):
        r = _post(auth_client, f'{C}/cart/quote', {'items': [_line(product)], 'order_type': 'DELIVERY'})
        d = r.json()['data']
        assert d['subtotal'] == 39000 and d['delivery_fee'] == 12000 and d['total'] == 51000

    def test_quote_rejects_too_many_toppings(self, auth_client, cfg, product):
        product._group.max_select = 1
        product._group.save()
        items = [_line(product, toppings=[product._cheese, product._bacon])]
        r = _post(auth_client, f'{C}/cart/quote', {'items': items, 'order_type': 'DELIVERY'})
        assert r.status_code == 422 and r.json()['code'] == 'topping_max'

    def test_loyalty_redemption_discounts_total(self, auth_client, cfg, active_shift, product, address, customer):
        customer.loyalty_points = 50
        customer.save()
        # 1x base 39000 + 12000 delivery - 50pts*100 = 5000 discount = 46000
        r = _post(auth_client, f'{C}/orders', {
            'items': [_line(product)], 'order_type': 'DELIVERY',
            'address_id': address.id, 'points_used': 50,
        })
        assert r.status_code == 201, r.content
        d = r.json()['data']
        assert d['totals']['discount'] == 5000 and d['totals']['total'] == 46000
        customer.refresh_from_db()
        assert customer.loyalty_points == 0   # reserved

    def test_server_ignores_client_prices(self, auth_client, cfg, product):
        # client tries to send its own unit/total — server recomputes from scratch
        items = [{'product_id': product.id, 'quantity': 1, 'unit': 1, 'price': 1}]
        r = _post(auth_client, f'{C}/cart/quote', {'items': items, 'order_type': 'PICKUP', 'total': 1})
        assert r.json()['data']['subtotal'] == 39000


# --------------------------------------------------------------------------- #
#  Stop-selling at submit                                                     #
# --------------------------------------------------------------------------- #
class TestStopSellingAtSubmit:
    def test_sold_out_between_quote_and_order_is_rejected(self, auth_client, cfg, active_shift, product, address):
        product.bot.is_selling = False
        product.bot.save()
        r = _post(auth_client, f'{C}/orders', {
            'items': [_line(product)], 'order_type': 'DELIVERY', 'address_id': address.id})
        assert r.status_code == 409 and r.json()['code'] == 'item_unavailable'

    def test_stopped_category_blocks_order_submit(self, auth_client, cfg, active_shift, product, address, category):
        # Browse hides a stopped category's products; submit must reject them too.
        category.bot.is_selling = False
        category.bot.save()
        r = _post(auth_client, f'{C}/orders', {
            'items': [_line(product)], 'order_type': 'DELIVERY', 'address_id': address.id})
        assert r.status_code == 409 and r.json()['code'] == 'item_unavailable'


# --------------------------------------------------------------------------- #
#  Dispatch — attribution + price integrity (THE core)                       #
# --------------------------------------------------------------------------- #
class TestDispatch:
    def _place(self, auth_client, product, address, size=None, toppings=()):
        r = _post(auth_client, f'{C}/orders', {
            'items': [_line(product, size, toppings)], 'order_type': 'DELIVERY',
            'address_id': address.id})
        assert r.status_code == 201, r.content
        return r.json()['data']['id']

    def test_dispatch_creates_pos_order_under_cashier_with_full_price(
            self, auth_client, operator_client, cfg, active_shift, cashier, product, address):
        bo_id = self._place(auth_client, product, address, product._large,
                            [product._cheese, product._bacon])
        r = _post(operator_client, f'{A}/orders/{bo_id}/dispatch', {'cashier_id': cashier.id})
        assert r.status_code == 200, r.content
        pos_id = r.json()['data']['pos_order_id']

        from base.models import Order, OrderItem
        from smartfood.models import BotOrder
        order = Order.objects.get(id=pos_id)
        assert order.cashier_id == cashier.id                 # attributed to THIS cashier
        assert order.order_type == 'DELIVERY'
        item = OrderItem.objects.get(order=order)
        # line price carries size + toppings (NOT base 39000): 63000
        assert item.price == Decimal('63000.00')
        assert order.subtotal == Decimal('63000.00')        # food
        # total_amount = what the customer pays: food 63000 + 12000 delivery
        # (63000 < 100000 free-delivery threshold), no discount = 75000
        assert order.total_amount == Decimal('75000.00')
        bo = BotOrder.objects.get(id=bo_id)
        assert bo.status == 'DISPATCHED' and bo.pos_order_id == pos_id
        assert bo.dispatched_cashier_id == cashier.id

    def test_dispatch_to_cashier_not_on_shift_fails(self, auth_client, operator_client, cfg,
                                                    active_shift, product, address, db):
        from base.models import User
        idle = User.objects.create(first_name='Idle', last_name='C', email='idle@x.local',
                                   role='CASHIER', status='ACTIVE', password='!')
        bo_id = self._place(auth_client, product, address)
        r = _post(operator_client, f'{A}/orders/{bo_id}/dispatch', {'cashier_id': idle.id})
        assert r.status_code == 400
        from smartfood.models import BotOrder
        assert BotOrder.objects.get(id=bo_id).status == 'PENDING'

    def test_double_dispatch_conflict(self, auth_client, operator_client, cfg, active_shift,
                                      cashier, product, address):
        bo_id = self._place(auth_client, product, address)
        assert _post(operator_client, f'{A}/orders/{bo_id}/dispatch',
                     {'cashier_id': cashier.id}).status_code == 200
        r2 = _post(operator_client, f'{A}/orders/{bo_id}/dispatch', {'cashier_id': cashier.id})
        assert r2.status_code == 409

    def test_reject_creates_no_pos_order_and_refunds_points(self, auth_client, operator_client,
                                                            cfg, active_shift, product, address, customer):
        customer.loyalty_points = 50
        customer.save()
        bo_id = self._place_with_points(auth_client, product, address)
        r = _post(operator_client, f'{A}/orders/{bo_id}/reject', {'reason': 'out of range'})
        assert r.status_code == 200
        from smartfood.models import BotOrder
        bo = BotOrder.objects.get(id=bo_id)
        assert bo.status == 'REJECTED' and bo.pos_order_id is None
        customer.refresh_from_db()
        assert customer.loyalty_points == 50   # refunded

    def _place_with_points(self, auth_client, product, address):
        r = _post(auth_client, f'{C}/orders', {
            'items': [_line(product)], 'order_type': 'DELIVERY',
            'address_id': address.id, 'points_used': 50})
        assert r.status_code == 201, r.content
        return r.json()['data']['id']

    def test_pending_queue_and_active_cashiers(self, auth_client, operator_client, cfg,
                                               active_shift, cashier, product, address):
        self._place(auth_client, product, address)
        q = operator_client.get(f'{A}/orders/pending')
        assert q.status_code == 200 and len(q.json()['data']['items']) == 1
        ac = operator_client.get(f'{A}/cashiers/active')
        assert cashier.id in [c['cashier_id'] for c in ac.json()['data']['items']]

    def test_dispatch_total_reflects_delivery_and_discount(self, auth_client, operator_client, cfg,
                                                           active_shift, cashier, product, address, customer):
        customer.loyalty_points = 50
        customer.save()
        # 1x base 39000 + 12000 delivery - 5000 (50pts * 100) = 46000 payable
        bo_id = _post(auth_client, f'{C}/orders', {
            'items': [_line(product)], 'order_type': 'DELIVERY',
            'address_id': address.id, 'points_used': 50}).json()['data']['id']
        r = _post(operator_client, f'{A}/orders/{bo_id}/dispatch', {'cashier_id': cashier.id})
        assert r.status_code == 200, r.content
        from base.models import Order
        o = Order.objects.get(id=r.json()['data']['pos_order_id'])
        assert o.subtotal == Decimal('39000.00')         # food
        assert o.discount_amount == Decimal('5000.00')   # allocated to product analytics
        assert o.total_amount == Decimal('46000.00')     # food + delivery - loyalty discount


# --------------------------------------------------------------------------- #
#  Orders / tracking / IDOR                                                   #
# --------------------------------------------------------------------------- #
class TestOrdersTracking:
    def test_idor_cannot_see_others_order(self, auth_client, client, cfg, active_shift,
                                          product, address, db):
        bo_id = _post(auth_client, f'{C}/orders', {
            'items': [_line(product)], 'order_type': 'DELIVERY',
            'address_id': address.id}).json()['data']['id']
        # a second customer with their own token
        import secrets
        from datetime import timedelta
        from django.utils import timezone
        from smartfood.models import Customer, CustomerSession
        from smartfood.repositories import CustomerSessionRepository
        other = Customer.objects.create(telegram_id=42, first_name='Eve')
        raw = secrets.token_hex(32)
        CustomerSession.objects.create(customer=other, payload=CustomerSessionRepository.hash_token(raw),
                                       expires_at=timezone.now() + timedelta(hours=1))
        client.defaults['HTTP_AUTHORIZATION'] = 'Bearer ' + raw
        assert client.get(f'{C}/orders/{bo_id}').status_code == 404

    def test_track_reflects_pos_order_after_dispatch(self, auth_client, operator_client, cfg,
                                                     active_shift, cashier, product, address):
        bo_id = _post(auth_client, f'{C}/orders', {
            'items': [_line(product)], 'order_type': 'DELIVERY',
            'address_id': address.id}).json()['data']['id']
        before = auth_client.get(f'{C}/orders/{bo_id}/track').json()['data']
        assert before['status'] == 'PENDING' and before['pos_order'] is None
        _post(operator_client, f'{A}/orders/{bo_id}/dispatch', {'cashier_id': cashier.id})
        after = auth_client.get(f'{C}/orders/{bo_id}/track').json()['data']
        assert after['status'] == 'DISPATCHED' and after['pos_order']['uuid']

    def test_cancel_pending(self, auth_client, cfg, active_shift, product, address):
        bo_id = _post(auth_client, f'{C}/orders', {
            'items': [_line(product)], 'order_type': 'DELIVERY',
            'address_id': address.id}).json()['data']['id']
        r = _post(auth_client, f'{C}/orders/{bo_id}/cancel', {})
        assert r.status_code == 200 and r.json()['data']['status'] == 'CANCELED'
