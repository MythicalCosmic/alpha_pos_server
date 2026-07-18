"""Admin shift rows and global KPIs must share the actor's branch scope."""
import secrets
from datetime import timedelta

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from base.repositories.session import SessionRepository


pytestmark = pytest.mark.django_db


def _user(email, role, branch):
    from base.models import User

    return User.objects.create(
        first_name=role.title(),
        last_name=branch,
        email=email,
        password='unused',
        role=role,
        status=User.UserStatus.ACTIVE,
        branch_id=branch,
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


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_shift_list_summary_and_active_endpoint_are_branch_scoped(admin_user):
    from admins.services.shift_service import ShiftService
    from base.models import Shift, User

    manager_a = _user('manager-a@test.local', User.RoleChoices.MANAGER, 'branch-a')
    cashier_a = _user('cashier-a@test.local', User.RoleChoices.CASHIER, 'branch-a')
    cashier_b = _user('cashier-b@test.local', User.RoleChoices.CASHIER, 'branch-b')
    shift_a = Shift.objects.create(
        user=cashier_a,
        branch_id='branch-a',
        status=Shift.Status.ACTIVE,
        start_time=timezone.now() - timedelta(hours=1),
    )
    shift_b = Shift.objects.create(
        user=cashier_b,
        branch_id='branch-b',
        status=Shift.Status.ACTIVE,
        start_time=timezone.now() - timedelta(hours=1),
    )

    result, status = ShiftService.list(per_page=50, actor=manager_a)
    assert status == 200, result
    assert [row['id'] for row in result['data']['shifts']] == [shift_a.id]
    assert result['data']['pagination']['total'] == 1
    assert result['data']['summary']['shift_count'] == 1
    active, active_status = ShiftService.get_active_shifts(actor=manager_a)
    assert active_status == 200, active
    assert [row['id'] for row in active['data']] == [shift_a.id]

    auth = _auth(manager_a)
    client = Client()
    listed = client.get('/api/admins/shifts', **auth)
    assert listed.status_code == 200, listed.content
    assert [row['id'] for row in listed.json()['data']['shifts']] == [shift_a.id]
    assert listed.json()['data']['summary']['shift_count'] == 1
    active_http = client.get('/api/admins/shifts/active', **auth)
    assert active_http.status_code == 200, active_http.content
    assert [row['id'] for row in active_http.json()['data']] == [shift_a.id]

    admin_user.branch_id = 'cloud'
    admin_user.save(update_fields=['branch_id'])
    global_result, global_status = ShiftService.list(
        per_page=50, actor=admin_user,
    )
    assert global_status == 200, global_result
    assert {row['id'] for row in global_result['data']['shifts']} == {
        shift_a.id, shift_b.id,
    }
    assert global_result['data']['summary']['shift_count'] == 2
