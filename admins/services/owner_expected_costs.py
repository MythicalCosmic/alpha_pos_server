"""Costs the owner summary expects but that are not recorded yet.

The owner summary is cash-basis: it only knows what has been entered. In a
running month that makes profit look far too high, because salaries are paid
at month end, bills arrive late and supplier invoices are typed in weeks after
the goods. This block estimates what is still missing for the same window, so
the owner sees a number closer to reality next to the recorded one:

- salaries: the decided monthly payroll (a PAYROLL recurring cost, e.g. from
  the owner's salary workbook), spread over the days of the window;
- rent, utilities, taxes: their fixed monthly amount, or last month's paid
  bills, spread the same way;
- supplier purchases: an ESTIMATE at the supplier share of sales of the last
  complete month, when that month looks complete.

Each line keeps its basis so nothing is presented as recorded. Anything the
app cannot estimate (taxes never recorded, cash missing from the till) is
listed in ``not_included``.
"""
import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from zoneinfo import ZoneInfo

from django.db.models import Q, Sum

from admins.models import RecurringCost
from base.financial import FinancialReportingGroup as Group
from base.money import uzs_int
from hr.models import Expense, SalaryPayment

TASHKENT = ZoneInfo('Asia/Tashkent')

ZERO = Decimal('0')
BILL_GROUPS = (Group.RENT, Group.UTILITIES, Group.TAXES)
COUNTED = (Expense.Status.PENDING, Expense.Status.APPROVED, Expense.Status.PAID)
# A reference month whose supplier costs fall outside this share of sales is
# itself incomplete (or unusual) and must not drive an estimate.
SUPPLIER_SHARE_RANGE = (Decimal('0.25'), Decimal('0.85'))
# Taxes below this share of sales mean real taxes are simply not recorded.
TAX_SHARE_FLOOR = Decimal('0.005')
TILL_CASH_CATEGORY = 'SF_TILL_CASH_NOT_RECEIVED'


def _months(date_from, date_to):
    """Yield (month_start, month_end, month_days, covered_days) for the window."""
    current = date_from.replace(day=1)
    while current <= date_to:
        days = calendar.monthrange(current.year, current.month)[1]
        month_end = current.replace(day=days)
        covered = (min(date_to, month_end) - max(date_from, current)).days + 1
        yield current, month_end, days, covered
        current = month_end + timedelta(days=1)


def _share(amount, covered, days):
    return (Decimal(amount) * covered / days).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def _group_filter(groups):
    return (
        Q(category_reporting_group_snapshot__in=groups)
        | Q(category_reporting_group_snapshot='', category__reporting_group__in=groups)
    )


def _expense_total(branch_id, groups, date_from, date_to, statuses=COUNTED):
    return Expense.objects.filter(
        _group_filter(groups), is_deleted=False, branch_id=branch_id, status__in=statuses,
        expense_date__gte=date_from, expense_date__lte=date_to,
    ).aggregate(total=Sum('amount'))['total'] or ZERO


def _schedules(branch_id, group, month_start, month_end):
    return RecurringCost.objects.filter(
        branch_id=branch_id, is_active=True, reporting_group=group,
        start_date__lte=month_end,
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=month_start))


def _payroll_recorded(branch_id, seg_from, seg_to):
    """Payroll recorded in a date span, counted like the owner summary does."""
    expenses = _expense_total(branch_id, (Group.PAYROLL,), seg_from, seg_to)
    salaries = ZERO
    for paid_at, amount in SalaryPayment.objects.filter(
        is_deleted=False, branch_id=branch_id, status=SalaryPayment.Status.PAID, paid_at__isnull=False,
    ).values_list('paid_at', 'net_amount'):
        if seg_from <= paid_at.astimezone(TASHKENT).date() <= seg_to:
            salaries += Decimal(amount or 0)
    return expenses + salaries


