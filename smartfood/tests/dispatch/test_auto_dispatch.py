"""Bot-order auto-dispatch to an active connected cashier.
POS (presence registry), and REJECT when no POS is online (product decision).
Plus auto courier-assign (Phase 4, default OFF)."""
import secrets
import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db


def _bot_order(customer, product):
    from smartfood.models import BotOrder, BotOrderItem
    o = BotOrder.objects.create(
        customer=customer, status=BotOrder.Status.PENDING, order_type='DELIVERY',
        phone_number=customer.phone_number, address_text='Amir Temur 12',
        address_lat=Decimal('41.311158'), address_lng=Decimal('69.279737'),
        subtotal=Decimal('39000'), total=Decimal('39000'))
    BotOrderItem.objects.create(
        bot_order=o, product=product, quantity=1,
        unit_price=Decimal('39000'), line_total=Decimal('39000'))
    return o


class TestAutoDispatch:
    def test_dispatches_to_connected_cashier(self, cfg, active_shift, cashier, product, customer):
        from base.services import presence
        from smartfood.services.dispatch_service import DispatchService
        presence.mark_device_live('till-1', 'branch-a', cashier.id)  # this till is online
        o = _bot_order(customer, product)
        body, st = DispatchService.auto_dispatch(o.id)
        assert st == 200, body
        o.refresh_from_db()
        assert o.status == 'DISPATCHED' and o.pos_order_id
        assert o.dispatched_cashier_id == cashier.id

    def test_rejects_when_no_pos_online(self, cfg, active_shift, cashier, product, customer):
        # cashier is on shift but NO presence heartbeat -> no connected POS -> reject
        from django.core.cache import cache
        from smartfood.services.dispatch_service import DispatchService
        cache.clear()
        o = _bot_order(customer, product)
        body, st = DispatchService.auto_dispatch(o.id)
        assert st == 200
        o.refresh_from_db()
        assert o.status == 'REJECTED' and o.pos_order_id is None

    def test_rejects_when_connected_cashier_has_no_active_shift(self, cfg, cashier, product, customer):
        # till online + reports the cashier, but the cashier has NO active shift
        from base.services import presence
        from smartfood.services.dispatch_service import DispatchService
        presence.mark_device_live('till-1', 'branch-a', cashier.id)
        o = _bot_order(customer, product)
        DispatchService.auto_dispatch(o.id)
        o.refresh_from_db()
        assert o.status == 'REJECTED'

    def test_connected_pos_endpoint(self, operator_client, cfg, cashier):
        from base.services import presence
        presence.mark_device_live('till-1', 'branch-a', cashier.id)
        r = operator_client.get('/api/admins/smartfood/pos/connected')
        assert r.status_code == 200, r.content
        items = r.json()['data']['items']
        assert any(i['device_id'] == 'till-1' and i['cashier_id'] == cashier.id
                   for i in items)


@pytest.mark.django_db(transaction=True)
class TestCreateAutoDispatchIntegration:
    """The durable worker dispatches accepted orders without operator action."""

    def test_create_auto_dispatches_when_pos_online(self, settings, cfg, active_shift,
                                                    cashier, product, customer, address):
        from base.services import presence
        from smartfood.services.order_service import BotOrderService
        from smartfood.models import BotOrder, BotOrderDispatchJob
        from smartfood.services.dispatch_job_service import DispatchJobService
        settings.SMARTFOOD_AUTO_DISPATCH = True
        presence.mark_device_live('till-1', 'branch-a', cashier.id)
        res, st = BotOrderService.create(
            customer, items=[{'product_id': product.id, 'quantity': 1}],
            order_type='DELIVERY', address_id=address.id,
            client_order_id=uuid.uuid4())
        assert st == 201, res
        bo = BotOrder.objects.get(id=res['data']['id'])
        job = BotOrderDispatchJob.objects.get(bot_order=bo)
        assert bo.status == 'PENDING' and bo.pos_order_id is None
        assert job.status == 'PENDING' and job.attempts == 0
        assert DispatchJobService.process_due(limit=10) == {
            'claimed': 1,
            'completed': 1,
        }
        bo.refresh_from_db()
        assert bo.status == 'DISPATCHED' and bo.pos_order_id

    def test_create_retries_durably_when_pos_disappears(self, settings, cfg, active_shift,
                                                        cashier, product, customer, address):
        from django.core.cache import cache
        from base.services import presence
        from smartfood.services.order_service import BotOrderService
        from smartfood.models import BotOrder, BotOrderDispatchJob
        from smartfood.services.dispatch_job_service import DispatchJobService
        settings.SMARTFOOD_AUTO_DISPATCH = True
        presence.mark_device_live('till-1', 'branch-a', cashier.id)
        res, st = BotOrderService.create(
            customer, items=[{'product_id': product.id, 'quantity': 1}],
            order_type='DELIVERY', address_id=address.id,
            client_order_id=uuid.uuid4())
        assert st == 201, res
        bo = BotOrder.objects.get(id=res['data']['id'])
        cache.clear()
        assert DispatchJobService.process_due(limit=10) == {
            'claimed': 1,
            'completed': 0,
        }
        assert bo.status == 'PENDING'
        job = BotOrderDispatchJob.objects.get(bot_order=bo)
        assert job.status == 'PENDING' and job.attempts == 1


@pytest.mark.django_db(transaction=True)
class TestAutoCourierAssign:
    """Phase 4: auto courier-assign is OFF by default; when enabled it hands a
    dispatched DELIVERY order to an available online courier. Manual assignment
    (POST /api/admins/couriers/assign) is always available regardless."""

    def _courier(self, online=True, branch_id='cloud'):
        from base.models import User
        from couriers.models import Courier
        u = User.objects.create(
            first_name='Co', last_name='Ur', email=f'cour-{secrets.token_hex(3)}@x.local',
            role='USER', status='ACTIVE', password='!')
        return Courier.objects.create(
            user=u, code=f'C{secrets.randbelow(9999)}', phone='+998900000000',
            branch_id=branch_id, online=online)

    def _dispatch(self, customer, product, cashier):
        from smartfood.services.dispatch_service import DispatchService
        o = _bot_order(customer, product)            # DELIVERY
        body, st = DispatchService.dispatch(o.id, cashier.id)
        assert st == 200, body
        o.refresh_from_db()
        return o

    def test_assigns_when_enabled(self, settings, cfg, active_shift, cashier, product, customer):
        settings.COURIER_AUTO_ASSIGN = True
        courier = self._courier(online=True, branch_id=active_shift.branch_id)
        o = self._dispatch(customer, product, cashier)
        from couriers.models import DeliveryAssignment
        assert DeliveryAssignment.objects.filter(
            order_id=o.pos_order_id, courier=courier).exists()

    def test_no_assign_when_disabled(self, settings, cfg, active_shift, cashier, product, customer):
        settings.COURIER_AUTO_ASSIGN = False
        self._courier(online=True, branch_id=active_shift.branch_id)
        o = self._dispatch(customer, product, cashier)
        from couriers.models import DeliveryAssignment
        assert not DeliveryAssignment.objects.filter(order_id=o.pos_order_id).exists()

    def test_no_assign_when_no_online_courier(self, settings, cfg, active_shift, cashier, product, customer):
        settings.COURIER_AUTO_ASSIGN = True
        self._courier(
            online=False, branch_id=active_shift.branch_id,
        )                                            # offline -> not eligible
        o = self._dispatch(customer, product, cashier)
        from couriers.models import DeliveryAssignment
        assert not DeliveryAssignment.objects.filter(order_id=o.pos_order_id).exists()
