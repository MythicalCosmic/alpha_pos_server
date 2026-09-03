"""Canonical branch-scoped restaurant P&L and close workflow.

Open reports are deliberately provisional. A final result only comes from an
append-only period close after all evidence gates pass.
"""

import calendar
import copy
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from admins.models import (
    CashboxExpenseClassification,
    ProductCostProfile,
    ProfitAdjustment,
    ProfitPeriodClose,
    ProfitabilityConfiguration,
    RecurringCost,
)
from base.financial import (
    EXPENSE_REPORTING_GROUPS,
    FinancialReportingGroup,
    PROFIT_EXPENSE_GROUPS,
)
from base.models import Order, OrderItem, OrderRefund, Product
from base.services.business_day import business_date, resolve_reporting_window
from base.services.refund_lines import (
    REFUND_EVENT_ALIAS,
    refund_item_events,
    refund_line_revenue,
)
from base.services.revenue import net_line_revenue
from base.services.tender import breakdown_for_orders, breakdown_for_refunds
from cashbox.models import CashboxExpense, CashboxExpenseCategory
from hr.models import CashTransaction, Expense, ExpenseCategory, SalaryPayment
from stock.models import StockTransaction, SupplierTransaction


ZERO = Decimal('0')
MONEY_QUANTUM = Decimal('0.01')
RATE_QUANTUM = Decimal('0.01')
DEFAULT_START_DATE = date(2026, 8, 15)

EXPENSE_GROUP_ORDER = (
    FinancialReportingGroup.PAYROLL,
    FinancialReportingGroup.RENT,
    FinancialReportingGroup.UTILITIES,
    FinancialReportingGroup.OPERATING,
    FinancialReportingGroup.WASTE_SPOILAGE,
    FinancialReportingGroup.FINANCE_FEES,
    FinancialReportingGroup.DEPRECIATION,
    FinancialReportingGroup.TAXES,
)


class ProfitabilityError(ValueError):
    """Validated input or accounting-state problem safe to return to an admin."""

    def __init__(self, message, *, errors=None, report=None):
        super().__init__(message)
        self.errors = errors or {}
        self.report = report


def _decimal(value, *, field='amount', positive=False, places='0.01'):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ProfitabilityError(
            f'{field} must be a valid number', errors={field: 'Invalid number'},
        ) from None
    if not result.is_finite() or (positive and result <= ZERO):
        raise ProfitabilityError(
            f'{field} must be greater than zero',
            errors={field: 'Must be greater than zero'},
        )
    quantum = Decimal(places)
    rounded = result.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded != result:
        raise ProfitabilityError(
            f'{field} has too many decimal places',
            errors={field: f'Maximum precision is {abs(quantum.as_tuple().exponent)} decimals'},
        )
    return rounded


def _date(value, field, *, required=True):
    if isinstance(value, date):
        return value
    if value is None or str(value).strip() == '':
        if not required:
            return None
        raise ProfitabilityError(
            f'{field} must be YYYY-MM-DD', errors={field: 'Invalid date'},
        )
    try:
        parsed = parse_date(str(value).strip())
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        raise ProfitabilityError(
            f'{field} must be YYYY-MM-DD', errors={field: 'Invalid date'},
        )
    return parsed


def _positive_int(value, field, *, default, maximum=None):
    if value in (None, ''):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    if parsed < 1 or (maximum is not None and parsed > maximum):
        message = 'Must be a positive integer'
        if maximum is not None:
            message = f'Must be between 1 and {maximum}'
        raise ProfitabilityError(
            f'{field} is invalid', errors={field: message},
        )
    return parsed


def _boolean(value, field):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {'true', '1'}:
            return True
        if normalized in {'false', '0'}:
            return False
    raise ProfitabilityError(
        f'{field} must be a boolean', errors={field: 'Use true or false'},
    )


def _money(value):
    return Decimal(value or ZERO).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _money_string(value):
    return f'{_money(value):.2f}'


def _clean_model(instance, message):
    try:
        instance.full_clean()
    except ValidationError as exc:
        errors = getattr(exc, 'message_dict', None) or {
            'non_field_errors': exc.messages,
        }
        raise ProfitabilityError(message, errors=errors) from None


def _rate_string(numerator, denominator):
    denominator = Decimal(denominator or ZERO)
    if denominator == ZERO:
        return '0.00'
    rate = (Decimal(numerator or ZERO) * Decimal('100') / denominator)
    return f'{rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP):.2f}'


def resolve_branch_id(branch_id=None):
    explicit = str(branch_id or '').strip()
    if explicit and explicit.lower() != 'cloud':
        return explicit
    configured = str(
        getattr(settings, 'CLOUD_DEFAULT_TARGET_BRANCH_ID', '') or ''
    ).strip()
    if configured and configured.lower() != 'cloud':
        return configured
    candidates = list(
        Order.objects.filter(is_deleted=False)
        .exclude(branch_id='')
        .exclude(branch_id__iexact='cloud')
        .values_list('branch_id', flat=True)
        .distinct()[:2]
    )
    if len(candidates) == 1:
        return candidates[0]
    raise ProfitabilityError(
        'A branch_id is required for profitability reporting',
        errors={'branch_id': 'Select one branch'},
    )


def _configuration(branch_id):
    saved = ProfitabilityConfiguration.objects.filter(branch_id=branch_id).first()
    if saved:
        return saved, True
    return ProfitabilityConfiguration(
        branch_id=branch_id,
        reporting_start_date=DEFAULT_START_DATE,
    ), False


def serialize_configuration(config, *, saved=True):
    return {
        'saved': saved,
        'branch_id': config.branch_id,
        'reporting_start_date': config.reporting_start_date.isoformat(),
        'payroll_confirmed_through': (
            config.payroll_confirmed_through.isoformat()
            if config.payroll_confirmed_through else None
        ),
        'fixed_costs_confirmed_through': (
            config.fixed_costs_confirmed_through.isoformat()
            if config.fixed_costs_confirmed_through else None
        ),
        'updated_at': config.updated_at.isoformat() if config.pk else None,
    }


@transaction.atomic
def update_configuration(branch_id, payload, actor):
    branch_id = resolve_branch_id(branch_id)
    config, _created = ProfitabilityConfiguration.objects.select_for_update().get_or_create(
        branch_id=branch_id,
        defaults={'reporting_start_date': DEFAULT_START_DATE},
    )
    if 'reporting_start_date' in payload:
        start = _date(payload.get('reporting_start_date'), 'reporting_start_date')
        if ProfitPeriodClose.objects.filter(branch_id=branch_id).exists():
            if start != config.reporting_start_date:
                raise ProfitabilityError(
                    'Reporting start cannot change after a period is closed',
                    errors={'reporting_start_date': 'A closed period already exists'},
                )
        config.reporting_start_date = start
    for field in ('payroll_confirmed_through', 'fixed_costs_confirmed_through'):
        if field in payload:
            config.__setattr__(field, _date(payload.get(field), field, required=False))
    config.updated_by = actor
    config.save()
    return serialize_configuration(config)


