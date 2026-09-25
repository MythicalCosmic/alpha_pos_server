"""Owner money summary for the dashboard.

A cash-basis view of one reporting window: net sales minus supplier purchases,
operating expenses and payroll, next to the current Safe/Bank balances and
supplier debt. Each figure keeps its sources visible, and ``warnings`` lists
data that is still incomplete instead of silently treating it as zero.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from admins.services.profitability_service import resolve_branch_id
from base.financial import FinancialReportingGroup as Group
from base.models import Order, OrderRefund, TreasuryAccount, TreasuryTransaction
from base.money import uzs_int
from base.services.business_day import resolve_reporting_window
from hr.models import Expense, SalaryPayment
from stock.models import Supplier, SupplierPayment

ZERO = Decimal('0')

# Rejected, canceled and voided requests are not spending.
COUNTED_EXPENSE_STATUSES = (
    Expense.Status.PENDING,
    Expense.Status.APPROVED,
    Expense.Status.PAID,
)

OPERATING_GROUPS = frozenset({
    Group.OPERATING,
    Group.UTILITIES,
    Group.RENT,
    Group.TAXES,
    Group.FINANCE_FEES,
    Group.WASTE_SPOILAGE,
    Group.DEPRECIATION,
})
OWNER_GROUPS = frozenset({Group.OWNER_DRAW, Group.NON_BUSINESS})

BUCKETS = ('suppliers', 'operating', 'payroll', 'owner', 'capital', 'unclassified')


def _bucket(reporting_group):
    if reporting_group == Group.INVENTORY_PURCHASE:
        return 'suppliers'
    if reporting_group == Group.PAYROLL:
        return 'payroll'
    if reporting_group in OPERATING_GROUPS:
        return 'operating'
    if reporting_group in OWNER_GROUPS:
        return 'owner'
    if reporting_group == Group.CAPITAL_EXPENDITURE:
        return 'capital'
    return 'unclassified'


def _expense_buckets(branch_id, window):
    rows = (
        Expense.objects.filter(
            is_deleted=False,
            branch_id=branch_id,
            status__in=COUNTED_EXPENSE_STATUSES,
            expense_date__gte=window.date_from,
            expense_date__lte=window.date_to,
        )
        .values(
            'status',
            'category_reporting_group_snapshot',
            'category__reporting_group',
            'category_parent_name_snapshot',
            'category_name_snapshot',
        )
        .annotate(amount=Sum('amount'), fees=Sum('fee_uzs'), count=Count('id'))
    )
    buckets = {
        name: {'total': ZERO, 'count': 0, 'categories': defaultdict(lambda: [ZERO, 0])}
        for name in BUCKETS
    }
    pending_total, pending_count = ZERO, 0

    for row in rows:
        group = row['category_reporting_group_snapshot'] or row['category__reporting_group'] or ''
        amount = (row['amount'] or ZERO) + (row['fees'] or ZERO)
        bucket = buckets[_bucket(group)]
        bucket['total'] += amount
        bucket['count'] += row['count']
        label = (row['category_parent_name_snapshot'], row['category_name_snapshot'])
        bucket['categories'][label][0] += amount
        bucket['categories'][label][1] += row['count']
        if row['status'] != Expense.Status.PAID:
            pending_total += amount
            pending_count += row['count']

    for bucket in buckets.values():
        bucket['categories'] = [
            {
                'parent': parent or '',
                'name': name or '',
                'amount_uzs': uzs_int(amount),
                'count': count,
            }
            for (parent, name), (amount, count) in sorted(
                bucket['categories'].items(), key=lambda item: -item[1][0],
            )
        ]
    return buckets, pending_total, pending_count


def _payroll_without_salary_month(branch_id, window):
    """Staff payments recorded as expenses in months that have no salary records yet.

    Once a month's salaries are recorded, its staff payments have been reconciled
    against them (advances are itemized inside the salary), so only months still
    waiting for payroll are flagged.
    """
    rows = Expense.objects.filter(
        is_deleted=False,
        branch_id=branch_id,
        status__in=COUNTED_EXPENSE_STATUSES,
        expense_date__gte=window.date_from,
        expense_date__lte=window.date_to,
        category_reporting_group_snapshot=Group.PAYROLL,
    ).values_list('expense_date', 'amount', 'fee_uzs')
    paid_months = set(
        SalaryPayment.objects.filter(is_deleted=False, branch_id=branch_id)
        .values_list('period_year', 'period_month')
    )
    count, amount = 0, ZERO
    for expense_date, value, fee in rows:
        if (expense_date.year, expense_date.month) not in paid_months:
            count += 1
            amount += value + (fee or ZERO)
    return {'count': count, 'amount': amount}


def _salary_advances(branch_id, window):
    """Advances paid from the safe/bank on salaries that are not paid yet.

    Salaries are paid in two parts: an advance in the middle of the month and
    the rest on payday. The salary itself only counts once it is PAID; until
    then its advances are the part of it that has already left the money, so
    they count when they are paid. Once the salary is PAID it counts in full
    and its advances drop out here, so nothing is counted twice.
    """
    unpaid = SalaryPayment.objects.filter(
        is_deleted=False, branch_id=branch_id,
    ).exclude(status=SalaryPayment.Status.PAID).values('id')
    rows = window.filter(
        TreasuryTransaction.objects.filter(
            is_deleted=False,
            branch_id=branch_id,
            type=TreasuryTransaction.Type.SALARY_PAYMENT,
            reference_type='SalaryPayment',
            reference_id__in=unpaid,
            reversal__isnull=True,
        ),
        'created_at',
    ).aggregate(total=Sum('delta'), count=Count('id'))
    return -(rows['total'] or ZERO), rows['count']


def _unlinked_supplier_purchases(branch_id, window):
    """Supplier-purchase expenses that are not yet attributed to a supplier."""
    return Expense.objects.filter(
        is_deleted=False,
        branch_id=branch_id,
        status__in=COUNTED_EXPENSE_STATUSES,
        expense_date__gte=window.date_from,
        expense_date__lte=window.date_to,
        category_reporting_group_snapshot=Group.INVENTORY_PURCHASE,
        supplier_link__isnull=True,
    ).aggregate(count=Count('id'), amount=Sum('amount'))


def _ledger_overlaps(branch_id, payments):
    """Supplier ledger payments that also appear as a supplier-purchase expense.

    A match is the same amount recorded within a day of the payment, which is how
    a single payment ends up counted twice. Unrelated purchases are not flagged.
    """
    count, total = 0, ZERO
    for payment in payments:
        paid_on = timezone.localtime(payment.paid_at).date()
        duplicate = Expense.objects.filter(
            is_deleted=False,
            branch_id=branch_id,
            status__in=COUNTED_EXPENSE_STATUSES,
            category_reporting_group_snapshot=Group.INVENTORY_PURCHASE,
            amount=payment.principal_uzs,
            expense_date__range=(paid_on - timedelta(days=1), paid_on + timedelta(days=1)),
        ).exists()
        if duplicate:
            count += 1
            total += payment.principal_uzs + payment.fee_uzs
    return count, total


def _sales(branch_id, window):
    orders = window.filter(
        Order.objects.filter(
            is_deleted=False,
            is_paid=True,
            paid_at__isnull=False,
            branch_id=branch_id,
        ),
        'paid_at',
    )
    refunds = window.filter(
        OrderRefund.objects.filter(is_deleted=False, branch_id=branch_id),
        'refunded_at',
    )
    gross = orders.aggregate(total=Sum('total_amount'))['total'] or ZERO
    refunded = refunds.aggregate(total=Sum('amount'))['total'] or ZERO
    return Decimal(gross), Decimal(refunded), orders.count()


def _balances(branch_id):
    accounts = list(TreasuryAccount.objects.filter(is_deleted=False, branch_id=branch_id))
    safe = sum((account.balance for account in accounts if account.kind == TreasuryAccount.Kind.SAFE), ZERO)
    bank = sum((account.balance for account in accounts if account.kind == TreasuryAccount.Kind.BANK), ZERO)
    updated = max((account.last_updated for account in accounts), default=None)
    debt = Supplier.objects.filter(
        is_deleted=False,
        branch_id=branch_id,
        current_balance__gt=0,
    ).aggregate(total=Sum('current_balance'), count=Count('id'))
    return {
        'safe_uzs': uzs_int(safe),
        'bank_uzs': uzs_int(bank),
        'total_uzs': uzs_int(safe + bank),
        'treasury_updated_at': updated.isoformat() if updated else None,
        'supplier_debt_uzs': uzs_int(debt['total'] or ZERO),
        'suppliers_with_debt': debt['count'],
    }


def _warning(code, *, count=None, amount=None):
    warning = {'code': code}
    if count is not None:
        warning['count'] = count
    if amount is not None:
        warning['amount_uzs'] = uzs_int(amount)
    return warning


def get_owner_summary(date_from=None, date_to=None, *, branch_id=None, include_expected=False,
                      **window_kwargs):
    """Recorded money for one window. ``include_expected`` adds the estimate of
    costs the period still carries (owner_expected_costs); it costs a second
    pass over the previous month, so it is off unless a caller asks for it."""
    branch_id = resolve_branch_id(branch_id)
    window = resolve_reporting_window(date_from=date_from, date_to=date_to, **window_kwargs)

    gross, refunded, order_count = _sales(branch_id, window)
    net_sales = gross - refunded
    buckets, pending_total, pending_count = _expense_buckets(branch_id, window)

    ledger_payments = window.filter(
        SupplierPayment.objects.filter(
            branch_id=branch_id,
            status=SupplierPayment.Status.POSTED,
        ),
        'paid_at',
    )
    ledger = ledger_payments.aggregate(total=Sum('principal_uzs'), fees=Sum('fee_uzs'), count=Count('id'))
    ledger_total = (ledger['total'] or ZERO) + (ledger['fees'] or ZERO)

    salaries = window.filter(
        SalaryPayment.objects.filter(
            is_deleted=False,
            branch_id=branch_id,
            status=SalaryPayment.Status.PAID,
            paid_at__isnull=False,
        ),
        'paid_at',
    ).aggregate(total=Sum('net_amount'), count=Count('id'))
    salary_total = salaries['total'] or ZERO
    advance_total, advance_count = _salary_advances(branch_id, window)

    supplier_total = buckets['suppliers']['total'] + ledger_total
    payroll_total = buckets['payroll']['total'] + salary_total + advance_total
    operating_total = buckets['operating']['total']
    raw_profit = net_sales - supplier_total - operating_total - payroll_total
    after_owner = raw_profit - buckets['owner']['total']

    warnings = []
    if not salaries['count']:
        warnings.append(_warning('SALARY_RECORDS_MISSING'))
    unreconciled = _payroll_without_salary_month(branch_id, window) if buckets['payroll']['count'] else None
    if unreconciled and unreconciled['count']:
        warnings.append(_warning(
            'PAYROLL_RECORDED_AS_EXPENSES',
            count=unreconciled['count'], amount=unreconciled['amount'],
        ))
    unlinked = _unlinked_supplier_purchases(branch_id, window) if buckets['suppliers']['count'] else None
    if unlinked and unlinked['count']:
        warnings.append(_warning(
            'SUPPLIER_PURCHASES_RECORDED_AS_EXPENSES',
            count=unlinked['count'], amount=unlinked['amount'],
        ))
    overlap_count, overlap_total = (
        _ledger_overlaps(branch_id, ledger_payments)
        if buckets['suppliers']['count'] and ledger['count'] else (0, ZERO)
    )
    if overlap_count:
        warnings.append(_warning(
            'SUPPLIER_LEDGER_OVERLAP_POSSIBLE', count=overlap_count, amount=overlap_total,
        ))
    if buckets['unclassified']['count']:
        warnings.append(_warning(
            'UNCLASSIFIED_EXPENSES',
            count=buckets['unclassified']['count'], amount=buckets['unclassified']['total'],
        ))
    if pending_count:
        warnings.append(_warning(
            'EXPENSES_NOT_PAID_THROUGH_TREASURY', count=pending_count, amount=pending_total,
        ))

    expected = None
    if include_expected:
        from admins.services.owner_expected_costs import expected_costs, reference_month
        from base.services.business_day import business_date

        ref_from, ref_to = reference_month(window)
        reference = get_owner_summary(ref_from, ref_to, branch_id=branch_id, include_expected=False)
        expected = expected_costs(
            branch_id, window,
            net_sales=net_sales, supplier_total=supplier_total, payroll_total=payroll_total,
            raw_profit=raw_profit, owner_withdrawals=buckets['owner']['total'],
            reference_summary=reference, today=business_date(),
        )

    def bucket_payload(name):
        bucket = buckets[name]
        return {
            'total_uzs': uzs_int(bucket['total']),
            'count': bucket['count'],
            'categories': bucket['categories'],
        }

    return {
        'branch_id': branch_id,
        'range': window.metadata(),
        'sales': {
            'gross_sales_uzs': uzs_int(gross),
            'refunds_uzs': uzs_int(refunded),
            'net_sales_uzs': uzs_int(net_sales),
            'paid_orders': order_count,
        },
        'costs': {
            'suppliers': {
                **bucket_payload('suppliers'),
                'total_uzs': uzs_int(supplier_total),
                'from_expenses_uzs': uzs_int(buckets['suppliers']['total']),
                'from_supplier_payments_uzs': uzs_int(ledger_total),
                'supplier_payment_count': ledger['count'],
            },
            'operating': bucket_payload('operating'),
            'payroll': {
                **bucket_payload('payroll'),
                'total_uzs': uzs_int(payroll_total),
                'from_expenses_uzs': uzs_int(buckets['payroll']['total']),
                'from_salary_payments_uzs': uzs_int(salary_total),
                'salary_payment_count': salaries['count'],
                'from_salary_advances_uzs': uzs_int(advance_total),
                'salary_advance_count': advance_count,
            },
            'total_uzs': uzs_int(supplier_total + operating_total + payroll_total),
        },
        'outside_profit': {
            'owner_withdrawals': bucket_payload('owner'),
            'capital_expenditure': bucket_payload('capital'),
            'unclassified': bucket_payload('unclassified'),
        },
        'profit': {
            'raw_profit_uzs': uzs_int(raw_profit),
            'raw_margin_pct': (
                str((raw_profit / net_sales * 100).quantize(Decimal('0.1')))
                if net_sales else None
            ),
            'after_owner_withdrawals_uzs': uzs_int(after_owner),
        },
        'balances': _balances(branch_id),
        'expected': expected,
        'warnings': warnings,
        'generated_at': timezone.now().isoformat(),
    }
