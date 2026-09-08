import json
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.test import Client
from django.utils import timezone

from admins.models import RecurringCost
from admins.services.cash_position_service import CashPositionService
from base.financial import FinancialReportingGroup
from base.models import Session, TreasuryAccount, TreasuryTransaction, User
from base.repositories import SessionRepository
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from hr.models import Employee, Expense, ExpenseCategory, SalaryPayment
from stock.models import Supplier, SupplierTransaction


pytestmark = pytest.mark.django_db
BRANCH = "branch1"
TASHKENT = ZoneInfo("Asia/Tashkent")


def _user(role, email, *, branch=BRANCH):
    return User.objects.create(
        first_name=role.title(),
        last_name="Tester",
        email=email,
        password="!",
        role=role,
        status=User.UserStatus.ACTIVE,
        permissions=DEFAULT_ROLE_PERMISSIONS[role],
        branch_id=branch,
    )


def _client(user):
    token = secrets.token_hex(32)
    user_agent = f"cash-position-{user.id}"
    Session.objects.create(
        user_id=user,
        ip_address="127.0.0.1",
        user_agent=user_agent,
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return Client(
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_USER_AGENT=user_agent,
    )


def _freeze(monkeypatch, year=2026, month=9, day=17):
    frozen = datetime(year, month, day, 12, tzinfo=TASHKENT)
    monkeypatch.setattr(timezone, "now", lambda: frozen)
    return frozen


def _treasury(kind, entries):
    balance = sum((Decimal(str(delta)) for delta in entries), Decimal("0"))
    account = TreasuryAccount.objects.create(
        kind=kind,
        balance=balance,
        branch_id=BRANCH,
    )
    running = Decimal("0")
    transactions = []
    for index, delta in enumerate(entries):
        delta = Decimal(str(delta))
        row = TreasuryTransaction.objects.create(
            account=account,
            type=(
                TreasuryTransaction.Type.ADJUSTMENT
                if delta >= 0 else TreasuryTransaction.Type.EXPENSE
            ),
            delta=delta,
            balance_before=running,
            balance_after=running + delta,
            branch_id=BRANCH,
            category=f"test-{index}",
        )
        running += delta
        transactions.append(row)
    return account, transactions


def test_cash_position_calculates_each_net_step_and_keeps_unreviewed_debt(
    monkeypatch,
):
    _freeze(monkeypatch)
    actor = _user(User.RoleChoices.MANAGER, "manager-position@test.local")
    _treasury(TreasuryAccount.Kind.SAFE, [10_000_000])
    _bank, bank_rows = _treasury(
        TreasuryAccount.Kind.BANK,
        [10_000_000, -3_100_000],
    )

    verified = Supplier.objects.create(
        name="Verified supplier",
        current_balance=2_000_000,
        branch_id=BRANCH,
    )
    SupplierTransaction.objects.create(
        supplier=verified,
        type=SupplierTransaction.Type.PURCHASE,
        amount=2_000_000,
        balance_before=0,
        balance_after=2_000_000,
        branch_id=BRANCH,
    )
    unreviewed = Supplier.objects.create(
        name="Opening debt supplier",
        current_balance=4_047_000,
        branch_id=BRANCH,
    )

    employee_user = _user(
        User.RoleChoices.USER,
        "payroll-employee@test.local",
    )
    employee = Employee.objects.create(
        user=employee_user,
        position="Cook",
        hire_date=date(2025, 1, 1),
        base_salary=3_000_000,
        branch_id=BRANCH,
    )
    SalaryPayment.objects.create(
        employee=employee,
        period_year=2026,
        period_month=8,
        base_amount=3_100_000,
        net_amount=3_100_000,
        status=SalaryPayment.Status.PAID,
        branch_id=BRANCH,
    )

    utilities = ExpenseCategory.objects.create(
        code="ELECTRICITY",
        name="Electricity",
        reporting_group=FinancialReportingGroup.UTILITIES,
        branch_id=BRANCH,
    )
    Expense.objects.create(
        category=utilities,
        category_code_snapshot=utilities.code,
        category_name_snapshot=utilities.name,
        amount=3_100_000,
        expense_date=date(2026, 8, 20),
        status=Expense.Status.PAID,
        treasury_transaction=bank_rows[-1],
        branch_id=BRANCH,
    )
    RecurringCost.objects.create(
        branch_id=BRANCH,
        name="Rent",
        reporting_group=FinancialReportingGroup.RENT,
        monthly_amount=3_000_000,
        start_date=date(2026, 1, 1),
        created_by=actor,
    )

    body, status = CashPositionService.get(actor=actor)

    assert status == 200
    position = body["data"]
    assert position["funds"] == {
        "safe_uzs": 10_000_000,
        "bank_uzs": 6_900_000,
        "total_uzs": 16_900_000,
    }
    assert position["supplier_debts"]["total_uzs"] == 6_047_000
    assert position["supplier_debts"]["review_required_count"] == 1
    opening_row = next(
        row for row in position["supplier_debts"]["rows"]
        if row["supplier_id"] == unreviewed.id
    )
    assert opening_row["amount_uzs"] == 4_047_000
    assert opening_row["evidence_status"] == "OPENING_BALANCE_REVIEW_REQUIRED"

    # Sep 17 uses the Aug 31-day baseline: 3,100,000 / 31 * 17.
    assert position["payroll"]["due_estimate_uzs"] == 1_700_000
    historical = next(
        row for row in position["monthly_costs"]["rows"]
        if row["basis"] == "PREVIOUS_MONTH_ACTUAL"
    )
    assert historical["accrued_estimate_uzs"] == 1_700_000
    assert historical["divisor_days"] == 31
    fixed = next(
        row for row in position["monthly_costs"]["rows"]
        if row["basis"] == "FIXED_MONTHLY"
    )
    assert fixed["accrued_estimate_uzs"] == 1_700_000
    assert fixed["divisor_days"] == 30
    assert position["positions"] == {
        "after_suppliers_uzs": 10_853_000,
        "after_payroll_uzs": 9_153_000,
        "final_uzs": 5_753_000,
    }
    assert position["status"] == "REVIEW_REQUIRED"
    assert any(
        issue["code"] == "SUPPLIER_OPENING_BALANCE_REVIEW_REQUIRED"
        for issue in position["data_quality"]["issues"]
    )


def test_fixed_utilities_override_previous_month_utility_history(monkeypatch):
    _freeze(monkeypatch)
    actor = _user(User.RoleChoices.ADMIN, "admin-utilities@test.local")
    _treasury(TreasuryAccount.Kind.SAFE, [5_000_000])
    _bank, bank_rows = _treasury(
        TreasuryAccount.Kind.BANK,
        [5_000_000, -3_100_000],
    )
    category = ExpenseCategory.objects.create(
        code="POWER",
        name="Power",
        reporting_group=FinancialReportingGroup.UTILITIES,
        branch_id=BRANCH,
    )
    Expense.objects.create(
        category=category,
        category_name_snapshot="Power",
        amount=3_100_000,
        expense_date=date(2026, 8, 3),
        status=Expense.Status.PAID,
        treasury_transaction=bank_rows[-1],
        branch_id=BRANCH,
    )
    RecurringCost.objects.create(
        branch_id=BRANCH,
        name="Fixed utilities plan",
        reporting_group=FinancialReportingGroup.UTILITIES,
        monthly_amount=3_000_000,
        start_date=date(2026, 1, 1),
        created_by=actor,
    )

    body, status = CashPositionService.get(actor=actor)

    assert status == 200
    costs = body["data"]["monthly_costs"]
    assert costs["historical_utility_count"] == 0
    assert costs["due_estimate_uzs"] == 1_700_000
    assert [row["basis"] for row in costs["rows"]] == ["FIXED_MONTHLY"]


def test_current_month_paid_cost_is_not_reserved_twice(monkeypatch):
    _freeze(monkeypatch)
    actor = _user(User.RoleChoices.ADMIN, "admin-paid-cost@test.local")
    _safe, safe_rows = _treasury(
        TreasuryAccount.Kind.SAFE,
        [10_000_000, -1_000_000],
    )
    _treasury(TreasuryAccount.Kind.BANK, [0])
    rent = ExpenseCategory.objects.create(
        code="RENT",
        name="Rent",
        reporting_group=FinancialReportingGroup.RENT,
        branch_id=BRANCH,
    )
    Expense.objects.create(
        category=rent,
        category_code_snapshot=rent.code,
        category_name_snapshot=rent.name,
        amount=1_000_000,
        expense_date=date(2026, 9, 5),
        status=Expense.Status.PAID,
        treasury_transaction=safe_rows[-1],
        branch_id=BRANCH,
    )
    RecurringCost.objects.create(
        branch_id=BRANCH,
        name="Rent",
        reporting_group=FinancialReportingGroup.RENT,
        monthly_amount=3_000_000,
        start_date=date(2026, 1, 1),
        created_by=actor,
    )

    body, status = CashPositionService.get(actor=actor)

    assert status == 200
    costs = body["data"]["monthly_costs"]
    assert costs["accrued_estimate_uzs"] == 1_700_000
    assert costs["paid_current_period_uzs"] == 1_000_000
    assert costs["due_estimate_uzs"] == 700_000
    assert body["data"]["positions"]["final_uzs"] == 8_300_000


def test_recurring_cost_api_is_branch_scoped_and_manager_gated(monkeypatch):
    _freeze(monkeypatch)
    manager = _user(User.RoleChoices.MANAGER, "manager-cost@test.local")
    warehouse = _user(User.RoleChoices.WAREHOUSE, "warehouse-cost@test.local")

    response = _client(manager).post(
        "/api/admins/money-control/recurring-costs",
        json.dumps({
            "name": "Ijara",
            "reporting_group": "RENT",
            "monthly_amount": 3_000_000,
        }),
        content_type="application/json",
    )

    assert response.status_code == 201, response.content
    item = response.json()["data"]["item"]
    assert item["name"] == "Ijara"
    assert item["start_date"] == "2026-09-01"
    saved = RecurringCost.objects.get(id=item["id"])
    assert saved.branch_id == BRANCH

    forbidden = _client(warehouse).post(
        "/api/admins/money-control/recurring-costs",
        json.dumps({
            "name": "Tax",
            "reporting_group": "TAXES",
            "monthly_amount": 1_000_000,
        }),
        content_type="application/json",
    )
    assert forbidden.status_code == 403
    assert RecurringCost.objects.count() == 1


def test_cash_position_endpoint_permissions(monkeypatch):
    _freeze(monkeypatch)
    manager = _user(User.RoleChoices.MANAGER, "manager-view@test.local")
    warehouse = _user(User.RoleChoices.WAREHOUSE, "warehouse-view@test.local")
    _treasury(TreasuryAccount.Kind.SAFE, [1_000_000])
    _treasury(TreasuryAccount.Kind.BANK, [2_000_000])

    allowed = _client(manager).get("/api/admins/money-control/cash-position")
    forbidden = _client(warehouse).get("/api/admins/money-control/cash-position")

    assert allowed.status_code == 200
    assert allowed.json()["data"]["funds"]["total_uzs"] == 3_000_000
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "PERMISSION_DENIED"