def _effective_window(date_from=None, date_to=None, *, branch_id, **window_kwargs):
    requested = resolve_reporting_window(
        date_from=date_from,
        date_to=date_to,
        **window_kwargs,
    )
    config, saved = _configuration(branch_id)
    if requested.date_to < config.reporting_start_date:
        return requested, None, config, saved
    if requested.mode == 'custom' and requested.date_from < config.reporting_start_date:
        raise ProfitabilityError(
            'Custom profitability windows cannot cross the reporting start date',
            errors={'range': f'Reporting starts {config.reporting_start_date.isoformat()}'},
        )
    effective_from = max(requested.date_from, config.reporting_start_date)
    effective = resolve_reporting_window(
        date_from=effective_from,
        date_to=requested.date_to,
    )
    return requested, effective, config, saved


def _profile_for(profiles, on_date):
    for profile in profiles:
        if profile.effective_from > on_date:
            continue
        if profile.effective_to and profile.effective_to < on_date:
            continue
        return profile
    return None


def _prorated_monthly(amount, schedule_start, schedule_end, report_start, report_end):
    lo = max(schedule_start, report_start)
    hi = min(schedule_end or report_end, report_end)
    if hi < lo:
        return ZERO
    total = ZERO
    cursor = date(lo.year, lo.month, 1)
    final_month = date(hi.year, hi.month, 1)
    while cursor <= final_month:
        days_in_month = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = cursor.replace(day=days_in_month)
        overlap_start = max(lo, cursor)
        overlap_end = min(hi, month_end)
        if overlap_end >= overlap_start:
            overlap_days = Decimal((overlap_end - overlap_start).days + 1)
            total += Decimal(amount) * overlap_days / Decimal(days_in_month)
        cursor = month_end + timedelta(days=1)
    return _money(total)


def _add_blocker(blockers, code, message, *, count=None, amount=None):
    row = {'code': code, 'message': message}
    if count is not None:
        row['count'] = int(count)
    if amount is not None:
        row['amount'] = _money_string(amount)
    blockers.append(row)


def _not_started_report(requested, config, saved, branch_id):
    blockers = []
    _add_blocker(
        blockers,
        'REPORTING_NOT_STARTED',
        f'Profitability reporting starts {config.reporting_start_date.isoformat()}.',
    )
    return {
        'status': 'NOT_STARTED',
        'branch_id': branch_id,
        'range': requested.metadata(
            effective_from=None,
            effective_to=None,
            launch_clamped=False,
            reporting_start_date=config.reporting_start_date.isoformat(),
        ),
        'configuration': serialize_configuration(config, saved=saved),
        'summary': {
            key: '0.00' for key in (
                'gross_sales', 'refunds', 'net_sales', 'cogs', 'gross_profit',
                'payroll', 'rent', 'utilities', 'operating_expenses',
                'waste_spoilage', 'finance_fees', 'depreciation', 'taxes',
                'total_operating_expenses', 'other_income', 'net_profit',
                'net_margin_pct',
            )
        },
        'cash_flow': {
            'known_inflows': '0.00', 'known_outflows': '0.00',
            'known_net_movement': '0.00',
        },
        'breakdown': {'expenses': [], 'cost_sources': []},
        'coverage': {
            'costed_revenue_pct': '0.00',
            'can_close': False,
            'blockers': blockers,
            'missing_cost_products': [],
        },
        'generated_at': timezone.now().isoformat(),
    }


def _closed_snapshot(branch_id, effective, *, live):
    if live:
        return None
    closed = ProfitPeriodClose.objects.filter(
        branch_id=branch_id,
        period_start=effective.date_from,
        period_end=effective.date_to,
    ).order_by('-revision').first()
    if not closed:
        return None
    snapshot = copy.deepcopy(closed.report_snapshot)
    snapshot['status'] = 'FINAL'
    snapshot['close'] = {
        'id': closed.id,
        'revision': closed.revision,
        'closed_at': closed.closed_at.isoformat(),
        'correction_reason': closed.correction_reason,
    }
    return snapshot


