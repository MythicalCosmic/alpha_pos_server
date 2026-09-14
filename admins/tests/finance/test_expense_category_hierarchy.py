import json
import secrets
from datetime import date, timedelta

import pytest
from django.test import Client
from django.utils import timezone

from base.models import AuditLog, IdempotencyKey, Session, User
from base.repositories import SessionRepository
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from base.services.sync.config import FK_UUID_MAPPINGS
from hr.models import Expense, ExpenseCategory, ExpenseTransition
from hr.services import ExpenseCategoryService, ExpenseService


pytestmark = pytest.mark.django_db
BRANCH = 'branch1'


def _manager():
    return User.objects.create(
        first_name='Expense',
        last_name='Manager',
        email='expense-hierarchy-manager@test.local',
        password='!',
        role=User.RoleChoices.MANAGER,
        status=User.UserStatus.ACTIVE,
        permissions=DEFAULT_ROLE_PERMISSIONS['MANAGER'],
        branch_id=BRANCH,
    )


def _client(user):
    token = secrets.token_hex(32)
    user_agent = f'expense-hierarchy-{user.id}'
    Session.objects.create(
        user_id=user,
        ip_address='127.0.0.1',
        user_agent=user_agent,
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return Client(
        HTTP_AUTHORIZATION=f'Bearer {token}',
        HTTP_USER_AGENT=user_agent,
    )


def _category(code, name, **values):
    return ExpenseCategory.objects.create(
        code=code,
        name=name,
        branch_id=BRANCH,
        **values,
    )


def test_category_tree_is_bounded_and_group_nodes_cannot_receive_new_expenses(
    django_assert_num_queries,
):
    actor = _manager()
    root = _category(
        'FACILITIES',
        'Facilities',
        cost_behavior=ExpenseCategory.CostBehavior.MIXED,
        reporting_group='OPERATING',
    )
    child = _category(
        'FACILITIES_ELECTRICITY',
        'Electricity',
        parent=root,
        cost_behavior=ExpenseCategory.CostBehavior.VARIABLE,
        reporting_group='UTILITIES',
    )

    with django_assert_num_queries(2):
        body, status = ExpenseCategoryService.list(include_inactive=True)

    assert status == 200
    rows = body['data']['categories']
    assert [row['id'] for row in rows] == [root.id, child.id]
    assert rows[0]['is_selectable'] is False
    assert rows[0]['active_child_count'] == 1
    assert rows[1]['path'] == ['Facilities', 'Electricity']
    assert rows[1]['cost_behavior'] == 'VARIABLE'
    assert rows[1]['is_selectable'] is True

    rejected, rejected_status = ExpenseCategoryService.create(
        name='Meter charge',
        code='METER_CHARGE',
        parent_id=child.id,
        actor=actor,
    )
    assert rejected_status == 422
    assert rejected['code'] == 'EXPENSE_CATEGORY_DEPTH_EXCEEDED'

    expense_result, expense_status = ExpenseService.create(
        actor=actor,
        category_id=root.id,
        amount_uzs=100_000,
        expense_date='2026-09-14',
        requested_source='SAFE',
    )
    assert expense_status == 422
    assert expense_result['code'] == 'EXPENSE_CATEGORY_GROUP_ONLY'


def test_new_expense_keeps_immutable_category_path_and_classification_snapshots():
    actor = _manager()
    root = _category('FACILITIES', 'Facilities')
    child = _category(
        'ELECTRICITY',
        'Electricity',
        parent=root,
        cost_behavior=ExpenseCategory.CostBehavior.VARIABLE,
        reporting_group='UTILITIES',
    )

    created, status = ExpenseService.create(
        actor=actor,
        category_id=child.id,
        amount_uzs=250_000,
        expense_date='2026-09-14',
        requested_source='SAFE',
        description='September electricity',
    )
    assert status == 201
    expense = Expense.objects.get(pk=created['data']['expense_id'])
    child.name = 'Power'
    child.cost_behavior = ExpenseCategory.CostBehavior.FIXED
    child.reporting_group = 'OPERATING'
    child.save(update_fields=[
        'name', 'cost_behavior', 'reporting_group', 'updated_at',
    ])

    expense = ExpenseService._queryset().get(pk=expense.pk)
    serialized = ExpenseService.serialize(expense)
    assert serialized['category']['path'] == ['Facilities', 'Electricity']
    assert serialized['category']['cost_behavior'] == 'VARIABLE'
    assert serialized['category']['reporting_group'] == 'UTILITIES'
    assert expense.category_name_snapshot == 'Electricity'


def test_expense_filters_use_hierarchy_and_immutable_classification():
    actor = _manager()
    root = _category('FACILITIES', 'Facilities')
    child = _category(
        'RENT',
        'Rent',
        parent=root,
        cost_behavior=ExpenseCategory.CostBehavior.FIXED,
        reporting_group='OPERATING',
    )
    other = _category(
        'SUPPLIES',
        'Supplies',
        cost_behavior=ExpenseCategory.CostBehavior.VARIABLE,
        reporting_group='OPERATING',
    )
    for category, source in ((child, 'SAFE'), (other, 'BANK')):
        created, status = ExpenseService.create(
            actor=actor,
            category_id=category.id,
            amount_uzs=100_000,
            expense_date='2026-09-14',
            requested_source=source,
        )
        assert status == 201, created

    by_tree, tree_status = ExpenseService.list(
        actor=actor,
        view_all=True,
        category_id=root.id,
        include_subcategories=True,
    )
    child.cost_behavior = ExpenseCategory.CostBehavior.VARIABLE
    child.reporting_group = 'UTILITIES'
    child.save(update_fields=[
        'cost_behavior',
        'reporting_group',
        'updated_at',
    ])
    by_behavior, behavior_status = ExpenseService.list(
        actor=actor,
        view_all=True,
        cost_behavior='FIXED',
        reporting_group='OPERATING',
        source_account='SAFE',
    )

    assert tree_status == 200
    assert [row['category_id'] for row in by_tree['data']['expenses']] == [child.id]
    assert behavior_status == 200
    assert [row['category_id'] for row in by_behavior['data']['expenses']] == [
        child.id,
    ]


def test_category_counts_are_scoped_to_the_actor_branch():
    actor = _manager()
    category = _category('OFFICE', 'Office')
    for branch_id in (BRANCH, BRANCH, 'branch2'):
        Expense.objects.create(
            category=category,
            category_code_snapshot=category.code,
            category_name_snapshot=category.name,
            amount=50_000,
            expense_date=date(2026, 9, 14),
            status=Expense.Status.PENDING,
            requested_source='SAFE',
            created_by=actor,
            branch_id=branch_id,
        )

    scoped, scoped_status = ExpenseCategoryService.list(
        include_inactive=True,
        actor=actor,
    )
    other_branch, other_status = ExpenseCategoryService.list(
        include_inactive=True,
        branch_id='branch2',
    )

    assert scoped_status == other_status == 200
    assert scoped['data']['categories'][0]['direct_expense_count'] == 2
    assert other_branch['data']['categories'][0]['direct_expense_count'] == 1


def test_expense_category_sync_uses_a_dedicated_parent_uuid_key():
    root = _category('FACILITIES', 'Facilities')
    child = _category('RENT', 'Rent', parent=root)

    payload = child.to_sync_dict()

    assert payload['expense_parent_uuid'] == str(root.uuid)
    assert FK_UUID_MAPPINGS['expense_parent_uuid'] == (
        'hr',
        'ExpenseCategory',
        'parent',
    )
    assert FK_UUID_MAPPINGS['parent_uuid'] == (
        'stock',
        'StockCategory',
        'parent',
    )


def test_compatibility_category_route_accepts_hierarchy_fields():
    actor = _manager()
    client = _client(actor)
    root = _category('FACILITIES', 'Facilities')

    response = client.post(
        '/api/admins/hr/expense-categories/',
        json.dumps({
            'name': 'Rent',
            'code': 'FACILITIES_RENT',
            'parent_id': root.id,
            'cost_behavior': 'FIXED',
            'reporting_group': 'OPERATING',
            'allowed_sources': ['SAFE', 'BANK'],
        }),
        content_type='application/json',
    )

    assert response.status_code == 201, response.content
    category = response.json()['data']['category']
    assert category['path'] == ['Facilities', 'Rent']
    assert category['cost_behavior'] == 'FIXED'


def test_parent_deactivation_is_blocked_until_active_children_are_inactive():
    actor = _manager()
    root = _category('FACILITIES', 'Facilities')
    child = _category('RENT', 'Rent', parent=root)

    blocked, status = ExpenseCategoryService.deactivate(root.id, actor=actor)
    assert status == 409
    assert blocked['code'] == 'EXPENSE_CATEGORY_ACTIVE_CHILDREN'

    _, child_status = ExpenseCategoryService.deactivate(child.id, actor=actor)
    _, root_status = ExpenseCategoryService.deactivate(root.id, actor=actor)
    assert child_status == 200
    assert root_status == 200


def test_reclassification_previews_then_atomically_updates_pending_expenses():
    actor = _manager()
    source = _category('UNSORTED', 'Unsorted')
    parent = _category('FACILITIES', 'Facilities')
    target = _category(
        'CLEANING',
        'Cleaning supplies',
        parent=parent,
        cost_behavior=ExpenseCategory.CostBehavior.VARIABLE,
        reporting_group='OPERATING',
        requires_description=True,
    )
    expenses = []
    for amount in (100_000, 150_000):
        result, status = ExpenseService.create(
            actor=actor,
            category_id=source.id,
            amount_uzs=amount,
            expense_date=date(2026, 9, 14),
            requested_source='SAFE',
            description='Cleaning materials',
        )
        assert status == 201
        expenses.append(Expense.objects.get(pk=result['data']['expense_id']))

    preview, preview_status = ExpenseService.reclassify_pending(
        actor=actor,
        expense_ids=[row.id for row in expenses],
        category_id=target.id,
        expected_category_id=source.id,
        dry_run=True,
    )
    assert preview_status == 200
    assert preview['data']['reclassification']['mutation_applied'] is False
    assert preview['data']['reclassification']['amount_uzs'] == 250_000
    assert not Expense.objects.filter(
        id__in=[row.id for row in expenses], category=target,
    ).exists()

    applied, applied_status = ExpenseService.reclassify_pending(
        actor=actor,
        expense_ids=[row.id for row in expenses],
        category_id=target.id,
        expected_category_id=source.id,
        reason='Reviewed against the September receipts',
        dry_run=False,
        idempotency_key='reclassify-cleaning-september',
    )
    assert applied_status == 200
    assert applied['data']['reclassification']['mutation_applied'] is True
    assert Expense.objects.filter(
        id__in=[row.id for row in expenses],
        category=target,
        category_parent_name_snapshot='Facilities',
        category_cost_behavior_snapshot='VARIABLE',
    ).count() == 2
    events = ExpenseTransition.objects.filter(
        expense_id__in=[row.id for row in expenses],
        metadata__event='CATEGORY_RECLASSIFIED',
    )
    assert events.count() == 2
    assert all(
        event.metadata['category_before']['category_id'] == source.id
        for event in events
    )

    unchanged, unchanged_status = ExpenseService.reclassify_pending(
        actor=actor,
        expense_ids=[row.id for row in expenses],
        category_id=target.id,
        expected_category_id=target.id,
        reason='No actual category change',
        dry_run=False,
    )
    assert unchanged_status == 409
    assert unchanged['code'] == 'EXPENSE_RECLASSIFICATION_NO_CHANGE'
    assert events.count() == 2


def test_reclassification_endpoint_is_idempotent_and_never_crosses_branch_scope():
    actor = _manager()
    client = _client(actor)
    source = _category('UNSORTED', 'Unsorted')
    target = _category('OFFICE', 'Office', cost_behavior='FIXED')
    own, own_status = ExpenseService.create(
        actor=actor,
        category_id=source.id,
        amount_uzs=80_000,
        expense_date='2026-09-14',
        requested_source='SAFE',
    )
    assert own_status == 201
    foreign = Expense.objects.create(
        category=source,
        category_code_snapshot=source.code,
        category_name_snapshot=source.name,
        amount=90_000,
        expense_date=date(2026, 9, 14),
        status=Expense.Status.PENDING,
        requested_source='SAFE',
        created_by=actor,
        branch_id='branch2',
    )
    payload = {
        'expense_ids': [own['data']['expense_id']],
        'category_id': target.id,
        'expected_category_id': source.id,
        'reason': 'Manager review',
        'dry_run': False,
    }
    first = client.post(
        '/api/admins/expenses/reclassify',
        json.dumps(payload),
        content_type='application/json',
        HTTP_IDEMPOTENCY_KEY='expense-reclassification-1',
    )
    replay = client.post(
        '/api/admins/expenses/reclassify',
        json.dumps(payload),
        content_type='application/json',
        HTTP_IDEMPOTENCY_KEY='expense-reclassification-1',
    )
    assert first.status_code == 200, first.content
    assert replay.status_code == 200, replay.content
    assert replay.json() == first.json()
    assert ExpenseTransition.objects.filter(
        expense_id=own['data']['expense_id'],
        metadata__event='CATEGORY_RECLASSIFIED',
    ).count() == 1

    # Simulate a worker committing the business transaction before its HTTP
    # idempotency response cache is saved. The deterministic action ID must
    # recover the same response without another transition or audit event.
    IdempotencyKey.objects.all().delete()
    recovered = client.post(
        '/api/admins/expenses/reclassify',
        json.dumps(payload),
        content_type='application/json',
        HTTP_IDEMPOTENCY_KEY='expense-reclassification-1',
    )
    assert recovered.status_code == 200, recovered.content
    assert recovered.json() == first.json()
    assert ExpenseTransition.objects.filter(
        expense_id=own['data']['expense_id'],
        metadata__event='CATEGORY_RECLASSIFIED',
    ).count() == 1
    assert AuditLog.objects.filter(
        action=AuditLog.Action.EXPENSE_CATEGORY_UPDATE,
        target_type='ExpenseBatch',
        target_id=target.id,
    ).count() == 1

    forbidden = client.post(
        '/api/admins/expenses/reclassify',
        json.dumps({
            **payload,
            'expense_ids': [foreign.id],
        }),
        content_type='application/json',
        HTTP_IDEMPOTENCY_KEY='expense-reclassification-foreign',
    )
    assert forbidden.status_code == 404
    foreign.refresh_from_db()
    assert foreign.category_id == source.id
