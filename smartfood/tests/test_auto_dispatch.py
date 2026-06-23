"""WS Phase 3: auto-dispatch a bot order to the active cashier on a CONNECTED
POS (presence registry), and REJECT when no POS is online (product decision).
Plus auto courier-assign (Phase 4, default OFF)."""
import secrets
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db


def _bot_order(customer, product):
    from smartfood.models import BotOrder, BotOrderItem
    o = BotOrder.objects.create(
        customer=customer, status=BotOrder.Status.PENDING, order_type='DELIVERY',
        subtotal=Decimal('39000'), total=Decimal('39000'))
    BotOrderItem.objects.create(
        bot_order=o, product=product, quantity=1,
        unit_price=Decimal('39000'), line_total=Decimal('39000'))
    return o


class TestAutoDispatch:
    def test_dispatches_to_connected_cashier(self, cfg, active_shift, cashier, product, customer):
        from base.services import presence
        from smartfood.services.dispatch_service import DispatchService
        presence.mark_device_live('till-1', 'cloud', cashier.id)     # this till is online
        o = _bot_order(customer, product)
        body, st = DispatchService.auto_dispatch(o.id)
        assert st == 200, body
        o.refresh_from_db()
        assert o.status == 'DISPATCHED' and o.pos_order_id
        assert o.dispatched_cashier_id == cashier.id

    def test_rejects_when_no_pos_online(self, cfg, active_shift, cashier, product, customer):
        # cashier is on shift but NO presence heartbeat -> no connected POS -> reject
        from smartfood.services.dispatch_service import DispatchService
        o = _bot_order(customer, product)
        body, st = DispatchService.auto_dispatch(o.id)
        assert st == 200
        o.refresh_from_db()
        assert o.status == 'REJECTED' and o.pos_order_id is None

    def test_rejects_when_connected_cashier_has_no_active_shift(self, cfg, cashier, product, customer):
        # till online + reports the cashier, but the cashier has NO active shift
        from base.services import presence
        from smartfood.services.dispatch_service import DispatchService
        presence.mark_device_live('till-1', 'cloud', cashier.id)
        o = _bot_order(customer, product)
        DispatchService.auto_dispatch(o.id)
        o.refresh_from_db()
        assert o.status == 'REJECTED'

    def test_connected_pos_endpoint(self, operator_client, cfg, cashier):
        from base.services import presence
        presence.mark_device_live('till-1', 'cloud', cashier.id)
        r = operator_client.get('/api/admins/smartfood/pos/connected')
        assert r.status_code == 200, r.content
        items = r.json()['data']['items']
        assert any(i['device_id'] == 'till-1' and i['cashier_id'] == cashier.id
                   for i in items)


@pytest.mark.django_db(transaction=True)
class TestCreateAutoDispatchIntegration:
    """End-to-end: placing an order auto-dispatches it (on_commit) when a POS is
    online — the customer's order lands on a cashier with no operator action."""

    def test_create_auto_dispatches_when_pos_online(self, settings, cfg, active_shift,
                                                    cashier, product, customer, address):
        from base.services import presence
        from smartfood.services.order_service import BotOrderService
        from smartfood.models import BotOrder
        settings.SMARTFOOD_AUTO_DISPATCH = True
        presence.mark_device_live('till-1', 'cloud', cashier.id)
        res, st = BotOrderService.create(
            customer, items=[{'product_id': product.id, 'quantity': 1}],
            order_type='DELIVERY', address_id=address.id)
        assert st == 201, res
        bo = BotOrder.objects.get(id=res['data']['id'])
        assert bo.status == 'DISPATCHED' and bo.pos_order_id   # on_commit -> auto_dispatch

    def test_create_rejects_when_no_pos_online(self, settings, cfg, active_shift,
                                               cashier, product, customer, address):
        from smartfood.services.order_service import BotOrderService
        from smartfood.models import BotOrder
        settings.SMARTFOOD_AUTO_DISPATCH = True            # no presence heartbeat
        res, st = BotOrderService.create(
            customer, items=[{'product_id': product.id, 'quantity': 1}],
            order_type='DELIVERY', address_id=address.id)
        assert st == 201
        bo = BotOrder.objects.get(id=res['data']['id'])
        assert bo.status == 'REJECTED'


@pytest.mark.django_db(transaction=True)
class TestAutoCourierAssign:
    """Phase 4: auto courier-assign is OFF by default; when enabled it hands a
    dispatched DELIVERY order to an available online courier. Manual assignment
    (POST /api/admins/couriers/assign) is always available regardless."""

    def _courier(self, online=True):
        from base.models import User
        from couriers.models import Courier
        u = User.objects.create(
            first_name='Co', last_name='Ur', email=f'cour-{secrets.token_hex(3)}@x.local',
            role='USER', status='ACTIVE', password='!')
        return Courier.objects.create(
            user=u, code=f'C{secrets.randbelow(9999)}', phone='+998900000000',
            branch_id='cloud', online=online)

    def _dispatch(self, customer, product, cashier):
        from smartfood.services.dispatch_service import DispatchService
        o = _bot_order(customer, product)            # DELIVERY
        body, st = DispatchService.dispatch(o.id, cashier.id)
        assert st == 200, body
        o.refresh_from_db()
        return o

    def test_assigns_when_enabled(self, settings, cfg, active_shift, cashier, product, customer):
        settings.COURIER_AUTO_ASSIGN = True
        courier = self._courier(online=True)
        o = self._dispatch(customer, product, cashier)
        from couriers.models import DeliveryAssignment
        assert DeliveryAssignment.objects.filter(
            order_id=o.pos_order_id, courier=courier).exists()

    def test_no_assign_when_disabled(self, settings, cfg, active_shift, cashier, product, customer):
        settings.COURIER_AUTO_ASSIGN = False
        self._courier(online=True)
        o = self._dispatch(customer, product, cashier)
        from couriers.models import DeliveryAssignment
        assert not DeliveryAssignment.objects.filter(order_id=o.pos_order_id).exists()

    def test_no_assign_when_no_online_courier(self, settings, cfg, active_shift, cashier, product, customer):
        settings.COURIER_AUTO_ASSIGN = True
        self._courier(online=False)                  # offline -> not eligible
        o = self._dispatch(customer, product, cashier)
        from couriers.models import DeliveryAssignment
        assert not DeliveryAssignment.objects.filter(order_id=o.pos_order_id).exists()