def profitability_report(
    date_from=None,
    date_to=None,
    *,
    branch_id=None,
    datetime_from=None,
    datetime_to=None,
    from_at=None,
    to_at=None,
    tod_from=None,
    tod_to=None,
    live=False,
):
    branch_id = resolve_branch_id(branch_id)
    requested, window, config, config_saved = _effective_window(
        date_from,
        date_to,
        branch_id=branch_id,
        datetime_from=datetime_from,
        datetime_to=datetime_to,
        from_at=from_at,
        to_at=to_at,
        tod_from=tod_from,
        tod_to=tod_to,
    )
    if window is None:
        return _not_started_report(requested, config, config_saved, branch_id)
    final = _closed_snapshot(branch_id, window, live=live)
    if final is not None:
        return final

    sale_orders = window.filter(
        Order.objects.filter(
            is_deleted=False,
            is_paid=True,
            paid_at__isnull=False,
            branch_id=branch_id,
        ),
        'paid_at',
    )
    refund_events = window.filter(
        OrderRefund.objects.filter(is_deleted=False, branch_id=branch_id),
        'refunded_at',
    )

    gross_sales = sale_orders.aggregate(total=Sum('total_amount'))['total'] or ZERO
    refund_total = refund_events.aggregate(total=Sum('amount'))['total'] or ZERO
    net_sales = Decimal(gross_sales) - Decimal(refund_total)
    sale_split, sale_card_detail = breakdown_for_orders(sale_orders)
    refund_split, refund_card_detail = breakdown_for_refunds(refund_events)

    sale_items = list(
        OrderItem.objects.filter(
            is_deleted=False,
            order__in=sale_orders,
        )
        .select_related('product', 'order')
        .annotate(reporting_revenue=net_line_revenue())
        .order_by('order_id', 'id')
    )
    product_ids = {item.product_id for item in sale_items}
    profiles_by_product = defaultdict(list)
    for profile in ProductCostProfile.objects.filter(
        branch_id=branch_id,
        product_id__in=product_ids,
    ).select_related('product').order_by('product_id', '-effective_from'):
        profiles_by_product[profile.product_id].append(profile)

    item_ids = [item.id for item in sale_items]
    actual_costs = {
        row['order_item_id']: Decimal(row['total'] or ZERO)
        for row in StockTransaction.objects.filter(
            is_deleted=False,
            movement_type=StockTransaction.MovementType.SALE_OUT,
            order_item_id__in=item_ids,
        ).values('order_item_id').annotate(total=Sum('total_cost'))
    }
    non_refund_returns = StockTransaction.objects.filter(
        is_deleted=False,
        movement_type=StockTransaction.MovementType.RETURN_FROM_CUSTOMER,
        order_item_id__in=item_ids,
    ).exclude(reference_type='ORDER_REVERSAL')
    for row in non_refund_returns.values('order_item_id').annotate(
        total=Sum('total_cost'),
    ):
        item_id = row['order_item_id']
        actual_costs[item_id] = actual_costs.get(item_id, ZERO) - Decimal(
            row['total'] or ZERO
        )

    cost_by_source = defaultdict(Decimal)
    revenue_by_source = defaultdict(Decimal)
    line_count_by_source = defaultdict(int)
    missing_products = {}
    zero_actual_cost_rows = 0
    total_product_revenue = ZERO
    sale_cogs = ZERO

    for item in sale_items:
        line_revenue = Decimal(item.reporting_revenue or ZERO)
        total_product_revenue += line_revenue
        actual = actual_costs.get(item.id)
        source = None
        cost = ZERO
        if actual is not None and actual > ZERO:
            source = 'ACTUAL_STOCK'
            cost = actual
        else:
            if actual is not None:
                zero_actual_cost_rows += 1
            paid_date = timezone.localtime(item.order.paid_at).date()
            profile = _profile_for(profiles_by_product[item.product_id], paid_date)
            if profile and profile.verified_at:
                if profile.treatment == ProductCostProfile.Treatment.ZERO:
                    source = 'EXPLICIT_ZERO'
                elif (
                    profile.treatment == ProductCostProfile.Treatment.STANDARD
                    and profile.standard_unit_cost is not None
                    and profile.standard_unit_cost > ZERO
                ):
                    source = 'VERIFIED_STANDARD'
                    cost = Decimal(profile.standard_unit_cost) * Decimal(item.quantity)
        if source is None:
            source = 'MISSING'
            target = missing_products.setdefault(item.product_id, {
                'product_id': item.product_id,
                'product_name': item.product.name,
                'quantity': 0,
                'revenue': ZERO,
            })
            target['quantity'] += item.quantity
            target['revenue'] += line_revenue
        else:
            sale_cogs += cost
        cost_by_source[source] += cost
        revenue_by_source[source] += line_revenue
        line_count_by_source[source] += 1

    refunded_product_revenue = refund_item_events(
        OrderItem.objects.all(), pk__in=refund_events,
    ).aggregate(
        total=Sum(refund_line_revenue(REFUND_EVENT_ALIAS)),
    )['total'] or ZERO
    net_product_revenue = (
        Decimal(total_product_revenue) - Decimal(refunded_product_revenue)
    )

    refund_order_ids = list(refund_events.values_list('order_id', flat=True))
    refunded_actual_items = set(
        StockTransaction.objects.filter(
            is_deleted=False,
            movement_type=StockTransaction.MovementType.SALE_OUT,
            order_id__in=refund_order_ids,
            order_item_id__isnull=False,
            total_cost__gt=0,
        ).values_list('order_item_id', flat=True)
    )
    returned_cost = ZERO
    if refunded_actual_items:
        returned_cost = window.filter(
            StockTransaction.objects.filter(
                is_deleted=False,
                movement_type=StockTransaction.MovementType.RETURN_FROM_CUSTOMER,
                reference_type='ORDER_REVERSAL',
                order_id__in=refund_order_ids,
                order_item_id__in=refunded_actual_items,
            ),
            'created_at',
        ).aggregate(total=Sum('total_cost'))['total'] or ZERO
    cogs = Decimal(sale_cogs) - Decimal(returned_cost)

    unstructured_stock_count = StockTransaction.objects.filter(
        is_deleted=False,
        movement_type=StockTransaction.MovementType.SALE_OUT,
        order_id__in=sale_orders.values('id'),
        order_item_id__isnull=True,
    ).count()

    expense_groups = {group: ZERO for group in EXPENSE_GROUP_ORDER}
    unclassified_cashbox = []
    cashbox_rows = list(
        window.filter(
            CashboxExpense.objects.filter(
                is_deleted=False,
            ).filter(Q(branch_id=branch_id) | Q(shift__branch_id=branch_id)),
            'created_at',
        ).select_related('category')
    )
    overrides = {
        row.expense_id: row
        for row in CashboxExpenseClassification.objects.filter(
            expense_id__in=[expense.id for expense in cashbox_rows]
        )
    }
    for expense in cashbox_rows:
        if expense.canonical_expense_id:
            continue
        override = overrides.get(expense.id)
        group = (
            override.reporting_group if override
            else expense.category.reporting_group if expense.category_id
            else FinancialReportingGroup.REVIEW
        )
        if override and override.represented_elsewhere:
            continue
        if group == FinancialReportingGroup.REVIEW or group in PROFIT_EXPENSE_GROUPS:
            unclassified_cashbox.append(expense)

    period_expenses = Expense.objects.filter(
        is_deleted=False,
        branch_id=branch_id,
        expense_date__gte=window.date_from,
        expense_date__lte=window.date_to,
    ).select_related('category')
    pending_hr = period_expenses.filter(status=Expense.Status.PENDING)
    paid_expenses = window.filter(
        Expense.objects.filter(
            is_deleted=False,
            branch_id=branch_id,
            status=Expense.Status.PAID,
            paid_at__isnull=False,
        ).select_related('category'),
        'paid_at',
    )
    unclassified_hr = []
    for expense in paid_expenses:
        group = (
            expense.category.reporting_group
            if expense.category_id else FinancialReportingGroup.REVIEW
        )
        if group == FinancialReportingGroup.REVIEW:
            unclassified_hr.append(expense)
        elif group in expense_groups:
            expense_groups[group] += (
                Decimal(expense.amount) + Decimal(expense.fee_uzs or ZERO)
            )

    salary_period = (
        (
            Q(period_year__gt=window.date_from.year)
            | Q(
                period_year=window.date_from.year,
                period_month__gte=window.date_from.month,
            )
        )
        & (
            Q(period_year__lt=window.date_to.year)
            | Q(
                period_year=window.date_to.year,
                period_month__lte=window.date_to.month,
            )
        )
    )
    salary_rows = SalaryPayment.objects.filter(
        salary_period,
        is_deleted=False,
        branch_id=branch_id,
        status__in=[SalaryPayment.Status.APPROVED, SalaryPayment.Status.PAID],
    )
    payroll = ZERO
    for salary in salary_rows:
        month_start = date(salary.period_year, salary.period_month, 1)
        month_end = month_start.replace(
            day=calendar.monthrange(salary.period_year, salary.period_month)[1]
        )
        payroll += _prorated_monthly(
            salary.net_amount, month_start, month_end,
            window.date_from, window.date_to,
        )
    expense_groups[FinancialReportingGroup.PAYROLL] += payroll
    pending_salary_count = SalaryPayment.objects.filter(
        salary_period,
        is_deleted=False,
        branch_id=branch_id,
        status=SalaryPayment.Status.PENDING,
    ).count()

    recurring_rows = RecurringCost.objects.filter(
        branch_id=branch_id,
        is_active=True,
        start_date__lte=window.date_to,
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=window.date_from))
    for schedule in recurring_rows:
        if schedule.reporting_group in expense_groups:
            expense_groups[schedule.reporting_group] += _prorated_monthly(
                schedule.monthly_amount,
                schedule.start_date,
                schedule.end_date,
                window.date_from,
                window.date_to,
            )

    waste_rows = window.filter(
        StockTransaction.objects.filter(
            is_deleted=False,
            branch_id=branch_id,
            movement_type__in=[
                StockTransaction.MovementType.WASTE,
                StockTransaction.MovementType.SPOILAGE,
            ],
            reversal_of__isnull=True,
        ),
        'created_at',
    )
    reversed_waste = window.filter(
        StockTransaction.objects.filter(
            is_deleted=False,
            branch_id=branch_id,
            reversal_of__movement_type__in=[
                StockTransaction.MovementType.WASTE,
                StockTransaction.MovementType.SPOILAGE,
            ],
        ),
        'created_at',
    ).aggregate(total=Sum('total_cost'))['total'] or ZERO
    waste_cost = (
        (waste_rows.aggregate(total=Sum('total_cost'))['total'] or ZERO)
        - reversed_waste
    )
    expense_groups[FinancialReportingGroup.WASTE_SPOILAGE] += Decimal(waste_cost)

    adjustments = ProfitAdjustment.objects.filter(
        branch_id=branch_id,
        effective_date__gte=window.date_from,
        effective_date__lte=window.date_to,
    )
    other_income = ZERO
    for adjustment in adjustments.filter(status=ProfitAdjustment.Status.APPROVED):
        if adjustment.direction == ProfitAdjustment.Direction.INCOME:
            other_income += Decimal(adjustment.amount)
        elif adjustment.reporting_group in expense_groups:
            expense_groups[adjustment.reporting_group] += Decimal(adjustment.amount)
    draft_adjustments = adjustments.filter(status=ProfitAdjustment.Status.DRAFT)

    gross_profit = net_sales - cogs
    total_operating_expenses = sum(expense_groups.values(), ZERO)
    net_profit = gross_profit - total_operating_expenses + other_income

    cashbox_outflow = sum(
        (
            Decimal(row.amount)
            for row in cashbox_rows
            if not (
                overrides.get(row.id)
                and overrides[row.id].cash_movement_represented_elsewhere
            )
        ),
        ZERO,
    )
    cash_transactions = window.filter(
        CashTransaction.objects.filter(is_deleted=False, branch_id=branch_id),
        'created_at',
    )
    cash_ledger_inflow = cash_transactions.filter(
        type=CashTransaction.TransactionType.DEPOSIT,
    ).aggregate(total=Sum('amount'))['total'] or ZERO
    cash_ledger_outflow = cash_transactions.filter(type__in=[
        CashTransaction.TransactionType.WITHDRAWAL,
        CashTransaction.TransactionType.EXPENSE_PAYMENT,
        CashTransaction.TransactionType.SALARY_PAYMENT,
    ]).aggregate(total=Sum('amount'))['total'] or ZERO
    supplier_outflow_row = window.filter(
        SupplierTransaction.objects.filter(
            is_deleted=False,
            branch_id=branch_id,
            type=SupplierTransaction.Type.PAYMENT,
        ).exclude(source_account=SupplierTransaction.SourceAccount.DRAWER),
        'created_at',
    ).aggregate(amount=Sum('amount'), fees=Sum('fee'))
    supplier_outflow = Decimal(supplier_outflow_row['amount'] or ZERO) + Decimal(
        supplier_outflow_row['fees'] or ZERO
    )
    tender_inflow = sum((Decimal(value) for value in sale_split.values()), ZERO)
    tender_refund_outflow = sum(
        (Decimal(value) for value in refund_split.values()), ZERO
    )
    known_inflows = tender_inflow + Decimal(cash_ledger_inflow)
    known_outflows = (
        tender_refund_outflow + cashbox_outflow
        + Decimal(cash_ledger_outflow) + supplier_outflow
    )

    missing_revenue = revenue_by_source['MISSING']
    costed_revenue = total_product_revenue - missing_revenue
    blockers = []
    if not config_saved:
        _add_blocker(
            blockers, 'CONFIGURATION_NOT_SAVED',
            'Save the reporting start and confirmation settings.',
        )
    if missing_products:
        _add_blocker(
            blockers, 'MISSING_PRODUCT_COSTS',
            'Every sold product needs actual cost, a verified standard cost, or explicit zero COGS.',
            count=len(missing_products), amount=missing_revenue,
        )
    if unstructured_stock_count:
        _add_blocker(
            blockers, 'UNATTRIBUTED_STOCK_COST',
            'Some sale stock movements are not linked to an exact order line.',
            count=unstructured_stock_count,
        )
    if zero_actual_cost_rows:
        _add_blocker(
            blockers, 'ZERO_VALUE_STOCK_COST',
            'Some stock deductions have zero recorded cost and used fallback costing.',
            count=zero_actual_cost_rows,
        )
    unclassified_cashbox_amount = sum(
        (Decimal(row.amount) for row in unclassified_cashbox), ZERO
    )
    if unclassified_cashbox:
        _add_blocker(
            blockers, 'NONCANONICAL_CASHBOX_EXPENSES',
            'Link operating drawer payouts to a canonical paid expense.',
            count=len(unclassified_cashbox), amount=unclassified_cashbox_amount,
        )
    unclassified_hr_amount = sum(
        (Decimal(row.amount) for row in unclassified_hr), ZERO
    )
    if unclassified_hr:
        _add_blocker(
            blockers, 'UNCLASSIFIED_HR_EXPENSES',
            'Approved HR expenses require a financial reporting group.',
            count=len(unclassified_hr), amount=unclassified_hr_amount,
        )
    pending_hr_amount = pending_hr.aggregate(total=Sum('amount'))['total'] or ZERO
    if pending_hr.exists():
        _add_blocker(
            blockers, 'PENDING_HR_EXPENSES',
            'Approve or reject pending expenses before closing.',
            count=pending_hr.count(), amount=pending_hr_amount,
        )
    if pending_salary_count:
        _add_blocker(
            blockers, 'PENDING_PAYROLL',
            'Approve payroll records for the reporting period.',
            count=pending_salary_count,
        )
    if draft_adjustments.exists():
        _add_blocker(
            blockers, 'DRAFT_ADJUSTMENTS',
            'Approve or remove draft profit adjustments.',
            count=draft_adjustments.count(),
        )
    payroll_confirmed = bool(
        config.payroll_confirmed_through
        and config.payroll_confirmed_through >= window.date_to
    )
    if not payroll_confirmed:
        _add_blocker(
            blockers, 'PAYROLL_NOT_CONFIRMED',
            'Confirm payroll evidence through the period end.',
        )
    fixed_costs_confirmed = bool(
        config.fixed_costs_confirmed_through
        and config.fixed_costs_confirmed_through >= window.date_to
    )
    if not fixed_costs_confirmed:
        _add_blocker(
            blockers, 'FIXED_COSTS_NOT_CONFIRMED',
            'Confirm rent, utilities, and recurring costs through the period end.',
        )
    unknown_tender = Decimal(sale_split['unknown']) + Decimal(refund_split['unknown'])
    if unknown_tender:
        _add_blocker(
            blockers, 'UNKNOWN_TENDER',
            'Unattributed payment evidence must be repaired before closing.',
            amount=unknown_tender,
        )

    missing_rows = sorted(
        (
            {
                **row,
                'revenue': _money_string(row['revenue']),
            }
            for row in missing_products.values()
        ),
        key=lambda row: Decimal(row['revenue']),
        reverse=True,
    )
    report = {
        'status': 'PROVISIONAL',
        'branch_id': branch_id,
        'range': window.metadata(
            requested_from=requested.date_from.isoformat(),
            requested_to=requested.date_to.isoformat(),
            effective_from=window.date_from.isoformat(),
            effective_to=window.date_to.isoformat(),
            launch_clamped=requested.date_from != window.date_from,
            reporting_start_date=config.reporting_start_date.isoformat(),
            partial_launch_period=(
                window.date_from == config.reporting_start_date
                and config.reporting_start_date.day != 1
            ),
        ),
        'configuration': serialize_configuration(config, saved=config_saved),
        'summary': {
            'gross_sales': _money_string(gross_sales),
            'refunds': _money_string(refund_total),
            'net_sales': _money_string(net_sales),
            'cogs': _money_string(cogs),
            'gross_profit': _money_string(gross_profit),
            'payroll': _money_string(expense_groups[FinancialReportingGroup.PAYROLL]),
            'rent': _money_string(expense_groups[FinancialReportingGroup.RENT]),
            'utilities': _money_string(expense_groups[FinancialReportingGroup.UTILITIES]),
            'operating_expenses': _money_string(expense_groups[FinancialReportingGroup.OPERATING]),
            'waste_spoilage': _money_string(expense_groups[FinancialReportingGroup.WASTE_SPOILAGE]),
            'finance_fees': _money_string(expense_groups[FinancialReportingGroup.FINANCE_FEES]),
            'depreciation': _money_string(expense_groups[FinancialReportingGroup.DEPRECIATION]),
            'taxes': _money_string(expense_groups[FinancialReportingGroup.TAXES]),
            'total_operating_expenses': _money_string(total_operating_expenses),
            'other_income': _money_string(other_income),
            'net_profit': _money_string(net_profit),
            'net_margin_pct': _rate_string(net_profit, net_sales),
        },
        'cash_flow': {
            'known_inflows': _money_string(known_inflows),
            'known_outflows': _money_string(known_outflows),
            'known_net_movement': _money_string(known_inflows - known_outflows),
            'sales_by_tender': {
                key: _money_string(value) for key, value in sale_split.items()
            },
            'refunds_by_tender': {
                key: _money_string(value) for key, value in refund_split.items()
            },
            'card_detail': {
                'sales': {
                    key: _money_string(value)
                    for key, value in sale_card_detail.items()
                },
                'refunds': {
                    key: _money_string(value)
                    for key, value in refund_card_detail.items()
                },
            },
            'drawer_payouts': _money_string(cashbox_outflow),
            'hr_cash_ledger_outflows': _money_string(cash_ledger_outflow),
            'supplier_payment_outflows': _money_string(supplier_outflow),
            'other_cash_ledger_inflows': _money_string(cash_ledger_inflow),
            'scope_note': (
                'Known synced account movement only; local-only treasury events '
                'are not represented on the cloud.'
            ),
        },
        'breakdown': {
            'expenses': [
                {
                    'group': group,
                    'label': FinancialReportingGroup(group).label,
                    'amount': _money_string(expense_groups[group]),
                }
                for group in EXPENSE_GROUP_ORDER
            ],
            'cost_sources': [
                {
                    'source': source,
                    'line_count': line_count_by_source[source],
                    'revenue': _money_string(revenue_by_source[source]),
                    'cost': _money_string(cost_by_source[source]),
                }
                for source in (
                    'ACTUAL_STOCK', 'VERIFIED_STANDARD', 'EXPLICIT_ZERO', 'MISSING'
                )
            ],
            'actual_inventory_return_credit': _money_string(returned_cost),
            'product_revenue': _money_string(net_product_revenue),
            'product_refunds': _money_string(refunded_product_revenue),
            'non_product_revenue': _money_string(net_sales - net_product_revenue),
            'orders': sale_orders.count(),
            'refund_events': refund_events.count(),
        },
        'coverage': {
            'costed_revenue_pct': _rate_string(costed_revenue, total_product_revenue),
            'actual_cost_revenue_pct': _rate_string(
                revenue_by_source['ACTUAL_STOCK'], total_product_revenue,
            ),
            'standard_cost_revenue_pct': _rate_string(
                revenue_by_source['VERIFIED_STANDARD'], total_product_revenue,
            ),
            'zero_cogs_revenue_pct': _rate_string(
                revenue_by_source['EXPLICIT_ZERO'], total_product_revenue,
            ),
            'missing_cost_revenue': _money_string(missing_revenue),
            'missing_cost_products': missing_rows,
            'unclassified_cashbox_count': len(unclassified_cashbox),
            'unclassified_cashbox_amount': _money_string(unclassified_cashbox_amount),
            'unclassified_hr_count': len(unclassified_hr),
            'pending_expense_count': pending_hr.count(),
            'payroll_confirmed': payroll_confirmed,
            'fixed_costs_confirmed': fixed_costs_confirmed,
            'tender_attribution_complete': unknown_tender == ZERO,
            'can_close': not blockers,
            'blockers': blockers,
        },
        'source_policy': {
            'sales_clock': 'Order.paid_at',
            'refund_clock': 'OrderRefund.refunded_at',
            'food_cost': 'Structured stock movement, then verified effective standard cost',
            'payroll': 'SalaryPayment work month, prorated by calendar day',
            'recurring_costs': 'Monthly accrual, prorated by calendar day',
            'purchases': 'Inventory/cash movement; excluded from immediate P&L expense',
        },
        'generated_at': timezone.now().isoformat(),
    }
    return report


