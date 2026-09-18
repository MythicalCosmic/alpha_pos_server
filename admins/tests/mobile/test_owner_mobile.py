import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from admins.models import AdminDevice, OwnerPushOutbox
from admins.services import fcm, owner_push
from base.models import Session, Shift, User
from base.repositories import SessionRepository
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from hr.models import ExpenseCategory
from hr.services import ExpenseService

pytestmark = pytest.mark.django_db
BRANCH = 'branch1'
UA = 'AlphaPOS-Owner/1 (android)'
TASHKENT = ZoneInfo('Asia/Tashkent')
FCM = {'FCM_PROJECT_ID': 'alpha-test', 'FCM_SERVICE_ACCOUNT_FILE': '/nonexistent.json', 'OWNER_PUSH_ENABLED': True}


def _user(role, email, permissions=None):
    return User.objects.create(
        first_name=role.title(), last_name='Tester', email=email, password='!', role=role,
        status=User.UserStatus.ACTIVE,
        permissions=permissions if permissions is not None else DEFAULT_ROLE_PERMISSIONS[role],
        branch_id=BRANCH,
    )


def _login(user, ua=UA):
    token = secrets.token_hex(32)
    session = Session.objects.create(
        user_id=user, ip_address='127.0.0.1', user_agent=ua,
        payload=SessionRepository.hash_token(token), expires_at=timezone.now() + timedelta(days=7),
    )
    return token, session, Client(HTTP_AUTHORIZATION=f'Bearer {token}', HTTP_USER_AGENT=ua)


def _register(client, token='fcm-token-1', **extra):
    return client.post('/api/admins/devices', {'token': token, 'platform': 'android', 'locale': 'uz', **extra},
                       content_type='application/json')


# ------------------------------------------------------------------ refresh

def test_refresh_rotates_the_token_and_moves_the_phone_to_the_new_session():
    owner = _user('ADMIN', 'owner@test.local')
    old_token, session, client = _login(owner)
    assert _register(client).status_code == 200

    response = client.post('/api/admins/auth-refresh')

    assert response.status_code == 200, response.json()
    new_token = response.json()['data']['token']
    assert new_token != old_token
    assert client.get('/api/admins/auth-me').status_code == 401  # old token no longer works
    fresh = Client(HTTP_AUTHORIZATION=f'Bearer {new_token}', HTTP_USER_AGENT=UA)
    assert fresh.get('/api/admins/auth-me').status_code == 200
    device = AdminDevice.objects.get()
    assert device.session.payload == SessionRepository.hash_token(new_token)
    assert device.session.user_agent == UA


def test_refresh_keeps_the_user_agent_binding():
    owner = _user('ADMIN', 'owner-ua@test.local')
    token, _, _ = _login(owner)
    other_agent = Client(HTTP_AUTHORIZATION=f'Bearer {token}', HTTP_USER_AGENT='Mozilla/5.0 something else')

    assert other_agent.post('/api/admins/auth-refresh').status_code == 401


# ------------------------------------------------------------------ devices

def test_devices_register_update_and_unregister():
    owner = _user('ADMIN', 'owner-dev@test.local')
    _, session, client = _login(owner)

    first = _register(client, prefs={'shift_closed': False})
    assert first.status_code == 200
    device = first.json()['data']['device']
    assert device['prefs'] == {'expense_pending': True, 'shift_closed': False, 'daily_summary': True}

    bad = client.patch(f"/api/admins/devices/{device['id']}", {'prefs': {'nonsense': True}},
                       content_type='application/json')
    assert bad.status_code == 422
    ok = client.patch(f"/api/admins/devices/{device['id']}", {'prefs': {'daily_summary': False}, 'locale': 'ru'},
                      content_type='application/json')
    assert ok.json()['data']['device']['prefs']['daily_summary'] is False
    assert ok.json()['data']['device']['locale'] == 'ru'
    assert len(client.get('/api/admins/devices').json()['data']['devices']) == 1

    removed = client.delete('/api/admins/devices', {'token': 'fcm-token-1'}, content_type='application/json')
    assert removed.json()['data']['unregistered'] == 1
    assert AdminDevice.objects.get().is_active is False


def test_a_token_follows_the_latest_login_and_logout_removes_the_phone():
    first_owner = _user('ADMIN', 'first@test.local')
    second_owner = _user('ADMIN', 'second@test.local')
    _, _, first_client = _login(first_owner)
    _, _, second_client = _login(second_owner)
    _register(first_client)
    _register(second_client)

    device = AdminDevice.objects.get()
    assert device.user_id == second_owner.id

    assert second_client.post('/api/admins/auth-logout').status_code == 200
    assert not AdminDevice.objects.exists()


def test_devices_are_for_admins_only():
    _, _, manager_client = _login(_user('MANAGER', 'manager@test.local'))
    assert _register(manager_client).status_code == 403
    _, _, admin_client = _login(_user('ADMIN', 'admin-bad@test.local'))
    assert admin_client.post('/api/admins/devices', {'token': 'x', 'platform': 'windows'},
                             content_type='application/json').status_code == 422
    assert admin_client.post('/api/admins/devices', {'token': 'x', 'platform': 'ios', 'extra': 1},
                             content_type='application/json').status_code == 422


