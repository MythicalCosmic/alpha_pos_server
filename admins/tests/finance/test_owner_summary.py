import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.test import Client
from django.utils import timezone

from admins.services.owner_summary_service import get_owner_summary
from base.financial import FinancialReportingGroup
from base.models import Session, TreasuryAccount, User
from base.repositories import SessionRepository
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from hr.models import Employee, Expense, ExpenseCategory, SalaryPayment
from stock.models import Supplier


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
    user_agent = f"owner-summary-{user.id}"
    Session.objects.create(
        user_id=user,
        ip_address="127.0.0.1",
        user_agent=user_agent,
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_USER_AGENT=user_agent)


def _category(code, group):
    return ExpenseCategory.objects.create(
        code=code,
        name=code.title(),
        reporting_group=group,
        branch_id=BRANCH,
    )


def _expense(category, amount, day, status=Expense.Status.PENDING):
    return Expense.objects.create(
        category=category,
        category_code_snapshot=category.code,
        category_name_snapshot=category.name,
        category_reporting_group_snapshot=category.reporting_group,
        amount=Decimal(amount),
        expense_date=day,
        status=status,
        requested_source=Expense.Source.SAFE,
        branch_id=BRANCH,
    )


@pytest.fixture
def ledger():
    inventory = _category("MEAT", FinancialReportingGroup.INVENTORY_PURCHASE)
    operating = _category("NAPKINS", FinancialReportingGroup.OPERATING)
    payroll = _category("STAFF", FinancialReportingGroup.PAYROLL)
    owner = _category("OWNER", FinancialReportingGroup.OWNER_DRAW)
    review = _category("UNCLEAR", FinancialReportingGroup.REVIEW)

    in_range = date(2026, 8, 5)
    _expense(inventory, "4000000", in_range)
    _expense(inventory, "1000000", in_range, status=Expense.Status.PAID)
    _expense(inventory, "9000000", in_range, status=Expense.Status.CANCELED)
    _expense(inventory, "7000000", date(2026, 9, 1))
    _expense(operating, "300000", in_range)
    _expense(payroll, "700000", in_range)
    _expense(owner, "500000", in_range)
    _expense(review, "45000", in_range)

    TreasuryAccount.objects.create(kind=TreasuryAccount.Kind.SAFE, balance=Decimal("2172000"), branch_id=BRANCH)
    TreasuryAccount.objects.create(kind=TreasuryAccount.Kind.BANK, balance=Decimal("5556000"), branch_id=BRANCH)
    Supplier.objects.create(name="Donar go'sht", current_balance=Decimal("4254000"), branch_id=BRANCH)
    Supplier.objects.create(name="Paid off", current_balance=Decimal("0"), branch_id=BRANCH)


def test_summary_separates_suppliers_operating_payroll_and_owner_money(ledger):
    data = get_owner_summary("2026-08-01", "2026-08-10", branch_id=BRANCH)

    costs = data["costs"]
    assert costs["suppliers"]["total_uzs"] == 5_000_000
    assert costs["suppliers"]["from_expenses_uzs"] == 5_000_000
    assert costs["suppliers"]["count"] == 2
    assert costs["operating"]["total_uzs"] == 300_000
    assert costs["payroll"]["total_uzs"] == 700_000
    assert costs["total_uzs"] == 6_000_000
    assert data["outside_profit"]["owner_withdrawals"]["total_uzs"] == 500_000
    assert data["outside_profit"]["unclassified"]["total_uzs"] == 45_000

    assert data["sales"]["net_sales_uzs"] == 0
    assert data["profit"]["raw_profit_uzs"] == -6_000_000
    assert data["profit"]["after_owner_withdrawals_uzs"] == -6_500_000
    assert data["profit"]["raw_margin_pct"] is None

    assert data["balances"] == {
        "safe_uzs": 2_172_000,
        "bank_uzs": 5_556_000,
        "total_uzs": 7_728_000,
        "treasury_updated_at": data["balances"]["treasury_updated_at"],
        "supplier_debt_uzs": 4_254_000,
        "suppliers_with_debt": 1,
    }

    codes = {warning["code"]: warning for warning in data["warnings"]}
    assert set(codes) == {
        "SALARY_RECORDS_MISSING",
        "PAYROLL_RECORDED_AS_EXPENSES",
        "SUPPLIER_PURCHASES_RECORDED_AS_EXPENSES",
        "UNCLASSIFIED_EXPENSES",
        "EXPENSES_NOT_PAID_THROUGH_TREASURY",
    }
    assert codes["EXPENSES_NOT_PAID_THROUGH_TREASURY"] == {
        "code": "EXPENSES_NOT_PAID_THROUGH_TREASURY", "count": 5, "amount_uzs": 5_545_000,
    }


def test_paid_salary_payments_count_as_payroll(ledger):
    worker = _user("CASHIER", "cashier@example.test")
    employee = Employee.objects.create(user=worker, position="Cashier", hire_date=date(2026, 1, 1), branch_id=BRANCH)
    SalaryPayment.objects.create(
        employee=employee,
        period_year=2026,
        period_month=7,
        base_amount=Decimal("3000000"),
        net_amount=Decimal("3000000"),
        status=SalaryPayment.Status.PAID,
        paid_at=datetime(2026, 8, 3, 12, tzinfo=TASHKENT),
        branch_id=BRANCH,
    )

    data = get_owner_summary("2026-08-01", "2026-08-10", branch_id=BRANCH)

    payroll = data["costs"]["payroll"]
    assert payroll["from_salary_payments_uzs"] == 3_000_000
    assert payroll["salary_payment_count"] == 1
    assert payroll["total_uzs"] == 3_700_000
    assert "SALARY_RECORDS_MISSING" not in {warning["code"] for warning in data["warnings"]}


def test_endpoint_requires_an_administrator(ledger):
    url = "/api/admins/dashboard/owner-summary?from=2026-08-01&to=2026-08-10"

    assert Client().get(url).status_code == 401
    assert _client(_user("CASHIER", "cashier2@example.test")).get(url).status_code == 403

    response = _client(_user("ADMIN", "admin@example.test")).get(url)

    assert response.status_code == 200
    assert response.json()["data"]["costs"]["suppliers"]["total_uzs"] == 5_000_000


def test_invalid_range_returns_422(ledger):
    response = _client(_user("ADMIN", "admin2@example.test")).get(
        "/api/admins/dashboard/owner-summary?from_at=2026-08-01T07:00:00%2B05:00",
    )

    assert response.status_code == 422