def _digest_report(report):
    stable = {
        key: report[key]
        for key in ('branch_id', 'range', 'summary', 'cash_flow', 'breakdown', 'coverage')
    }
    stable['range'] = {
        key: value for key, value in stable['range'].items()
        if key not in {'start_at', 'end_at'}
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def _use_repeatable_read_snapshot():
    """Pin every close query to one PostgreSQL evidence snapshot."""
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')


@transaction.atomic
def close_profit_period(branch_id, period, actor, *, correction_reason=''):
    # This must be the first database statement in the atomic block. Under
    # PostgreSQL READ COMMITTED, each aggregate would otherwise be allowed to
    # observe a different set of concurrently synced orders and refunds.
    _use_repeatable_read_snapshot()
    branch_id = resolve_branch_id(branch_id)
    try:
        year_text, month_text = str(period or '').split('-', 1)
        year, month = int(year_text), int(month_text)
        month_start = date(year, month, 1)
    except (TypeError, ValueError):
        raise ProfitabilityError(
            'period must be YYYY-MM', errors={'period': 'Invalid month'},
        ) from None
    month_end = month_start.replace(day=calendar.monthrange(year, month)[1])
    config = ProfitabilityConfiguration.objects.select_for_update().filter(
        branch_id=branch_id,
    ).first()
    if not config:
        raise ProfitabilityError('Save profitability configuration before closing')
    period_start = max(month_start, config.reporting_start_date)
    if period_start > month_end:
        raise ProfitabilityError('The selected month is before profitability reporting began')
    if month_end >= business_date():
        raise ProfitabilityError('Only a completed calendar month can be closed')

    report = profitability_report(
        period_start,
        month_end,
        branch_id=branch_id,
        live=True,
    )
    if not report['coverage']['can_close']:
        raise ProfitabilityError(
            'The period still has unresolved accounting evidence',
            errors={'blockers': report['coverage']['blockers']},
            report=report,
        )
    digest = _digest_report(report)
    existing = list(
        ProfitPeriodClose.objects.select_for_update().filter(
            branch_id=branch_id,
            period_start=period_start,
            period_end=month_end,
        ).order_by('-revision')
    )
    latest = existing[0] if existing else None
    if latest and latest.source_digest == digest:
        return latest, False
    correction_reason = str(correction_reason or '').strip()
    if latest and not correction_reason:
        raise ProfitabilityError(
            'A correction reason is required because source evidence changed',
            errors={'correction_reason': 'Required for a new revision'},
        )
    revision = latest.revision + 1 if latest else 1
    snapshot = copy.deepcopy(report)
    snapshot['status'] = 'FINAL'
    snapshot['close'] = {
        'revision': revision,
        'correction_reason': correction_reason,
        'closed_by': actor.id,
    }
    closed = ProfitPeriodClose.objects.create(
        branch_id=branch_id,
        period_start=period_start,
        period_end=month_end,
        revision=revision,
        source_digest=digest,
        report_snapshot=snapshot,
        correction_reason=correction_reason,
        closed_by=actor,
    )
    return closed, True


def serialize_product_cost(profile):
    return {
        'id': profile.id,
        'branch_id': profile.branch_id,
        'product_id': profile.product_id,
        'product_name': profile.product.name,
        'treatment': profile.treatment,
        'standard_unit_cost': (
            f'{profile.standard_unit_cost:.4f}'
            if profile.standard_unit_cost is not None else None
        ),
        'effective_from': profile.effective_from.isoformat(),
        'effective_to': profile.effective_to.isoformat() if profile.effective_to else None,
        'note': profile.note,
        'verified': bool(profile.verified_at),
        'verified_at': profile.verified_at.isoformat() if profile.verified_at else None,
    }


@transaction.atomic
def save_product_cost(branch_id, payload, actor, *, profile_id=None):
    branch_id = resolve_branch_id(branch_id)
    profile = None
    if profile_id is not None:
        profile = ProductCostProfile.objects.select_for_update().filter(
            id=profile_id, branch_id=branch_id,
        ).first()
        if not profile:
            raise ProfitabilityError('Product cost profile not found')
    product_id = payload.get('product_id', profile.product_id if profile else None)
    product = Product.objects.select_for_update().filter(
        id=product_id, is_deleted=False,
    ).first()
    if not product:
        raise ProfitabilityError(
            'Product not found', errors={'product_id': 'Unknown product'},
        )
    treatment = str(
        payload.get('treatment', profile.treatment if profile else '')
    ).upper()
    if treatment not in ProductCostProfile.Treatment.values:
        raise ProfitabilityError(
            'Invalid cost treatment', errors={'treatment': 'Use STANDARD or ZERO'},
        )
    effective_from = _date(
        payload.get('effective_from', profile.effective_from if profile else None),
        'effective_from',
    )
    effective_to = _date(
        payload.get('effective_to', profile.effective_to if profile else None),
        'effective_to', required=False,
    )
    if effective_to and effective_to < effective_from:
        raise ProfitabilityError(
            'effective_to cannot be before effective_from',
            errors={'effective_to': 'Invalid effective range'},
        )
    standard_cost = None
    if treatment == ProductCostProfile.Treatment.STANDARD:
        standard_cost = _decimal(
            payload.get(
                'standard_unit_cost',
                profile.standard_unit_cost if profile else None,
            ),
            field='standard_unit_cost', positive=True, places='0.0001',
        )
    overlap = ProductCostProfile.objects.filter(
        branch_id=branch_id,
        product=product,
        effective_from__lte=effective_to or date.max,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=effective_from))
    if profile:
        overlap = overlap.exclude(pk=profile.pk)
    if overlap.exists():
        raise ProfitabilityError(
            'This cost overlaps another effective period for the product',
            errors={'effective_from': 'Overlapping cost period'},
        )
    if profile is None:
        profile = ProductCostProfile(branch_id=branch_id)
    profile.product = product
    profile.treatment = treatment
    profile.standard_unit_cost = standard_cost
    profile.effective_from = effective_from
    profile.effective_to = effective_to
    profile.note = str(payload.get('note', profile.note or ''))[:255]
    if 'verified' in payload:
        if _boolean(payload['verified'], 'verified'):
            if not profile.verified_at:
                profile.verified_by = actor
                profile.verified_at = timezone.now()
        else:
            profile.verified_by = None
            profile.verified_at = None
    elif profile.pk is None:
        profile.verified_by = actor
        profile.verified_at = timezone.now()
    _clean_model(profile, 'Invalid product cost profile')
    profile.save()
    return serialize_product_cost(profile)


