"""Customer Mini App order WebSocket (Phase 0):
  * the per-order socket is scoped to the authenticated customer's OWN order;
  * anonymous / cross-customer / unknown-order sockets are rejected;
  * publish_bot_order_event targets the order's group with the right envelope;
  * cancel schedules a customer push.
"""
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.utils import timezone

# transaction=True so committed rows are visible to the consumer's worker thread
# AND so transaction.on_commit hooks actually fire.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _ws_env(settings):
    # In-memory layer (Redis group ops tangle the test loop); cloud auth gate on;
    # isolate from the license kill-switch.
    settings.CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}
    settings.OPEN_LAN = False
    settings.LICENSE_DEV_BYPASS = True


def _customer():
    from smartfood.models import Customer
    return Customer.objects.create(telegram_id=secrets.randbelow(10 ** 12) + 1)


def _token_for(customer):
    from smartfood.models import CustomerSession
    from smartfood.repositories import CustomerSessionRepository
    raw = secrets.token_hex(32)
    CustomerSession.objects.create(
        customer=customer, payload=CustomerSessionRepository.hash_token(raw),
        expires_at=timezone.now() + timedelta(hours=1))
    return raw


def _bot_order(customer):
    from smartfood.models import BotOrder
    return BotOrder.objects.create(
        customer=customer, status=BotOrder.Status.PENDING,
        order_type='DELIVERY', total=Decimal('10000'))


def _connect(path):
    async def _run():
        from config.asgi import application
        comm = WebsocketCommunicator(application, path)
        connected, _ = await comm.connect()
        await comm.disconnect()
        return connected
    return async_to_sync(_run)()


class TestCustomerOrderWsAuth:
    def test_owner_accepted(self):
        c = _customer()
        tok = _token_for(c)
        o = _bot_order(c)
        assert _connect(f'/ws/smartfood/orders/{o.id}/?token={tok}') is True

    def test_anonymous_rejected(self):
        c = _customer()
        o = _bot_order(c)
        assert _connect(f'/ws/smartfood/orders/{o.id}/') is False

    def test_cross_customer_rejected(self):
        owner = _customer()
        other = _customer()
        tok = _token_for(other)          # a DIFFERENT customer's token
        o = _bot_order(owner)
        assert _connect(f'/ws/smartfood/orders/{o.id}/?token={tok}') is False

    def test_unknown_order_rejected(self):
        c = _customer()
        tok = _token_for(c)
        assert _connect(f'/ws/smartfood/orders/999999/?token={tok}') is False

    def test_expired_token_rejected(self):
        from smartfood.models import CustomerSession
        from smartfood.repositories import CustomerSessionRepository
        c = _customer()
        o = _bot_order(c)
        raw = secrets.token_hex(32)
        CustomerSession.objects.create(
            customer=c, payload=CustomerSessionRepository.hash_token(raw),
            expires_at=timezone.now() - timedelta(minutes=1))   # already expired
        assert _connect(f'/ws/smartfood/orders/{o.id}/?token={raw}') is False


class TestPublisher:
    def test_publish_targets_order_group(self, monkeypatch):
        import smartfood.realtime as rt
        captured = {}
        monkeypatch.setattr(rt, '_group_send',
                            lambda group, message: captured.update(group=group, message=message))
        c = _customer()
        o = _bot_order(c)
        rt.publish_bot_order_event(o.id, 'dispatched')
        assert captured['group'] == f'botorder_{o.id}'
        assert captured['message']['type'] == 'order.event'
        assert captured['message']['event'] == 'dispatched'
        assert captured['message']['data']['id'] == o.id

    def test_publish_missing_order_is_noop(self, monkeypatch):
        import smartfood.realtime as rt
        calls = []
        monkeypatch.setattr(rt, '_group_send', lambda *a, **k: calls.append(a))
        rt.publish_bot_order_event(999999, 'dispatched')
        assert calls == []


class TestHooks:
    def test_cancel_publishes_after_commit(self, monkeypatch):
        import smartfood.realtime as rt
        calls = []
        monkeypatch.setattr(rt, 'publish_bot_order_event',
                            lambda oid, event: calls.append((oid, event)))
        c = _customer()
        o = _bot_order(c)
        from smartfood.services.order_service import BotOrderService
        res, st = BotOrderService.cancel(c, o.id)
        assert st == 200
        assert (o.id, 'canceled') in calls


class TestStatusBridge:
    def _pos_order(self):
        from base.models import Order, User
        u = User.objects.create(
            first_name='C', last_name='Z', email=f'pos-{secrets.token_hex(4)}@x.local',
            role='CASHIER', status='ACTIVE', password='!')
        return Order.objects.create(
            user=u, cashier=u, status='PREPARING', display_id=1,
            subtotal=Decimal('10000'), total_amount=Decimal('10000'))

    def test_linked_pos_status_change_publishes(self, monkeypatch):
        import smartfood.realtime as rt
        calls = []
        monkeypatch.setattr(rt, 'publish_bot_order_event',
                            lambda oid, event: calls.append((oid, event)))
        c = _customer()
        o = _bot_order(c)
        pos = self._pos_order()                      # created — bridge must NOT fire
        o.pos_order = pos
        o.status = 'DISPATCHED'
        o.save(update_fields=['pos_order', 'status', 'updated_at'])
        calls.clear()
        pos.status = 'READY'                         # later status change -> publish
        pos.save(update_fields=['status'])
        assert (o.id, 'status') in calls

    def test_unlinked_order_status_change_is_noop(self, monkeypatch):
        import smartfood.realtime as rt
        calls = []
        monkeypatch.setattr(rt, 'publish_bot_order_event',
                            lambda oid, event: calls.append((oid, event)))
        pos = self._pos_order()                      # no bot_order link
        pos.status = 'READY'
        pos.save(update_fields=['status'])
        assert calls == []
