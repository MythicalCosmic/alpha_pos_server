from datetime import timedelta
import importlib
import json
import secrets
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.cache import cache
from django.db import connection
from django.db.models.signals import post_delete
from django.http import JsonResponse
from django.test import Client, RequestFactory
from django.utils import timezone

from base.models import Session, User
from base.repositories.session import SessionRepository
from couriers.auth import courier_required, resolve_courier
from couriers.consumers import _handshake_token
from couriers.models import Courier
from couriers.tokens import (
    CourierTokenAudienceError,
    issue_login_claim,
    issue_session,
)


pytestmark = pytest.mark.django_db


def _identity(*, role='MANAGER', phone='998901234567'):
    user = User.objects.create(
        first_name='Role',
        last_name='Drift',
        email=f'role-drift-{phone}@example.com',
        password='!',
        role=role,
        status=User.UserStatus.ACTIVE,
        synced_at=timezone.now() - timedelta(days=1),
    )
    courier = Courier.objects.create(
        user=user,
        phone=phone,
        code=f'CR-{phone[-4:]}',
    )
    raw = secrets.token_hex(32)
    session = Session.objects.create(
        user_id=user,
        ip_address='127.0.0.1',
        user_agent='',
        payload=SessionRepository.hash_token(raw),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return user, courier, session, raw


def test_profile_with_manager_role_is_neither_courier_nor_admin_audience():
    _user, _courier, _session, raw = _identity()
    request = RequestFactory().get(
        '/courier/me/', HTTP_AUTHORIZATION=f'Token {raw}',
    )

    assert resolve_courier(request) == (None, None, None)
    assert Client().get(
        '/api/admins/couriers/',
        HTTP_AUTHORIZATION=f'Bearer {raw}',
    ).status_code == 403


def test_courier_auth_rejects_conflicting_cookie_and_header_credentials():
    request = RequestFactory().get(
        '/courier/me/',
        HTTP_AUTHORIZATION='Token ' + ('b' * 64),
    )
    request.COOKIES['session_key'] = 'a' * 64

    response = courier_required(
        lambda _request: JsonResponse({'success': True}),
    )(request)

    assert response.status_code == 401
    assert json.loads(response.content)['code'] == 'session_credential_conflict'


def test_courier_websocket_rejects_split_browser_identity():
    first = 'a' * 64
    second = 'b' * 64
    assert _handshake_token({
        'query_string': f'token={first}'.encode(),
        'headers': [
            (b'authorization', f'Token {second}'.encode()),
            (b'cookie', f'session_key={first}'.encode()),
        ],
    }) is None
    assert _handshake_token({
        'query_string': f'token={first}'.encode(),
        'headers': [
            (b'authorization', f'tOkEn {first}'.encode()),
            (b'cookie', f'session_key={first}'.encode()),
        ],
    }) == first


def test_courier_revoke_returns_token_free_conflict_response():
    client = Client()
    first = 'a' * 64
    second = 'b' * 64
    client.cookies['session_key'] = first

    response = client.post(
        '/auth/courier/revoke/',
        data=json.dumps({}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Token {second}',
    )

    assert response.status_code == 401
    assert response.json()['code'] == 'session_credential_conflict'
    assert first not in response.content.decode()
    assert second not in response.content.decode()


def test_token_minting_rejects_non_courier_role():
    _user, courier, _session, _raw = _identity(phone='998901234568')

    with pytest.raises(CourierTokenAudienceError):
        issue_login_claim(courier)
    with pytest.raises(CourierTokenAudienceError):
        issue_session(courier, ip_address='127.0.0.1', user_agent='test')


def test_isolation_migration_publishes_role_and_invalidates_after_commit(
    django_capture_on_commit_callbacks,
):
    user, _courier, session, _raw = _identity(
        role='CASHIER', phone='998901234569',
    )
    old_version = user.sync_version
    key = f'session:{session.payload}'
    cache.set(key, {'still': 'cached'}, 300)
    migration = importlib.import_module(
        'couriers.migrations.0007_isolate_courier_identity',
    )
    editor = SimpleNamespace(connection=connection)

    # Runtime Session deletion has its own immediate signal. Historical models
    # used by MigrationExecutor do not; disconnect it here so this unit test
    # exercises the migration's explicit commit boundary.
    from base.signals import _invalidate_session_cache
    post_delete.disconnect(_invalidate_session_cache, sender=Session)
    try:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            migration.isolate_courier_users(apps, editor)
    finally:
        post_delete.connect(_invalidate_session_cache, sender=Session)

    user.refresh_from_db()
    assert user.role == User.RoleChoices.COURIER
    assert user.sync_version == old_version + 1
    assert user.synced_at is not None
    assert not Session.objects.filter(pk=session.pk).exists()
    assert cache.get(key) == {'still': 'cached'}
    assert len(callbacks) == 1

    callbacks[0]()
    assert cache.get(key) is None