# ------------------------------------------------------------------ events

def _category():
    return ExpenseCategory.objects.create(code='SUPPLIES', name='Supplies', reporting_group='OPERATING',
                                          allowed_sources=['SAFE', 'BANK'], branch_id=BRANCH)


def _owner_with_phone(email='push-owner@test.local', **device):
    owner = _user('ADMIN', email)
    _, session, _ = _login(owner)
    AdminDevice.objects.create(user=owner, session=session, token=f'tok-{email}', platform='android', **device)
    return owner


@override_settings(**FCM)
def test_new_pending_expense_notifies_other_admins_once(django_capture_on_commit_callbacks):
    owner = _owner_with_phone()
    requester = _user('ADMIN', 'requester@test.local')
    _, session, _ = _login(requester)
    AdminDevice.objects.create(user=requester, session=session, token='tok-requester', platform='ios')
    category = _category()

    with django_capture_on_commit_callbacks(execute=True):
        body, status = ExpenseService.create(
            actor=requester, category_id=category.id, amount_uzs=1_250_000, requested_source='SAFE',
            expense_date=timezone.localdate(), description='Gas cylinders',
        )
    assert status == 201, body

    rows = list(OwnerPushOutbox.objects.all())
    assert len(rows) == 1
    row = rows[0]
    assert row.device.user_id == owner.id  # the requester is not notified about their own request
    assert row.title == 'Tasdiqlash kerak'
    assert row.body == 'Yangi xarajat: 1,250,000 so‘m — Supplies. Gas cylinders'
    assert row.data == {'kind': 'expense_pending', 'route': f"/approvals/{body['data']['expense_id']}",
                        'event': f"expense:{body['data']['expense_id']}"}

    with django_capture_on_commit_callbacks(execute=True):
        owner_push.on_expense_pending(body['data']['expense_id'])
    assert OwnerPushOutbox.objects.count() == 1


@override_settings(**FCM)
def test_backfilled_expenses_and_muted_phones_are_not_notified(django_capture_on_commit_callbacks):
    _owner_with_phone()
    _owner_with_phone('muted@test.local', prefs={'expense_pending': False})
    requester = _user('ADMIN', 'import@test.local')
    category = _category()

    with django_capture_on_commit_callbacks(execute=True):
        ExpenseService.create(actor=requester, category_id=category.id, amount_uzs=10_000,
                              requested_source='SAFE', expense_date=timezone.localdate() - timedelta(days=30))
    assert not OwnerPushOutbox.objects.exists()

    with django_capture_on_commit_callbacks(execute=True):
        ExpenseService.create(actor=requester, category_id=category.id, amount_uzs=10_000,
                              requested_source='SAFE', expense_date=timezone.localdate())
    assert OwnerPushOutbox.objects.count() == 1  # only the phone that wants expense alerts


@override_settings(**FCM)
def test_expired_sessions_and_disabled_push_send_nothing(django_capture_on_commit_callbacks):
    owner = _owner_with_phone()
    Session.objects.filter(user_id=owner).update(expires_at=timezone.now() - timedelta(minutes=1))
    category = _category()
    requester = _user('ADMIN', 'req2@test.local')
    with django_capture_on_commit_callbacks(execute=True):
        ExpenseService.create(actor=requester, category_id=category.id, amount_uzs=10_000,
                              requested_source='SAFE', expense_date=timezone.localdate())
    assert not OwnerPushOutbox.objects.exists()

    Session.objects.filter(user_id=owner).update(expires_at=timezone.now() + timedelta(days=1))
    with override_settings(OWNER_PUSH_ENABLED=False), django_capture_on_commit_callbacks(execute=True):
        ExpenseService.create(actor=requester, category_id=category.id, amount_uzs=20_000,
                              requested_source='SAFE', expense_date=timezone.localdate())
    assert not OwnerPushOutbox.objects.exists()


@override_settings(**FCM)
def test_closed_shift_notifies_once(django_capture_on_commit_callbacks):
    _owner_with_phone()
    cashier = _user('CASHIER', 'cashier@test.local')
    now = timezone.now()
    shift = Shift.objects.create(user=cashier, start_time=now - timedelta(hours=8), branch_id=BRANCH,
                                 total_orders=42, total_revenue=Decimal('3450000'))
    assert not OwnerPushOutbox.objects.exists()

    with django_capture_on_commit_callbacks(execute=True):
        shift.status = Shift.Status.ENDED
        shift.end_time = now
        shift.save()
    with django_capture_on_commit_callbacks(execute=True):
        shift.save()  # later saves (sync, notes) do not notify again

    row = OwnerPushOutbox.objects.get()
    assert row.body == 'Cashier Tester: savdo 3,450,000 so‘m, 42 ta buyurtma.'


