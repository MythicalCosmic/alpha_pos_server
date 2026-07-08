"""Cloud admin: provision a courier + login credential (POST /api/admins/couriers).

Mirrors the till's /api/couriers/create so the desktop "Kuryer QR" page and the
admin panel behave identically. The manager may supply a password; otherwise one is
generated. The returned QR token is exactly what /auth/courier/login decodes.
"""
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
    from couriers.models import Courier
    tok = _staff_token()
    r = _post(ROOT, {'first_name': 'Ali', 'last_name': 'Valiyev',
                     'phone': '+998901112233', 'password': 'kuryer123'}, tok)
    assert r.status_code == 200, r.content
    d = r.json()['data']
    # the flat shape the FE asked for
    assert d['phone'] == '+998901112233'
    assert d['code'].startswith('CR-')
    assert isinstance(d['id'], int)
    assert d['password'] == 'kuryer123'          # manager-supplied, echoed once
    assert d['qr']['token'] == '+998901112233:kuryer123'
    assert Courier.objects.filter(phone='+998901112233').exists()

    # the rider scans the QR -> the app logs in with {qr: token}
    login = _post('/auth/courier/login/', {'qr': d['qr']['token']})
    assert login.status_code == 200, login.content
    assert login.json().get('token')


def test_create_path_generates_password_when_omitted():
    tok = _staff_token()
    r = _post(CREATE, {'first_name': 'Bek', 'phone': '+998905550000'}, tok)
    assert r.status_code == 200, r.content
    d = r.json()['data']
    assert d['password'] and len(d['password']) >= 6
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


def test_regenerate_rotates_password():
    tok = _staff_token()
    r = _post(CREATE, {'phone': '+998909990000', 'password': 'first123'}, tok)
    pk = r.json()['data']['id']
    r2 = _post(f'/api/admins/couriers/{pk}/regenerate', {}, tok)
    assert r2.status_code == 200, r2.content
    new_pw = r2.json()['data']['password']
    assert new_pw != 'first123'
    assert _post('/auth/courier/login/', {'qr': '+998909990000:first123'}).status_code == 401
    assert _post('/auth/courier/login/', {'qr': f'+998909990000:{new_pw}'}).status_code == 200