def serialize_recurring_cost(cost):
    return {
        'id': cost.id,
        'name': cost.name,
        'reporting_group': cost.reporting_group,
        'monthly_amount': _money_string(cost.monthly_amount),
        'start_date': cost.start_date.isoformat(),
        'end_date': cost.end_date.isoformat() if cost.end_date else None,
        'is_active': cost.is_active,
        'note': cost.note,
    }


@transaction.atomic
def save_recurring_cost(branch_id, payload, actor, *, cost_id=None):
    branch_id = resolve_branch_id(branch_id)
    cost = None
    if cost_id is not None:
        cost = RecurringCost.objects.select_for_update().filter(
            id=cost_id, branch_id=branch_id,
        ).first()
        if not cost:
            raise ProfitabilityError('Recurring cost not found')
    name = str(payload.get('name', cost.name if cost else '')).strip()
    if not name:
        raise ProfitabilityError('name is required', errors={'name': 'Required'})
    group = str(payload.get(
        'reporting_group', cost.reporting_group if cost else '',
    )).upper()
    if group not in PROFIT_EXPENSE_GROUPS:
        raise ProfitabilityError(
            'Recurring costs require a P&L expense group',
            errors={'reporting_group': 'Invalid recurring-cost group'},
        )
    amount = _decimal(
        payload.get('monthly_amount', cost.monthly_amount if cost else None),
        field='monthly_amount', positive=True,
    )
    start = _date(
        payload.get('start_date', cost.start_date if cost else None), 'start_date',
    )
    end = _date(
        payload.get('end_date', cost.end_date if cost else None),
        'end_date', required=False,
    )
    if end and end < start:
        raise ProfitabilityError(
            'end_date cannot be before start_date',
            errors={'end_date': 'Invalid date range'},
        )
    if cost is None:
        cost = RecurringCost(branch_id=branch_id, created_by=actor)
    cost.name = name[:140]
    cost.reporting_group = group
    cost.monthly_amount = amount
    cost.start_date = start
    cost.end_date = end
    cost.is_active = _boolean(
        payload.get('is_active', cost.is_active if cost.pk else True),
        'is_active',
    )
    cost.note = str(payload.get('note', cost.note or ''))
    _clean_model(cost, 'Invalid recurring cost')
    cost.save()
    return serialize_recurring_cost(cost)