@override_settings(**FCM)
def test_daily_summary_is_due_after_the_configured_time_and_sent_once():
    _owner_with_phone()
    morning = datetime(2026, 9, 18, 8, 59, tzinfo=TASHKENT)
    later = datetime(2026, 9, 18, 9, 1, tzinfo=TASHKENT)
    assert owner_push.daily_summary_due(morning) is None
    assert owner_push.daily_summary_due(later) == date(2026, 9, 17)

    assert owner_push.send_daily_summary(date(2026, 9, 17)) == 1
    assert owner_push.send_daily_summary(date(2026, 9, 17)) == 0
    assert owner_push.daily_summary_sent(date(2026, 9, 17))
    row = OwnerPushOutbox.objects.get()
    assert row.title == '17.09 kun yakuni'


# ------------------------------------------------------------------ delivery

def _queued(**extra):
    owner = _owner_with_phone(f'deliver-{secrets.token_hex(3)}@test.local')
    return OwnerPushOutbox.objects.create(
        device=owner.admin_devices.get(), event_key='expense:1', kind='expense_pending',
        title='t', body='b', data={'route': '/'}, next_attempt_at=timezone.now(), **extra,
    )


def test_delivery_marks_sent_retries_and_gives_up():
    sent_row = _queued()
    with mock.patch.object(fcm, 'send', return_value=fcm.SendResult(ok=True)) as send:
        assert owner_push.deliver_batch() == (1, 0)
    send.assert_called_once_with(sent_row.device.token, 't', 'b', {'route': '/'})
    sent_row.refresh_from_db()
    assert sent_row.status == 'SENT' and sent_row.sent_at

    retry_row = _queued()
    with mock.patch.object(fcm, 'send', return_value=fcm.SendResult(ok=False, retry=True, error='503')):
        assert owner_push.deliver_batch() == (0, 1)
    retry_row.refresh_from_db()
    assert retry_row.status == 'PENDING' and retry_row.attempts == 1
    assert retry_row.next_attempt_at > timezone.now()

    retry_row.attempts = owner_push.MAX_ATTEMPTS - 1
    retry_row.next_attempt_at = timezone.now()
    retry_row.save()
    with mock.patch.object(fcm, 'send', return_value=fcm.SendResult(ok=False, retry=True, error='503')):
        owner_push.deliver_batch()
    retry_row.refresh_from_db()
    assert retry_row.status == 'DEAD'


def test_uninstalled_app_token_deactivates_the_phone():
    row = _queued()
    with mock.patch.object(fcm, 'send', return_value=fcm.SendResult(ok=False, drop_device=True, error='UNREGISTERED')):
        owner_push.deliver_batch()
    row.refresh_from_db()
    assert row.status == 'DEAD'
    assert row.device.is_active is False


@override_settings(**FCM)
def test_fcm_request_shape_and_error_classification():
    ok = mock.Mock(status_code=200)
    with mock.patch.object(fcm, '_access_token', return_value='oauth'), \
            mock.patch.object(fcm.requests, 'post', return_value=ok) as post:
        assert fcm.send('device-token', 'Title', 'Body', {'route': '/approvals/5', 'n': 1}).ok
    url, kwargs = post.call_args.args[0], post.call_args.kwargs
    assert url == 'https://fcm.googleapis.com/v1/projects/alpha-test/messages:send'
    assert kwargs['headers']['Authorization'] == 'Bearer oauth'
    message = kwargs['json']['message']
    assert message['token'] == 'device-token'
    assert message['data'] == {'route': '/approvals/5', 'n': '1'}

    gone = mock.Mock(status_code=404)
    gone.json.return_value = {'error': {'status': 'NOT_FOUND', 'details': [{'errorCode': 'UNREGISTERED'}]}}
    busy = mock.Mock(status_code=503)
    busy.json.return_value = {'error': {'status': 'UNAVAILABLE'}}
    assert fcm._classify(gone).drop_device
    assert fcm._classify(busy).retry and not fcm._classify(busy).drop_device

    with override_settings(FCM_PROJECT_ID=''):
        assert fcm.send('t', 'a', 'b').retry


# ------------------------------------------------------------------ reviewer

def test_review_account_can_read_but_not_change_anything():
    reviewer = _user('ADMIN', 'reviewer@test.local', permissions=['*', 'app.review_readonly'])
    _, _, client = _login(reviewer)

    assert client.get('/api/admins/auth-me').status_code == 200
    blocked = client.post('/api/admins/expenses/1/approve')
    assert blocked.status_code == 403
    assert blocked.json()['code'] == 'REVIEW_READ_ONLY'
    assert client.post('/api/admins/hr/salaries/1/pay/', {}, content_type='application/json').status_code == 403
    assert _register(client).status_code == 200  # push registration is allowed
    assert client.post('/api/admins/auth-refresh').status_code == 200

    _, _, admin = _login(_user('ADMIN', 'real-admin@test.local'))
    assert admin.post('/api/admins/expenses/999999/approve').status_code != 403
