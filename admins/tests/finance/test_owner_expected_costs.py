"""Expected-but-unrecorded costs on the owner summary."""
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from admins.models import RecurringCost
from admins.services.owner_summary_service import get_owner_summary
from base.financial import FinancialReportingGroup as Group
from base.models import Order, User
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from hr.models import Expense, ExpenseCategory

pytestmark = pytest.mark.django_db
BRANCH = 'branch1'
TASHKENT = ZoneInfo('Asia/Tashkent')


@pytest.fixture
def cashier():
    return User.objects.create(
        first_name='Cash', last_name='Ier', email='expected-costs@test.local', password='!',
        role=User.RoleChoices.CASHIER, status=User.UserStatus.ACTIVE,
        permissions=DEFAULT_ROLE_PERMISSIONS[User.RoleChoices.CASHIER], branch_id=BRANCH,
    )


@pytest.fixture
def today_is(monkeypatch):
    def set_today(day):
        monkeypatch.setattr('base.services.business_day.business_date', lambda *a, **k: day)
    return set_today


def _sale(cashier, amount, day, number):
    Order.objects.create(
        user=cashier, cashier=cashier, branch_id=BRANCH, display_id=number,
        status=Order.Status.COMPLETED, is_paid=True, payment_method=Order.PaymentMethod.CASH,
        subtotal=Decimal(amount), total_amount=Decimal(amount),
        paid_at=datetime(day.year, day.month, day.day, 12, tzinfo=TASHKENT),
    )


def _category(code, group):
    return ExpenseCategory.objects.create(code=code, name=code.title(), reporting_group=group, branch_id=BRANCH)


def _expense(category, amount, day, status=Expense.Status.PAID):
    return Expense.objects.create(
        category=category, category_code_snapshot=category.code, category_name_snapshot=category.name,
        category_reporting_group_snapshot=category.reporting_group, amount=Decimal(amount),
        expense_date=day, status=status, requested_source=Expense.Source.DRAWER, branch_id=BRANCH,
    )


def test_running_month_shows_what_is_still_missing(cashier, today_is):
    meat = _category('MEAT', Group.INVENTORY_PURCHASE)
    rent = _category('RENT', Group.RENT)
    power = _category('POWER', Group.UTILITIES)
    staff = _category('STAFF', Group.PAYROLL)
    # August, the last complete month: suppliers are 58% of sales.
    _sale(cashier, '1000000', date(2026, 8, 10), 1)
    _expense(meat, '580000', date(2026, 8, 10))
    _expense(rent, '150000', date(2026, 8, 1))
    _expense(power, '120000', date(2026, 8, 20))
    # September so far (10 of 30 days).
    _sale(cashier, '600000', date(2026, 9, 5), 2)
    _expense(meat, '100000', date(2026, 9, 5))
    _expense(staff, '50000', date(2026, 9, 6))
    RecurringCost.objects.create(
        branch_id=BRANCH, name='Salaries (workbook)', reporting_group=Group.PAYROLL,
        monthly_amount=Decimal('900000'), start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
        created_by=cashier,
    )
    today_is(date(2026, 9, 10))

    data = get_owner_summary('2026-09-01', '2026-09-30', branch_id=BRANCH, include_expected=True)

    assert data['profit']['raw_profit_uzs'] == 450_000   # 600k - 100k - 50k, unchanged
    expected = data['expected']
    assert expected['as_of'] == '2026-09-10' and expected['covered_days'] == 10
    assert expected['salaries'] == {
        'basis': 'FIXED', 'monthly_plan_uzs': 900_000, 'planned_uzs': 300_000,
        'recorded_uzs': 50_000, 'remaining_uzs': 250_000,
    }
    bills = {row['reporting_group']: row for row in expected['bills']}
    assert bills[Group.RENT] | {} == {
        'reporting_group': Group.RENT, 'basis': 'LAST_MONTH', 'reference_month': '2026-08',
        'monthly_plan_uzs': 150_000, 'planned_uzs': 50_000, 'recorded_uzs': 0, 'remaining_uzs': 50_000,
    }
    assert bills[Group.UTILITIES]['remaining_uzs'] == 40_000
    assert bills[Group.TAXES]['basis'] is None and bills[Group.TAXES]['remaining_uzs'] == 0
    assert expected['suppliers'] == {
        'reference_month': '2026-08', 'reference_share_pct': '58.0',
        'expected_uzs': 348_000, 'recorded_uzs': 100_000, 'remaining_uzs': 248_000,
    }
    assert expected['total_remaining_uzs'] == 250_000 + 50_000 + 40_000 + 248_000
    assert expected['estimated_profit_uzs'] == 450_000 - 588_000
    assert expected['not_included'] == ['TAXES', 'TILL_CASH_SHORTAGE']


