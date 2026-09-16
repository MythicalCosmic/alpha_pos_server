import secrets
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from base.models import Session, User
from base.repositories import SessionRepository
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from hr.models import ExpenseCategory, ExpenseSupplierLink
from hr.services import ExpenseService
from stock.models import Supplier


pytestmark = pytest.mark.django_db
BRANCH = 'branch1'


def _user(role, email):
    return User.objects.create(
        first_name=role.title(), last_name='Tester', email=email, password='!',
        role=role, status=User.UserStatus.ACTIVE,
        permissions=DEFAULT_ROLE_PERMISSIONS[role], branch_id=BRANCH,
    )


def _client(user):
    token = secrets.token_hex(32)
    agent = f'supplier-links-{user.id}'
    Session.objects.create(
        user_id=user, ip_address='127.0.0.1', user_agent=agent,
        payload=SessionRepository.hash_token(token), expires_at=timezone.now() + timedelta(hours=1),
    )
    return Client(HTTP_AUTHORIZATION=f'Bearer {token}', HTTP_USER_AGENT=agent)


@pytest.fixture
def setup():
    admin = _user('ADMIN', 'links-admin@test.local')
    meat = ExpenseCategory.objects.create(code='MEAT', name='Meat', reporting_group='INVENTORY_PURCHASE', branch_id=BRANCH)
    rent = ExpenseCategory.objects.create(code='RENT', name='Rent', reporting_group='RENT', branch_id=BRANCH)
    ids = []
    for category, amount in ((meat, 4_000_000), (rent, 15_000_000)):
        body, status = ExpenseService.create(
            actor=admin, category_id=category.id, amount_uzs=amount,
            expense_date='2026-08-05', requested_source='SAFE', description=category.name,
        )
        assert status == 201, body
        ids.append(body['data']['expense_id'])
    supplier = Supplier.objects.create(name="Donar go'sht", branch_id=BRANCH)
    return {'admin': admin, 'meat_id': ids[0], 'rent_id': ids[1], 'supplier': supplier}


def test_link_endpoint_moves_purchases_to_the_supplier_page(setup):
    client = _client(setup['admin'])

    linked = client.post(
        '/api/admins/hr/expenses/supplier-links/',
        {'expense_ids': [setup['meat_id']], 'supplier_id': setup['supplier'].id},
        content_type='application/json',
    )
    assert linked.status_code == 200, linked.json()
    assert ExpenseSupplierLink.objects.filter(expense_id=setup['meat_id']).exists()

    rejected = client.post(
        '/api/admins/hr/expenses/supplier-links/',
        {'expense_ids': [setup['rent_id']], 'supplier_id': setup['supplier'].id},
        content_type='application/json',
    )
    assert rejected.status_code == 422

    listed = client.get('/api/admins/expenses', {'supplier_purchases': 'exclude'}).json()['data']
    assert [row['id'] for row in listed['expenses']] == [setup['rent_id']]
    assert listed['totals']['amount_uzs'] == 15_000_000

    only = client.get('/api/admins/expenses', {'supplier_id': setup['supplier'].id}).json()['data']
    assert [row['supplier']['name'] for row in only['expenses']] == ["Donar go'sht"]

    purchases = client.get(f"/api/admins/stock/suppliers/{setup['supplier'].id}/purchases/").json()['data']
    assert purchases['totals']['amount_uzs'] == 4_000_000
    assert purchases['purchases'][0]['expense_id'] == setup['meat_id']

    assert client.get(f"/api/admins/stock/suppliers/{setup['supplier'].id}/purchases/", {'date_from': 'x'}).status_code == 422


def test_link_endpoint_requires_expense_management(setup):
    cashier = _client(_user('CASHIER', 'links-cashier@test.local'))
    response = cashier.post(
        '/api/admins/hr/expenses/supplier-links/',
        {'expense_ids': [setup['meat_id']], 'supplier_id': setup['supplier'].id},
        content_type='application/json',
    )
    assert response.status_code in (401, 403)
    assert not ExpenseSupplierLink.objects.exists()
