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
from hr.models import Employee, Expense, ExpenseCategory, ExpenseSupplierLink, SalaryPayment
from stock.models import Supplier, SupplierPayment, SupplierTransaction


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


def _supplier_payment(amount, paid_at):
    supplier = Supplier.objects.get(name="Donar go'sht")
    entry = SupplierTransaction.objects.create(
        supplier=supplier, type=SupplierTransaction.Type.PAYMENT, amount=Decimal(amount), branch_id=BRANCH,
    )
    return SupplierPayment.objects.create(
        supplier=supplier, branch_id=BRANCH, principal_uzs=Decimal(amount), total_debited_uzs=Decimal(amount),
        source_account=SupplierPayment.SourceAccount.SAFE,
        allocation_mode=SupplierPayment.AllocationMode.AUTO_OLDEST_DUE, status=SupplierPayment.Status.POSTED,
        supplier_balance_before_uzs=Decimal("0"), supplier_balance_after_uzs=Decimal("0"),
        supplier_transaction=entry, paid_at=paid_at,
    )


def test_supplier_payment_is_flagged_only_when_an_expense_repeats_it(ledger):
    _supplier_payment("3000000", datetime(2026, 8, 6, 12, tzinfo=TASHKENT))

    data = get_owner_summary("2026-08-01", "2026-08-10", branch_id=BRANCH)

    assert data["costs"]["suppliers"]["from_supplier_payments_uzs"] == 3_000_000
    assert "SUPPLIER_LEDGER_OVERLAP_POSSIBLE" not in {warning["code"] for warning in data["warnings"]}

    _supplier_payment("4000000", datetime(2026, 8, 6, 1, tzinfo=TASHKENT))

    data = get_owner_summary("2026-08-01", "2026-08-10", branch_id=BRANCH)

    codes = {warning["code"]: warning for warning in data["warnings"]}
    assert codes["SUPPLIER_LEDGER_OVERLAP_POSSIBLE"] == {
        "code": "SUPPLIER_LEDGER_OVERLAP_POSSIBLE", "count": 1, "amount_uzs": 4_000_000,
    }


def test_supplier_purchase_warning_counts_only_unlinked_purchases(ledger):
    donar = Supplier.objects.get(name="Donar go'sht")
    linked = Expense.objects.get(amount=Decimal("4000000"))
    ExpenseSupplierLink.objects.create(expense=linked, supplier=donar, branch_id=BRANCH)

    data = get_owner_summary("2026-08-01", "2026-08-10", branch_id=BRANCH)

    assert data["costs"]["suppliers"]["total_uzs"] == 5_000_000
    codes = {warning["code"]: warning for warning in data["warnings"]}
    assert codes["SUPPLIER_PURCHASES_RECORDED_AS_EXPENSES"] == {
        "code": "SUPPLIER_PURCHASES_RECORDED_AS_EXPENSES", "count": 1, "amount_uzs": 1_000_000,
    }

    ExpenseSupplierLink.objects.create(
        expense=Expense.objects.get(amount=Decimal("1000000")), supplier=donar, branch_id=BRANCH,
    )

    data = get_owner_summary("2026-08-01", "2026-08-10", branch_id=BRANCH)

    assert "SUPPLIER_PURCHASES_RECORDED_AS_EXPENSES" not in {warning["code"] for warning in data["warnings"]}


def test_staff_payments_are_flagged_only_for_months_without_salaries(ledger):
    data = get_owner_summary("2026-08-01", "2026-08-10", branch_id=BRANCH)
    assert "PAYROLL_RECORDED_AS_EXPENSES" in {warning["code"] for warning in data["warnings"]}

    worker = _user("CASHIER", "august-worker@example.test")
    employee = Employee.objects.create(user=worker, position="Cashier", hire_date=date(2026, 1, 1), branch_id=BRANCH)
    SalaryPayment.objects.create(
        employee=employee, period_year=2026, period_month=8,
        base_amount=Decimal("3000000"), net_amount=Decimal("3000000"),
        status=SalaryPayment.Status.PAID, paid_at=datetime(2026, 8, 31, 23, tzinfo=TASHKENT), branch_id=BRANCH,
    )

    data = get_owner_summary("2026-08-01", "2026-08-10", branch_id=BRANCH)

    assert data["costs"]["payroll"]["from_expenses_uzs"] == 700_000
    assert "PAYROLL_RECORDED_AS_EXPENSES" not in {warning["code"] for warning in data["warnings"]}


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


def test_salary_advances_count_when_paid_and_the_full_salary_only_on_payday():
    from base.models import TreasuryTransaction
    from base.services.treasury_service import TreasuryService

    actor = _user("ADMIN", "advances-admin@test.local")
    staff = User.objects.create(first_name="Abror", last_name="Staff", email="abror.staff@test.local",
                                password="!", role=User.RoleChoices.USER, branch_id=BRANCH)
    employee = Employee.objects.create(user=staff, position="Cook", hire_date=date(2026, 9, 1),
                                       base_salary=Decimal("6050000"), branch_id=BRANCH)
    TreasuryAccount.objects.create(kind=TreasuryAccount.Kind.SAFE, balance=Decimal("5000000"), branch_id=BRANCH)
    salary = SalaryPayment.objects.create(
        employee=employee, period_year=2026, period_month=9, base_amount=Decimal("6050000"),
        net_amount=Decimal("6050000"), status=SalaryPayment.Status.PENDING, branch_id=BRANCH,
    )

    def advance(amount):
        body, status = TreasuryService.record_expense(
            "SAFE", amount, category="SALARY", txn_type=TreasuryTransaction.Type.SALARY_PAYMENT,
            description="advance", performed_by=actor, reference_type="SalaryPayment",
            reference_id=salary.id, branch_id=BRANCH,
        )
        assert status < 400, body
        return TreasuryTransaction.objects.get(pk=body["data"]["transaction"]["id"])

    today = timezone.localdate()
    window = (today - timedelta(days=1), today + timedelta(days=1))
    advance(1313000)
    reversed_advance = advance(200000)
    body, status = TreasuryService.reverse_transaction(
        reversed_advance.id, performed_by=actor, reason="typo", branch_id=BRANCH)
    assert status < 400, body

    payroll = get_owner_summary(*window, branch_id=BRANCH)["costs"]["payroll"]
    assert payroll["from_salary_advances_uzs"] == 1313000
    assert payroll["salary_advance_count"] == 1
    assert payroll["from_salary_payments_uzs"] == 0
    assert payroll["total_uzs"] == 1313000

    # Payday: the whole salary counts once, its advances are not added again.
    salary.status = SalaryPayment.Status.PAID
    salary.paid_at = timezone.now()
    salary.save()
    payroll = get_owner_summary(*window, branch_id=BRANCH)["costs"]["payroll"]
    assert payroll["from_salary_advances_uzs"] == 0
    assert payroll["total_uzs"] == 6050000
