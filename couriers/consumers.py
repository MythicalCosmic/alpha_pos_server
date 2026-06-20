"""WebSocket consumers for the courier layer (Django Channels, sync flavour so
they may touch the ORM in the threadpool — same pattern as core.realtime).

  CourierConsumer  /ws/courier/   -> the courier mobile app
  CashierConsumer  /ws/cashier/   -> the cashier desktop (courier-on-map)

Auth is enforced on connect (token in ?token= or Authorization header). Anon
sockets are rejected; a courier may only join its own group, a cashier only its
own branch group (§7 security).
"""
import logging
from urllib.parse import parse_qs

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer
from django.conf import settings

from couriers.realtime import courier_group, branch_group

logger = logging.getLogger('couriers.ws')

_CLOSE_AUTH = 4401
_CLOSE_FORBIDDEN = 4403

# sane bounds so a bad client can't relay garbage onto the desktop map
_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LNG_MIN, _LNG_MAX = -180.0, 180.0


def _handshake_token(scope):
    qs = parse_qs((scope.get('query_string') or b'').decode('utf-8', 'ignore'))
    if qs.get('token'):
        return qs['token'][0]
    for key, val in (scope.get('headers') or []):
        if key == b'authorization':
            v = val.decode('utf-8', 'ignore')
            for prefix in ('Token ', 'Bearer '):
                if v.startswith(prefix):
                    return v[len(prefix):].strip()
    return None


def _license_blocked():
    if getattr(settings, 'LICENSE_DEV_BYPASS', False):
        return False
    try:
        from licensing.services.state import get_state
        return get_state().is_blocked()
    except Exception:
        return False


def _session_user(token):
    if not token:
        return None
    try:
        from base.repositories.session import SessionRepository
        session = SessionRepository.get_by_session_key(token)
    except Exception:
        return None
    if not session or not session.user_id or session.user_id.is_deleted:
        return None
    # Enforce token lifetime, same as the REST path (couriers.auth.resolve_courier
    # / base.security): an expired session must NOT grant a live socket.
    if session.is_expired():
        try:
            SessionRepository.invalidate_cache(token)
        except Exception:
            pass
        return None
    if getattr(session.user_id, 'status', 'ACTIVE') != 'ACTIVE':
        return None
    return session.user_id


class CourierConsumer(JsonWebsocketConsumer):
    """The courier app connects here. Joins ``courier_<id>``; the server pushes
    order.assigned / order.ready / order.status / payment.* events. The app
    sends ``location.ping`` while delivering."""

    def connect(self):
        if _license_blocked():
            self.close(code=_CLOSE_FORBIDDEN)
            return
        user = _session_user(_handshake_token(self.scope))
        courier = getattr(user, 'courier', None) if user else None
        if courier is None:
            self.close(code=_CLOSE_AUTH)
            return
        self.courier_id = courier.id
        self.group = courier_group(courier.id)
        async_to_sync(self.channel_layer.group_add)(self.group, self.channel_name)
        self.accept()
        self.send_json({'event': 'connected', 'data': {'courier_id': courier.code}})

    def disconnect(self, code):
        if getattr(self, 'group', None):
            async_to_sync(self.channel_layer.group_discard)(self.group, self.channel_name)

    # --- inbound: courier -> server ---------------------------------------- #
    def receive_json(self, content, **kwargs):
        if content.get('event') == 'location.ping':
            self._handle_location_ping(content.get('data') or {})

    def _handle_location_ping(self, data):
        try:
            lat = float(data['lat'])
            lng = float(data['lng'])
        except (KeyError, TypeError, ValueError):
            return
        if not (_LAT_MIN <= lat <= _LAT_MAX and _LNG_MIN <= lng <= _LNG_MAX):
            return
        from couriers.models import Courier
        from couriers import services
        courier = Courier.objects.select_related('user').filter(pk=self.courier_id).first()
        if not courier:
            return
        # Single funnel: stores last-known + trail breadcrumb and relays to the
        # cashier map (scoped to share_loc / active delivery inside the service).
        services.update_location(courier, lat, lng)

    # --- group_send handlers ----------------------------------------------- #
    def courier_event(self, message):
        self.send_json({'event': message['event'], 'data': message['data']})


class CashierConsumer(JsonWebsocketConsumer):
    """The cashier desktop connects here to watch couriers on the map. Joins
    ``branch_<branch_id>`` (from ?branch= or the staff user's branch). Receives
    courier.location / order.status / order.delivered."""

    # Roles allowed to watch ANY branch's courier map (multi-branch back-office).
    _ALL_BRANCH_ROLES = ('ADMIN', 'MANAGER')

    def connect(self):
        if _license_blocked():
            self.close(code=_CLOSE_FORBIDDEN)
            return
        qs = parse_qs((self.scope.get('query_string') or b'').decode('utf-8', 'ignore'))
        requested = (qs.get('branch') or [None])[0]
        # Trusted-LAN desktop edition would set OPEN_LAN; on the cloud server we
        # require a staff session AND that the user may watch the requested branch.
        if not getattr(settings, 'OPEN_LAN', False):
            user = _session_user(_handshake_token(self.scope))
            if user is None:
                self.close(code=_CLOSE_AUTH)
                return
            # A courier account is not a cashier — it must not subscribe to the
            # branch operations/location feed.
            if getattr(user, 'courier', None) is not None:
                self.close(code=_CLOSE_FORBIDDEN)
                return
            role = getattr(user, 'role', '')
            own_branch = getattr(user, 'branch_id', '') or ''
            if role not in self._ALL_BRANCH_ROLES:
                # Non-admins are pinned to their own branch; ignore/deny a
                # mismatching client-supplied ?branch= (cross-branch IDOR).
                if requested and own_branch and requested != own_branch:
                    self.close(code=_CLOSE_FORBIDDEN)
                    return
                requested = own_branch or requested
        branch = requested or getattr(settings, 'BRANCH_ID', 'cloud')
        self.group = branch_group(branch)
        async_to_sync(self.channel_layer.group_add)(self.group, self.channel_name)
        self.accept()
        self.send_json({'event': 'connected', 'data': {'branch_id': branch}})

    def disconnect(self, code):
        if getattr(self, 'group', None):
            async_to_sync(self.channel_layer.group_discard)(self.group, self.channel_name)

    def cashier_event(self, message):
        self.send_json({'event': message['event'], 'data': message['data']})