def test_no_plan_and_no_complete_reference_month_estimates_nothing(cashier, today_is):
    meat = _category('MEAT', Group.INVENTORY_PURCHASE)
    _sale(cashier, '1000000', date(2026, 8, 10), 1)
    _expense(meat, '100000', date(2026, 7, 10))          # July: 100k of 0 sales
    loss = _category('SF_TILL_CASH_NOT_RECEIVED', Group.OPERATING)
    _expense(loss, '30000', date(2026, 8, 31))
    today_is(date(2026, 9, 22))

    data = get_owner_summary('2026-08-01', '2026-08-31', branch_id=BRANCH, include_expected=True)

    expected = data['expected']
    assert expected['covered_days'] == 31
    assert expected['salaries']['basis'] is None
    assert expected['suppliers'] is None
    assert expected['total_remaining_uzs'] == 0
    assert expected['estimated_profit_uzs'] == data['profit']['raw_profit_uzs']
    # The August till loss is recorded, so it is not listed as missing.
    assert expected['not_included'] == ['TAXES', 'SALARY_PLAN']


def test_future_days_of_the_window_are_not_planned(cashier, today_is):
    RecurringCost.objects.create(
        branch_id=BRANCH, name='Rent', reporting_group=Group.RENT, monthly_amount=Decimal('3000000'),
        start_date=date(2026, 1, 1), created_by=cashier,
    )
    today_is(date(2026, 9, 3))
    data = get_owner_summary('2026-09-01', '2026-09-30', branch_id=BRANCH, include_expected=True)
    rent = next(row for row in data['expected']['bills'] if row['reporting_group'] == Group.RENT)
    assert rent['basis'] == 'FIXED'
    assert rent['planned_uzs'] == 300_000     # 3 of 30 days, not the whole month


def test_last_months_payment_never_covers_this_months_plan(cashier, today_is):
    """A 30-day window spanning August and September: August's salary and rent
    payments must not hide September's salaries and rent still to pay."""
    staff = _category('STAFF', Group.PAYROLL)
    rent = _category('RENT', Group.RENT)
    _expense(staff, '910000', date(2026, 8, 31))            # August payroll, paid Aug 31
    _expense(rent, '150000', date(2026, 8, 28))              # August rent
    _expense(staff, '50000', date(2026, 9, 6))               # September advance
    RecurringCost.objects.create(
        branch_id=BRANCH, name='Salaries (workbook)', reporting_group=Group.PAYROLL,
        monthly_amount=Decimal('900000'), start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
        created_by=cashier,
    )
    today_is(date(2026, 9, 10))

    data = get_owner_summary('2026-08-12', '2026-09-10', branch_id=BRANCH, include_expected=True)

    expected = data['expected']
    # September: 900k x 10/30 = 300k planned, 50k recorded; August has no plan.
    assert expected['salaries']['remaining_uzs'] == 250_000
    rent_row = next(row for row in expected['bills'] if row['reporting_group'] == Group.RENT)
    # September rent planned from August's 150k bill: 150k x 10/30 = 50k, none paid in September.
    assert rent_row['remaining_uzs'] == 50_000
    # Only the planned month's records are shown next to the plan.
    assert expected['salaries']['recorded_uzs'] == 50_000
    assert rent_row['recorded_uzs'] == 0