def serialize_adjustment(adjustment):
    return {
        'id': adjustment.id,
        'effective_date': adjustment.effective_date.isoformat(),
        'direction': adjustment.direction,
        'reporting_group': adjustment.reporting_group,
        'amount': _money_string(adjustment.amount),
        'description': adjustment.description,
        'status': adjustment.status,
        'approved_at': (
            adjustment.approved_at.isoformat() if adjustment.approved_at else None
        ),
    }


@transaction.atomic
def create_adjustment(branch_id, payload, actor):
    branch_id = resolve_branch_id(branch_id)
    direction = str(payload.get('direction', '')).upper()
    if direction not in ProfitAdjustment.Direction.values:
        raise ProfitabilityError(
            'direction must be INCOME or EXPENSE',
            errors={'direction': 'Invalid direction'},
        )
    group = str(payload.get('reporting_group', '')).upper()
    if direction == ProfitAdjustment.Direction.INCOME:
        group = FinancialReportingGroup.OTHER_INCOME
    elif group not in PROFIT_EXPENSE_GROUPS:
        raise ProfitabilityError(
            'Expense adjustments require a P&L expense group',
            errors={'reporting_group': 'Invalid expense group'},
        )
    description = str(payload.get('description', '')).strip()
    if not description:
        raise ProfitabilityError(
            'description is required', errors={'description': 'Required'},
        )
    adjustment = ProfitAdjustment(
        branch_id=branch_id,
        effective_date=_date(payload.get('effective_date'), 'effective_date'),
        direction=direction,
        reporting_group=group,
        amount=_decimal(payload.get('amount'), positive=True),
        description=description[:255],
        created_by=actor,
    )
    _clean_model(adjustment, 'Invalid profit adjustment')
    adjustment.save()
    return serialize_adjustment(adjustment)


