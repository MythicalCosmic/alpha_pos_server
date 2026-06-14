"""Realtime WebSocket auth gate: on the cloud server (OPEN_LAN off) the order/KDS/
cashier sockets must reject anonymous connections; the LAN appliance (OPEN_LAN)
accepts; a valid staff session token is accepted; the cashier-control channel
requires an elevated role."""
import secrets
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.utils import timezone

# transaction=True so rows are committed and visible to the consumer's worker thread.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _inmemory_channel_layer(settings):
    # Avoid the Redis channel layer in WS unit tests (group_add over async_to_sync
    # tangles the test event loop); the auth gate runs before any group_add anyway.
    settings.CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


def _try_connect(path):
    """Run the full handshake in ONE event loop; return whether it was accepted."""
    async def _run():
        from config.asgi import application
        comm = WebsocketCommunicator(application, path)
        connected, _ = await comm.connect()
        await comm.disconnect()
        return connected
    return async_to_sync(_run)()


def _staff_token(role='CASHIER'):
    from base.models import User, Session
    from base.repositories import SessionRepository
    u = User.objects.create(first_name='S', last_name='T', email=f'ws-{secrets.token_hex(4)}@x.local',
                            role=role, status='ACTIVE', password='!')
    raw = secrets.token_hex(32)
    Session.objects.create(user_id=u, payload=SessionRepository.hash_token(raw),
                           user_agent='', expires_at=timezone.now() + timedelta(hours=1))
    return raw


class TestWebsocketAuth:
    def test_anonymous_rejected_on_cloud_server(self, settings):
        settings.OPEN_LAN = False
        settings.LICENSE_DEV_BYPASS = True   # isolate the auth gate from the license gate
        assert _try_connect('/ws/orders/') is False

    def test_open_lan_accepts_anonymous(self, settings):
        settings.OPEN_LAN = True
        settings.LICENSE_DEV_BYPASS = True
        assert _try_connect('/ws/orders/') is True

    def test_valid_staff_token_accepted(self, settings):
        settings.OPEN_LAN = False
        settings.LICENSE_DEV_BYPASS = True
        assert _try_connect(f'/ws/orders/?token={_staff_token("CASHIER")}') is True

    def test_cashier_control_requires_elevated_role(self, settings):
        settings.OPEN_LAN = False
        settings.LICENSE_DEV_BYPASS = True
        # a plain cashier session must NOT open the server->till control channel
        assert _try_connect(f'/ws/cashiers/?token={_staff_token("CASHIER")}') is False
        # a manager session may
        assert _try_connect(f'/ws/cashiers/?token={_staff_token("MANAGER")}') is True