def _planned(branch_id, group, date_from, date_to, *, history):
    """Plan vs recorded for ``group``, month by month inside the window.

    Each calendar month's plan is compared only with what was recorded in that
    same month's part of the window, so last month's payment can never cover
    this month's plan when a window spans two months.
    """
    planned = recorded = remaining = ZERO
    monthly, basis, reference = ZERO, None, None
    for month_start, month_end, days, covered in _months(date_from, date_to):
        seg_from, seg_to = max(date_from, month_start), min(date_to, month_end)
        schedules = list(_schedules(branch_id, group, month_start, month_end))
        if schedules:
            amount = sum((Decimal(s.monthly_amount) for s in schedules), ZERO)
            basis = basis or 'FIXED'
        elif history:
            previous_end = month_start - timedelta(days=1)
            previous_start = previous_end.replace(day=1)
            amount = _expense_total(
                branch_id, (group,), previous_start, previous_end, statuses=(Expense.Status.PAID,),
            )
            if amount:
                basis = basis or 'LAST_MONTH'
                reference = reference or previous_start.strftime('%Y-%m')
        else:
            amount = ZERO
        if group == Group.PAYROLL:
            seg_recorded = _payroll_recorded(branch_id, seg_from, seg_to)
        else:
            seg_recorded = _expense_total(branch_id, (group,), seg_from, seg_to)
        seg_planned = _share(amount, covered, days)
        monthly = max(monthly, amount)
        planned += seg_planned
        recorded += seg_recorded
        if amount:
            remaining += max(seg_planned - seg_recorded, ZERO)
    return {'planned': planned, 'recorded': recorded, 'remaining': remaining,
            'monthly': monthly, 'basis': basis, 'reference': reference}


def expected_costs(branch_id, window, *, net_sales, supplier_total, payroll_total,
                   raw_profit, owner_withdrawals, reference_summary, today=None):
    today = today or date.today()
    date_from = window.date_from
    date_to = min(window.date_to, today)
    if date_to < date_from:
        return None
    covered_days = (date_to - date_from).days + 1

    lines = []
    plan = _planned(branch_id, Group.PAYROLL, date_from, date_to, history=False)
    salary_basis = plan['basis']
    salaries = {
        'basis': salary_basis,
        'monthly_plan_uzs': uzs_int(plan['monthly']),
        'planned_uzs': uzs_int(plan['planned']),
        'recorded_uzs': uzs_int(plan['recorded']),
        'remaining_uzs': uzs_int(plan['remaining']),
    }
    if salary_basis:
        lines.append(salaries['remaining_uzs'])

    bills = []
    for group in BILL_GROUPS:
        plan = _planned(branch_id, group, date_from, date_to, history=True)
        bills.append({
            'reporting_group': group,
            'basis': plan['basis'],
            'reference_month': plan['reference'],
            'monthly_plan_uzs': uzs_int(plan['monthly']),
            'planned_uzs': uzs_int(plan['planned']),
            'recorded_uzs': uzs_int(plan['recorded']),
            'remaining_uzs': uzs_int(plan['remaining']),
        })
        lines.append(bills[-1]['remaining_uzs'])

    suppliers = None
    if reference_summary and net_sales > 0:
        ref_sales = Decimal(reference_summary['sales']['net_sales_uzs'])
        ref_suppliers = Decimal(reference_summary['costs']['suppliers']['total_uzs'])
        share = ref_suppliers / ref_sales if ref_sales > 0 else ZERO
        low, high = SUPPLIER_SHARE_RANGE
        if low <= share <= high:
            expected = (share * net_sales).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            suppliers = {
                'reference_month': reference_summary['range']['from'][:7],
                'reference_share_pct': str((share * 100).quantize(Decimal('0.1'))),
                'expected_uzs': uzs_int(expected),
                'recorded_uzs': uzs_int(supplier_total),
                'remaining_uzs': uzs_int(max(expected - supplier_total, ZERO)),
            }
            lines.append(suppliers['remaining_uzs'])

    not_included = []
    taxes = _expense_total(branch_id, (Group.TAXES,), date_from, date_to)
    if net_sales > 0 and Decimal(taxes) < net_sales * TAX_SHARE_FLOOR:
        not_included.append('TAXES')
    till_loss = Expense.objects.filter(
        is_deleted=False, branch_id=branch_id, status__in=COUNTED,
        category_code_snapshot=TILL_CASH_CATEGORY,
        expense_date__gte=date_from, expense_date__lte=date_to,
    ).exists()
    if not till_loss:
        not_included.append('TILL_CASH_SHORTAGE')
    if not salary_basis:
        not_included.append('SALARY_PLAN')

    remaining = sum(lines)
    estimated = uzs_int(raw_profit) - remaining
    return {
        'as_of': date_to.isoformat(),
        'covered_days': covered_days,
        'salaries': salaries,
        'bills': bills,
        'suppliers': suppliers,
        'total_remaining_uzs': remaining,
        'estimated_profit_uzs': estimated,
        'estimated_after_owner_withdrawals_uzs': estimated - uzs_int(owner_withdrawals),
        'not_included': not_included,
    }


def reference_month(window):
    """The calendar month before the window's last month."""
    last_month_start = window.date_to.replace(day=1)
    end = last_month_start - timedelta(days=1)
    return end.replace(day=1), end