@transaction.atomic
def approve_adjustment(branch_id, adjustment_id, actor):
    branch_id = resolve_branch_id(branch_id)
    adjustment = ProfitAdjustment.objects.select_for_update().filter(
        id=adjustment_id, branch_id=branch_id,
    ).first()
    if not adjustment:
        raise ProfitabilityError('Profit adjustment not found')
    if adjustment.status == ProfitAdjustment.Status.APPROVED:
        return serialize_adjustment(adjustment)
    adjustment.status = ProfitAdjustment.Status.APPROVED
    adjustment.approved_by = actor
    adjustment.approved_at = timezone.now()
    adjustment.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    return serialize_adjustment(adjustment)


@transaction.atomic
def delete_draft_adjustment(branch_id, adjustment_id):
    branch_id = resolve_branch_id(branch_id)
    adjustment = ProfitAdjustment.objects.select_for_update().filter(
        id=adjustment_id, branch_id=branch_id,
    ).first()
    if not adjustment:
        raise ProfitabilityError('Profit adjustment not found')
    if adjustment.status != ProfitAdjustment.Status.DRAFT:
        raise ProfitabilityError(
            'Approved profit adjustments are immutable',
            errors={'status': 'Only a draft adjustment can be deleted'},
        )
    adjustment.delete()
    return {'id': adjustment_id, 'deleted': True}


@transaction.atomic
def classify_cashbox_expense(branch_id, expense_id, payload, actor):
    branch_id = resolve_branch_id(branch_id)
    expense = CashboxExpense.objects.filter(
        id=expense_id,
        is_deleted=False,
    ).filter(Q(branch_id=branch_id) | Q(shift__branch_id=branch_id)).first()
    if not expense:
        raise ProfitabilityError('Cashbox expense not found')
    group = str(payload.get('reporting_group', '')).upper()
    if group not in FinancialReportingGroup.values or group in {
        FinancialReportingGroup.REVIEW,
        FinancialReportingGroup.OTHER_INCOME,
    }:
        raise ProfitabilityError(
            'Choose a valid expense or cash-only group',
            errors={'reporting_group': 'Invalid classification'},
        )
    represented_elsewhere = _boolean(
        payload.get('represented_elsewhere', False),
        'represented_elsewhere',
    )
    cash_movement_represented_elsewhere = _boolean(
        payload.get('cash_movement_represented_elsewhere', False),
        'cash_movement_represented_elsewhere',
    )
    note = str(payload.get('note', '')).strip()[:255]
    if (represented_elsewhere or cash_movement_represented_elsewhere) and not note:
        raise ProfitabilityError(
            'A deduplication note is required',
            errors={'note': 'Explain where the duplicate is recorded'},
        )
    classification, _ = CashboxExpenseClassification.objects.update_or_create(
        expense=expense,
        defaults={
            'reporting_group': group,
            'represented_elsewhere': represented_elsewhere,
            'cash_movement_represented_elsewhere': (
                cash_movement_represented_elsewhere
            ),
            'note': note,
            'classified_by': actor,
        },
    )
    return {
        'expense_id': expense.id,
        'reporting_group': classification.reporting_group,
        'represented_elsewhere': classification.represented_elsewhere,
        'cash_movement_represented_elsewhere': (
            classification.cash_movement_represented_elsewhere
        ),
        'note': classification.note,
        'classified_at': classification.classified_at.isoformat(),
    }


