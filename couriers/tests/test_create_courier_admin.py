"""Courier provisioning and mobile-auth security contract tests."""
import json
import secrets
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

pytestmark = pytest.mark.django_db

ROOT = '/api/admins/couriers/'
CREATE = '/api/admins/couriers/create'


def _staff_token():
    from base.models import User, Session
    from base.repositories.session import SessionRepository
    u = User.objects.create(email='mgr@x.local', first_name='M', last_name='gr',
                            role='MANAGER', status='ACTIVE', password='!')
    tok = secrets.token_hex(32)
    Session.objects.create(user_id=u, ip_address='127.0.0.1',
                           payload=SessionRepository.hash_token(tok),
                           expires_at=timezone.now() + timedelta(hours=1))
    return tok


def _post(path, body, tok=None):
    c = Client()
    kw = {'HTTP_AUTHORIZATION': f'Bearer {tok}'} if tok else {}
    return c.post(path, data=json.dumps(body), content_type='application/json', **kw)


def test_post_root_creates_courier_with_supplied_password():
    from base.models import Session
    from base.repositories.session import SessionRepository
    from couriers.models import Courier, CourierLoginClaim, CourierRefreshToken
    from couriers.tokens import QR_CLAIM_PREFIX, _digest

    tok = _staff_token()
    r = _post(ROOT, {'first_name': 'Ali', 'last_name': 'Valiyev',
                     'phone': '+998901112233', 'password': 'kuryer123'}, tok)
    assert r.status_code == 200, r.content
    d = r.json()['data']
    # the flat shape the FE asked for
    assert d['phone'] == '+998901112233'
    assert d['code'].startswith('CR-')
    assert isinstance(d['id'], int)
    assert 'password' not in d
    assert d['qr']['v'] == 2
    assert d['qr']['token'].startswith(QR_CLAIM_PREFIX)
    assert d['qr']['expires_at'] == d['expires_at']
    assert '+998901112233' not in d['qr']['token']
    assert 'kuryer123' not in json.dumps(d)
    assert Courier.objects.filter(phone='+998901112233').exists()
    claim = CourierLoginClaim.objects.get()
    assert claim.token_digest == _digest(d['qr']['token'])
    assert d['qr']['token'] not in claim.token_digest

    # the rider scans the QR -> the app logs in with {qr: token}
    login = _post('/auth/courier/login/', {'qr': d['qr']['token']})
    assert login.status_code == 200, login.content
    auth = login.json()
    assert auth['token']
    assert auth['token_type'] == 'Token'
    assert auth['expires_at']
    assert auth['refresh_token']
    assert auth['refresh_expires_at']
    refresh_row = CourierRefreshToken.objects.get()
    assert refresh_row.token_digest == _digest(auth['refresh_token'])
    assert auth['refresh_token'] not in refresh_row.token_digest
    assert not Session.objects.filter(payload=auth['token']).exists()
    assert Session.objects.filter(
        payload=SessionRepository.hash_token(auth['token']),
    ).exists()

    # QR claims are one-time credentials, never reusable passwords.
    replay = _post('/auth/courier/login/', {'qr': d['qr']['token']})
    assert replay.status_code == 401
    assert replay.json()['message'] == 'Invalid or expired login QR'


def test_create_path_returns_claim_without_generated_password():
    tok = _staff_token()
    r = _post(CREATE, {'first_name': 'Bek', 'phone': '+998905550000'}, tok)
    assert r.status_code == 200, r.content
    d = r.json()['data']
    assert 'password' not in d
    assert ':' not in d['qr']['token']
    login = _post('/auth/courier/login/', {'qr': d['qr']['token']})
    assert login.status_code == 200


def test_requires_staff_auth():
    r = _post(ROOT, {'phone': '+998900000001'})
    assert r.status_code in (401, 403)


def test_duplicate_phone_409():
    tok = _staff_token()
    body = {'phone': '+998905556677'}
    assert _post(CREATE, body, tok).status_code == 200
    assert _post(CREATE, body, tok).status_code == 409


def test_short_password_rejected():
    tok = _staff_token()
    r = _post(CREATE, {'phone': '+998901234567', 'password': 'ab'}, tok)
    assert r.status_code == 400


def test_get_root_still_lists():
    tok = _staff_token()
    _post(CREATE, {'phone': '+998907778899'}, tok)
    c = Client()
    r = c.get(ROOT, HTTP_AUTHORIZATION=f'Bearer {tok}')
    assert r.status_code == 200, r.content


def test_regenerate_rotates_claim_and_invalidates_previous_claim():
    tok = _staff_token()
    r = _post(CREATE, {'phone': '+998909990000', 'password': 'first123'}, tok)
    pk = r.json()['data']['id']
    old_claim = r.json()['data']['qr']['token']
    r2 = _post(f'/api/admins/couriers/{pk}/regenerate', {}, tok)
    assert r2.status_code == 200, r2.content
    new_claim = r2.json()['data']['qr']['token']
    assert new_claim != old_claim
    assert 'password' not in r2.json()['data']
    assert _post('/auth/courier/login/', {'qr': old_claim}).status_code == 401
    assert _post('/auth/courier/login/', {'qr': new_claim}).status_code == 200

    # QR-only rotation does not silently change the deliberate manual fallback.
    manual = _post('/auth/courier/login/', {
        'phone': '+998909990000', 'password': 'first123',
    })
    assert manual.status_code == 200


