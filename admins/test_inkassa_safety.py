"""Inkassa idempotency, authorization, and reconciliation-first safeguards."""
import json
import secrets
from datetime import timedelta
import pytest
from django.test import Client, override_settings
from django.utils import timezone

from base.repositories.session import SessionRepository


pytestmark = pytest.mark.django_db


def _recognize(branch_id, tenders, actor, shift_id):
    from base.services.treasury_service import TreasuryService

    return TreasuryService.post_shift_settlement(
        shift_id,
        tenders,
        performed_by=actor,
        branch_id=branch_id,
    )


def _auth(user):
    from base.models import Session

    token = secrets.token_hex(32)
    Session.objects.create(
        user_id=user,
        ip_address='127.0.0.1',
        user_agent='',
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


def _make_global_admin(user):
    user.branch_id = 'cloud'
    user.save(update_fields=['branch_id'])


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_service_replay_is_exactly_once_and_payload_bound(admin_user):
    from admins.services.inkassa_service import AdminInkassaService
    from base.models import CashRegister, Inkassa, TreasuryAccount, TreasuryTransaction

    branch = 'branch-replay'
    _make_global_admin(admin_user)
    CashRegister.objects.create(branch_id=branch, current_balance='100.00')
    _recognize(
        branch,
        {'CASH': '100.00', 'PAYME': '50.00'},
        admin_user,
        shift_id=9501,
    )
    safe_before = TreasuryAccount.objects.get(kind='SAFE').balance
    body = {'cash': '100.00', 'payme': '50.00', 'notes': 'sealed handover'}

    first, status = AdminInkassaService.perform(
        admin_user, body, branch_id=branch, batch_key='service-replay-1',
    )
    assert status == 200, first
    assert first['data']['replayed'] is False
    assert Inkassa.objects.filter(collection_batch_key='service-replay-1').count() == 2

    replay, status = AdminInkassaService.perform(
        admin_user, body, branch_id=branch, batch_key='service-replay-1',
    )
    assert status == 200, replay
    assert replay['data']['replayed'] is True
    assert replay['data']['inkassas'] == first['data']['inkassas']
    assert Inkassa.objects.filter(collection_batch_key='service-replay-1').count() == 2
    assert TreasuryAccount.objects.get(kind='SAFE').balance == safe_before
    assert not TreasuryTransaction.objects.filter(
        type=TreasuryTransaction.Type.INKASSA,
        reference_type='InkassaLegacy',
    ).exists()

    conflict, status = AdminInkassaService.perform(
        admin_user,
        {**body, 'notes': 'changed after approval'},
        branch_id=branch,
        batch_key='service-replay-1',
    )
    assert status == 422, conflict
    assert 'batch_id' in conflict['errors']


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_http_replay_bypasses_no_money_and_writes_one_audit(admin_user):
    from base.models import AuditLog, CashRegister, Inkassa, TreasuryAccount

    branch = 'branch-http'
    _make_global_admin(admin_user)
    CashRegister.objects.create(branch_id=branch, current_balance='100.00')
    _recognize(branch, {'CASH': '100.00'}, admin_user, shift_id=9502)
    safe_before = TreasuryAccount.objects.get(kind='SAFE').balance
    auth = _auth(admin_user)
    request_body = {
        'branch_id': branch,
        'cash': '100.00',
        'notes': 'http retry proof',
    }
    client = Client()

    first = client.post(
        '/api/admins/inkassa/perform',
        data=json.dumps(request_body),
        content_type='application/json',
        HTTP_IDEMPOTENCY_KEY='http-replay-1',
        **auth,
    )
    assert first.status_code == 200, first.content
    assert first.json()['data']['replayed'] is False

    replay = client.post(
        '/api/admins/inkassa/perform',
        data=json.dumps(request_body),
        content_type='application/json',
        HTTP_IDEMPOTENCY_KEY='http-replay-1',
        **auth,
    )
    assert replay.status_code == 200, replay.content
    assert replay.json()['data']['replayed'] is True
    assert Inkassa.objects.filter(collection_batch_key='http-replay-1').count() == 1
    assert AuditLog.objects.filter(action=AuditLog.Action.INKASSA_PERFORM).count() == 1
    assert TreasuryAccount.objects.get(kind='SAFE').balance == safe_before

    conflict = client.post(
        '/api/admins/inkassa/perform',
        data=json.dumps({**request_body, 'cash': '99.00'}),
        content_type='application/json',
        HTTP_IDEMPOTENCY_KEY='http-replay-1',
        **auth,
    )
    assert conflict.status_code == 422, conflict.content


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_branch_admin_cannot_approve_legacy_opening(admin_user):
    from admins.services.inkassa_service import AdminInkassaService
    from base.models import CashRegister, Inkassa, TreasuryTransaction, User

    branch_admin = User.objects.create(
        first_name='Branch',
        last_name='Admin',
        email='branch-admin@test.local',
        password='unused',
        role=User.RoleChoices.ADMIN,
        status=User.UserStatus.ACTIVE,
        branch_id='branch-a',
    )
    CashRegister.objects.create(branch_id='branch-a', current_balance='100.00')

    result, status = AdminInkassaService.perform(
        branch_admin,
        {
            'cash': '50.00',
            'approve_legacy_opening': True,
            'legacy_opening_note': 'claimed opening',
        },
        branch_id='branch-a',
        batch_key='legacy-branch-admin',
    )
    assert status == 403, result
    assert not Inkassa.objects.exists()
    assert not TreasuryTransaction.objects.exists()


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_branch_manager_reads_only_own_inkassa(admin_user):
    from admins.services.inkassa_service import AdminInkassaService
    from base.models import CashRegister, Inkassa, User

    manager = User.objects.create(
        first_name='Branch',
        last_name='Manager',
        email='branch-manager@test.local',
        password='unused',
        role=User.RoleChoices.MANAGER,
        status=User.UserStatus.ACTIVE,
        branch_id='branch-a',
    )
    CashRegister.objects.create(branch_id='branch-a', current_balance='100.00')
    CashRegister.objects.create(branch_id='branch-b', current_balance='200.00')
    own = Inkassa.objects.create(
        cashier=manager,
        branch_id='branch-a',
        inkass_type='CASH',
        amount='10.00',
        balance_before='100.00',
        balance_after='90.00',
    )
    foreign = Inkassa.objects.create(
        cashier=admin_user,
        branch_id='branch-b',
        inkass_type='CASH',
        amount='20.00',
        balance_before='200.00',
        balance_after='180.00',
    )

    result, status = AdminInkassaService.get_balance(actor=manager)
    assert status == 200, result
    assert result['data']['branch_id'] == 'branch-a'
    result, status = AdminInkassaService.get_balance('branch-b', actor=manager)
    assert status == 403, result

    result, status = AdminInkassaService.get_history(actor=manager)
    assert status == 200, result
    assert [row['id'] for row in result['data']['inkassas']] == [own.id]
    result, status = AdminInkassaService.get_detail(foreign.id, actor=manager)
    assert status == 403, result