@transaction.atomic
def set_expense_category_group(source, category_id, reporting_group):
    group = str(reporting_group or '').upper()
    if group not in EXPENSE_REPORTING_GROUPS:
        raise ProfitabilityError(
            'Invalid financial reporting group',
            errors={'reporting_group': 'Invalid classification'},
        )
    models_by_source = {
        'cashbox': CashboxExpenseCategory,
        'hr': ExpenseCategory,
    }
    model = models_by_source.get(str(source or '').lower())
    if model is None:
        raise ProfitabilityError(
            'source must be cashbox or hr', errors={'source': 'Invalid source'},
        )
    category = model.objects.select_for_update().filter(
        id=category_id, is_deleted=False,
    ).first()
    if not category:
        raise ProfitabilityError('Expense category not found')
    category.reporting_group = group
    category.save(update_fields=[
        'reporting_group', 'updated_at', 'synced_at', 'sync_version',
    ])
    return {
        'source': str(source).lower(),
        'category_id': category.id,
        'category_name': category.name,
        'reporting_group': category.reporting_group,
    }


def setup_data(
    branch_id, *, payout_page=1, payout_page_size=100, payout_status='all',
):
    branch_id = resolve_branch_id(branch_id)
    payout_page = _positive_int(
        payout_page, 'payout_page', default=1,
    )
    payout_page_size = _positive_int(
        payout_page_size, 'payout_page_size', default=100, maximum=200,
    )
    payout_status = str(payout_status or 'all').strip().lower()
    if payout_status not in {'all', 'unresolved'}:
        raise ProfitabilityError(
            'payout_status is invalid',
            errors={'payout_status': 'Use all or unresolved'},
        )
    config, saved = _configuration(branch_id)
    costs = ProductCostProfile.objects.filter(branch_id=branch_id).select_related('product')
    recurring = RecurringCost.objects.filter(branch_id=branch_id)
    adjustments = ProfitAdjustment.objects.filter(branch_id=branch_id)
    sold_product_ids = set(OrderItem.objects.filter(
        is_deleted=False,
        order__is_deleted=False,
        order__is_paid=True,
        order__branch_id=branch_id,
        order__paid_at__date__gte=config.reporting_start_date,
    ).values_list('product_id', flat=True).distinct())
    products = Product.objects.filter(
        is_deleted=False,
    ).order_by('name')
    cashbox_categories = CashboxExpenseCategory.objects.filter(
        is_deleted=False, is_active=True,
    ).order_by('sort_order', 'name')
    hr_categories = ExpenseCategory.objects.filter(
        is_deleted=False, is_active=True,
    ).order_by('name')
    payout_query = CashboxExpense.objects.filter(
        is_deleted=False,
        created_at__date__gte=config.reporting_start_date,
    ).filter(Q(branch_id=branch_id) | Q(shift__branch_id=branch_id))
    if payout_status == 'unresolved':
        unresolved_category = (
            Q(category__isnull=True)
            | Q(category__reporting_group=FinancialReportingGroup.REVIEW)
        )
        payout_query = payout_query.filter(
            Q(
                profitability_classification__reporting_group=(
                    FinancialReportingGroup.REVIEW
                )
            )
            | (
                Q(profitability_classification__isnull=True)
                & unresolved_category
            )
        )
    payout_query = payout_query.select_related('category').order_by(
        '-created_at', '-id',
    )
    payout_total = payout_query.count()
    payout_offset = (payout_page - 1) * payout_page_size
    candidate_payouts = list(
        payout_query[payout_offset:payout_offset + payout_page_size]
    )
    payout_pages = (
        (payout_total + payout_page_size - 1) // payout_page_size
        if payout_total else 0
    )
    payout_overrides = {
        row.expense_id: row
        for row in CashboxExpenseClassification.objects.filter(
            expense_id__in=[row.id for row in candidate_payouts]
        )
    }
    return {
        'configuration': serialize_configuration(config, saved=saved),
        'reporting_groups': [
            {'value': value, 'label': label}
            for value, label in FinancialReportingGroup.choices
        ],
        'products': [
            {
                'id': product.id,
                'name': product.name,
                'price': _money_string(product.price),
                'sold_since_launch': product.id in sold_product_ids,
            }
            for product in products
        ],
        'product_costs': [serialize_product_cost(row) for row in costs],
        'recurring_costs': [serialize_recurring_cost(row) for row in recurring],
        'adjustments': [serialize_adjustment(row) for row in adjustments[:200]],
        'cashbox_categories': [
            {
                'id': row.id,
                'name': row.name,
                'reporting_group': row.reporting_group,
            }
            for row in cashbox_categories
        ],
        'hr_categories': [
            {
                'id': row.id,
                'name': row.name,
                'reporting_group': row.reporting_group,
            }
            for row in hr_categories
        ],
        'cashbox_expenses': [
            {
                'id': row.id,
                'created_at': row.created_at.isoformat(),
                'amount': _money_string(row.amount),
                'comment': CashboxExpense.visible_comment(row.comment),
                'category_name': row.category.name if row.category_id else None,
                'category_group': (
                    row.category.reporting_group if row.category_id else None
                ),
                'classification': (
                    {
                        'reporting_group': payout_overrides[row.id].reporting_group,
                        'represented_elsewhere': (
                            payout_overrides[row.id].represented_elsewhere
                        ),
                        'cash_movement_represented_elsewhere': (
                            payout_overrides[
                                row.id
                            ].cash_movement_represented_elsewhere
                        ),
                        'note': payout_overrides[row.id].note,
                    }
                    if row.id in payout_overrides else None
                ),
            }
            for row in candidate_payouts
        ],
        'cashbox_expenses_pagination': {
            'page': payout_page,
            'page_size': payout_page_size,
            'total_items': payout_total,
            'total_pages': payout_pages,
            'has_previous': payout_page > 1,
            'has_next': payout_page < payout_pages,
            'status': payout_status,
        },
        'closed_periods': [
            {
                'id': row.id,
                'period_start': row.period_start.isoformat(),
                'period_end': row.period_end.isoformat(),
                'revision': row.revision,
                'closed_at': row.closed_at.isoformat(),
                'correction_reason': row.correction_reason,
            }
            for row in ProfitPeriodClose.objects.filter(branch_id=branch_id)[:24]
        ],
    }