def test_expired_qr_claim_is_rejected():
    from couriers.models import CourierLoginClaim

    tok = _staff_token()
    created = _post(CREATE, {'phone': '+998901010101'}, tok).json()['data']
    CourierLoginClaim.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    response = _post('/auth/courier/login/', {'qr': created['qr']['token']})
    assert response.status_code == 401
    assert response.json()['message'] == 'Invalid or expired login QR'


def test_password_login_returns_explicit_access_and_refresh_expiry():
    tok = _staff_token()
    _post(CREATE, {
        'phone': '+998902020202', 'password': 'manual-secret',
    }, tok)
    response = _post('/auth/courier/login/', {
        'phone': '+998902020202', 'password': 'manual-secret',
    })
    assert response.status_code == 200
    body = response.json()
    assert set(('token', 'expires_at', 'refresh_token',
                'refresh_expires_at')).issubset(body)
    assert 'manual-secret' not in json.dumps(body)


def test_refresh_rotates_access_and_replay_revokes_replacement_family():
    from base.models import Session
    from base.repositories.session import SessionRepository
    from couriers.models import CourierRefreshToken

    tok = _staff_token()
    created = _post(CREATE, {'phone': '+998903030303'}, tok).json()['data']
    auth = _post('/auth/courier/login/', {'qr': created['qr']['token']}).json()

    refreshed = _post('/auth/courier/refresh/', {
        'refresh_token': auth['refresh_token'],
    })
    assert refreshed.status_code == 200, refreshed.content
    replacement = refreshed.json()
    assert replacement['token'] != auth['token']
    assert replacement['refresh_token'] != auth['refresh_token']
    assert not Session.objects.filter(
        payload=SessionRepository.hash_token(auth['token']),
    ).exists()
    assert Client().get(
        '/courier/me/', HTTP_AUTHORIZATION=f"Token {replacement['token']}",
    ).status_code == 200

    # Replaying the already-rotated credential is treated as theft: the whole
    # family, including the replacement access token, is revoked.
    replay = _post('/auth/courier/refresh/', {
        'refresh_token': auth['refresh_token'],
    })
    assert replay.status_code == 401
    assert Client().get(
        '/courier/me/', HTTP_AUTHORIZATION=f"Token {replacement['token']}",
    ).status_code == 401
    assert CourierRefreshToken.objects.filter(
        revoked_at__isnull=True,
    ).count() == 0


def test_expired_refresh_is_rejected_without_issuing_session():
    from couriers.models import CourierRefreshToken

    tok = _staff_token()
    created = _post(CREATE, {'phone': '+998904040404'}, tok).json()['data']
    auth = _post('/auth/courier/login/', {'qr': created['qr']['token']}).json()
    CourierRefreshToken.objects.update(
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    response = _post('/auth/courier/refresh/', {
        'refresh_token': auth['refresh_token'],
    })
    assert response.status_code == 401


def test_revoke_by_refresh_token_kills_family_and_is_idempotent():
    tok = _staff_token()
    created = _post(CREATE, {'phone': '+998905050505'}, tok).json()['data']
    auth = _post('/auth/courier/login/', {'qr': created['qr']['token']}).json()

    revoked = _post('/auth/courier/revoke/', {
        'refresh_token': auth['refresh_token'],
    })
    assert revoked.status_code == 200 and revoked.json() == {'ok': True}
    assert Client().get(
        '/courier/me/', HTTP_AUTHORIZATION=f"Token {auth['token']}",
    ).status_code == 401
    assert _post('/auth/courier/refresh/', {
        'refresh_token': auth['refresh_token'],
    }).status_code == 401
    assert _post('/auth/courier/revoke/', {
        'refresh_token': auth['refresh_token'],
    }).status_code == 200


def test_logout_also_revokes_refresh_family():
    tok = _staff_token()
    created = _post(CREATE, {'phone': '+998906060606'}, tok).json()['data']
    auth = _post('/auth/courier/login/', {'qr': created['qr']['token']}).json()
    logout = _post('/auth/courier/logout/', {}, auth['token'])
    assert logout.status_code == 200
    assert _post('/auth/courier/refresh/', {
        'refresh_token': auth['refresh_token'],
    }).status_code == 401


def test_password_reset_revokes_sessions_without_returning_password():
    tok = _staff_token()
    created = _post(CREATE, {
        'phone': '+998907070707', 'password': 'first-secret',
    }, tok).json()['data']
    auth = _post('/auth/courier/login/', {
        'phone': '+998907070707', 'password': 'first-secret',
    }).json()
    response = _post(
        f"/api/admins/couriers/{created['id']}/regenerate",
        {'password': 'second-secret'}, tok,
    )
    assert response.status_code == 200
    assert 'second-secret' not in json.dumps(response.json())
    assert Client().get(
        '/courier/me/', HTTP_AUTHORIZATION=f"Token {auth['token']}",
    ).status_code == 401
    assert _post('/auth/courier/refresh/', {
        'refresh_token': auth['refresh_token'],
    }).status_code == 401
    assert _post('/auth/courier/login/', {
        'phone': '+998907070707', 'password': 'first-secret',
    }).status_code == 401
    assert _post('/auth/courier/login/', {
        'phone': '+998907070707', 'password': 'second-secret',
    }).status_code == 200
