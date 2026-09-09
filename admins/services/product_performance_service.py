"""Canonical, refund-aware product performance reporting.

The report is intentionally assembled once and then rendered to JSON/XLSX/PDF/
CSV.  Historical food cost comes from the immutable stock movements written for
the exact sold order line.  A verified effective-dated ProductCostProfile is the
only fallback; the current recipe cost is never substituted into an old sale.
"""

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from admins.models import ProductCostProfile
from admins.services.profitability_service import resolve_branch_id
from base.models import Order, OrderItem, OrderRefund
from base.services.business_day import business_date, resolve_reporting_window
from base.services.refund_lines import (
    REFUND_EVENT_ALIAS,
    refund_item_events,
    refund_line_quantity,
    refund_line_revenue,
)
from base.services.revenue import net_line_revenue
from stock.models import StockTransaction


ZERO = Decimal('0')
MONEY_QUANTUM = Decimal('0.01')
COST_QUANTUM = Decimal('0.0001')
RATE_QUANTUM = Decimal('0.01')
MAX_REPORT_DAYS = 366
MAX_EVENT_LINES = 100_000

PRESETS = (
    'today', 'yesterday', 'last_7_days', 'last_month', 'current_month',
    'custom',
)
SORT_OPTIONS = (
    'highest_units', 'highest_revenue', 'highest_profit', 'lowest_profit',
    'highest_profit_margin', 'lowest_profit_margin',
)
ORDER_TYPES = {choice for choice, _label in Order.OrderType.choices}
ORDER_ORIGINS = {choice for choice, _label in Order.Origin.choices}
PAYMENT_METHODS = {choice for choice, _label in Order.PaymentMethod.choices}


class ProductPerformanceError(ValueError):
    """Safe validation/size error returned by the admin report API."""

    def __init__(self, message, *, code='INVALID_REPORT_FILTER', errors=None,
                 status=422):
        super().__init__(message)
        self.code = code
        self.errors = errors or {}
        self.status = status


def _money(value):
    return Decimal(value or ZERO).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _cost(value):
    return Decimal(value or ZERO).quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def _money_string(value):
    return f'{_money(value):.2f}'


def _cost_string(value):
    return f'{_cost(value):.4f}'


def _rate(numerator, denominator):
    denominator = Decimal(denominator or ZERO)
    if denominator == ZERO:
        return None
    return (Decimal(numerator or ZERO) * Decimal('100') / denominator).quantize(
        RATE_QUANTUM, rounding=ROUND_HALF_UP,
    )


def _margin(profit, revenue):
    """Return a profit margin only when positive revenue is meaningful."""
    if profit is None or Decimal(revenue or ZERO) <= ZERO:
        return None
    return _rate(profit, revenue)


def _rate_string(value):
    return None if value is None else f'{value:.2f}'


def _positive_int(value, field, *, default=None, maximum=None):
    if value in (None, ''):
        return default
    if isinstance(value, bool):
        parsed = 0
    else:
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            parsed = 0
    if parsed < 1 or (maximum is not None and parsed > maximum):
        limit = f' between 1 and {maximum}' if maximum is not None else ' positive'
        raise ProductPerformanceError(
            f'{field} must be{limit}', errors={field: 'Invalid value'},
        )
    return parsed


def _normalize_choice(value, field, choices, *, default=None):
    if value in (None, ''):
        return default
    normalized = str(value).strip().lower()
    aliases = {str(choice).lower().replace('-', '_'): choice for choice in choices}
    normalized = normalized.replace('-', '_')
    if normalized not in aliases:
        raise ProductPerformanceError(
            f'Unsupported {field}', errors={field: f'Use one of: {", ".join(choices)}'},
        )
    return aliases[normalized]


def _preset_window(
    preset=None, *, date_from=None, date_to=None, datetime_from=None,
    datetime_to=None, from_at=None, to_at=None, tod_from=None, tod_to=None,
    now=None,
):
    now = now or timezone.now()
    current_business_date = business_date(now)
    has_exact = any((datetime_from, datetime_to, from_at, to_at, tod_from, tod_to))
    if preset in (None, ''):
        preset = 'custom' if (date_from or date_to or has_exact) else 'today'
    preset = _normalize_choice(preset, 'preset', PRESETS)

    if preset == 'today':
        date_from = date_to = current_business_date
    elif preset == 'yesterday':
        date_from = date_to = current_business_date - timedelta(days=1)
    elif preset == 'last_7_days':
        date_from = current_business_date - timedelta(days=6)
        date_to = current_business_date
    elif preset == 'current_month':
        date_from = current_business_date.replace(day=1)
        date_to = current_business_date
    elif preset == 'last_month':
        date_to = current_business_date.replace(day=1) - timedelta(days=1)
        date_from = date_to.replace(day=1)
    elif not has_exact:
        if not date_from or not date_to:
            raise ProductPerformanceError(
                'Custom reports require from and to dates',
                code='INVALID_REPORT_RANGE',
                errors={'range': 'Supply from and to as YYYY-MM-DD'},
            )
        parsed_from = parse_date(str(date_from).strip())
        parsed_to = parse_date(str(date_to).strip())
        if parsed_from is None or parsed_to is None:
            raise ProductPerformanceError(
                'Report dates must use YYYY-MM-DD',
                code='INVALID_REPORT_RANGE',
                errors={'range': 'Invalid date'},
            )
        if parsed_to < parsed_from:
            raise ProductPerformanceError(
                'The end date must be on or after the start date',
                code='INVALID_REPORT_RANGE',
                errors={'to': 'Must be on or after from'},
            )

    try:
        window = resolve_reporting_window(
            date_from=date_from,
            date_to=date_to,
            datetime_from=datetime_from,
            datetime_to=datetime_to,
            from_at=from_at,
            to_at=to_at,
            tod_from=tod_from,
            tod_to=tod_to,
        )
    except ValueError as exc:
        raise ProductPerformanceError(
            str(exc), code='INVALID_REPORT_RANGE', errors={'range': str(exc)},
        ) from None
    if window.days > MAX_REPORT_DAYS:
        raise ProductPerformanceError(
            f'Report range cannot exceed {MAX_REPORT_DAYS} days',
            code='REPORT_RANGE_TOO_LARGE',
            errors={'range': f'Maximum {MAX_REPORT_DAYS} days'},
        )
    if window.date_to > current_business_date and window.mode == 'business':
        raise ProductPerformanceError(
            'The report end date cannot be in the future',
            code='INVALID_REPORT_RANGE',
            errors={'to': 'Future business dates are not allowed'},
        )
    return preset, window


def _profile_for(profiles, on_date):
    for profile in profiles:
        if profile.effective_from > on_date:
            continue
        if profile.effective_to and profile.effective_to < on_date:
            continue
        return profile
    return None


def _apply_order_filters(
    queryset, *, prefix='', cashier_id=None, order_type=None,
    order_origin=None, payment_method=None,
):
    if cashier_id:
        queryset = queryset.filter(**{f'{prefix}cashier_id': cashier_id})
    if order_type:
        queryset = queryset.filter(**{f'{prefix}order_type': order_type})
    if order_origin:
        queryset = queryset.filter(**{f'{prefix}order_origin': order_origin})
    if payment_method:
        methods = (
            ('CARD', 'UZCARD', 'HUMO') if payment_method == 'CARD'
            else (payment_method,)
        )
        queryset = queryset.filter(
            Q(**{f'{prefix}payment_method__in': methods})
            | Q(**{f'{prefix}payments__method__in': methods})
        ).distinct()
    return queryset


def _apply_item_filters(queryset, *, category_id=None, product_id=None, search=''):
    if category_id:
        queryset = queryset.filter(product__category_id=category_id)
    if product_id:
        queryset = queryset.filter(product_id=product_id)
    if search:
        queryset = queryset.filter(product__name__icontains=search)
    return queryset


def _new_bucket(*, product=None, category=None):
    return {
        'product_id': getattr(product, 'id', None),
        'product_name': getattr(product, 'name', ''),
        'category_id': getattr(category, 'id', None),
        'category_name': getattr(category, 'name', '') or 'Uncategorized',
        'current_catalog_price': Decimal(getattr(product, 'price', ZERO) or ZERO),
        'gross_units': 0,
        'refunded_units': 0,
        'gross_revenue': ZERO,
        'refund_amount': ZERO,
        'gross_cost': ZERO,
        'refund_cost_credit': ZERO,
        'gross_cost_complete': True,
        'cost_complete': True,
        'orders': set(),
        'refunds': set(),
        'cost_sources': Counter(),
        'cost_basis_revenue': ZERO,
        'known_cost_basis_revenue': ZERO,
        'min_selling_price': None,
        'max_selling_price': None,
    }


def _new_period_bucket(label):
    return {
        'label': label,
        'gross_units': 0,
        'refunded_units': 0,
        'gross_revenue': ZERO,
        'refund_amount': ZERO,
        'gross_cost': ZERO,
        'refund_cost_credit': ZERO,
        'gross_cost_complete': True,
        'cost_complete': True,
        'orders': set(),
        'refunds': set(),
        'cost_sources': Counter(),
        'cost_basis_revenue': ZERO,
        'known_cost_basis_revenue': ZERO,
        'min_selling_price': None,
        'max_selling_price': None,
    }


def _record_sale(bucket, *, item, revenue, line_cost, source):
    quantity = int(item.quantity)
    bucket['gross_units'] += quantity
    bucket['gross_revenue'] += revenue
    bucket['orders'].add(item.order_id)
    bucket['cost_basis_revenue'] += abs(revenue)
    bucket['cost_sources'][source] += 1
    if line_cost is None:
        bucket['gross_cost_complete'] = False
        bucket['cost_complete'] = False
    else:
        bucket['gross_cost'] += line_cost
        bucket['known_cost_basis_revenue'] += abs(revenue)
    if quantity:
        unit_price = revenue / Decimal(quantity)
        current_min = bucket.get('min_selling_price')
        current_max = bucket.get('max_selling_price')
        bucket['min_selling_price'] = unit_price if current_min is None else min(
            current_min, unit_price,
        )
        bucket['max_selling_price'] = unit_price if current_max is None else max(
            current_max, unit_price,
        )


def _record_refund(
    bucket, *, item, refund_id, refund_source, revenue, quantity,
    line_cost, source,
):
    bucket['refund_amount'] += revenue
    bucket['refunded_units'] += int(quantity or 0)
    bucket['refunds'].add(refund_id)
    if refund_source != OrderRefund.Source.ORDER_CANCEL:
        return
    bucket['cost_basis_revenue'] += abs(revenue)
    bucket['cost_sources'][source] += 1
    if line_cost is None:
        bucket['cost_complete'] = False
    else:
        bucket['refund_cost_credit'] += line_cost
        bucket['known_cost_basis_revenue'] += abs(revenue)


def _line_cost_resolver(items, branch_id):
    item_ids = [item.id for item in items]
    product_ids = {item.product_id for item in items}
    actual_costs = {
        row['order_item_id']: Decimal(row['total'] or ZERO)
        for row in StockTransaction.objects.filter(
            is_deleted=False,
            movement_type=StockTransaction.MovementType.SALE_OUT,
            order_item_id__in=item_ids,
        ).values('order_item_id').annotate(total=Sum('total_cost'))
    }
    profiles_by_product = defaultdict(list)
    profiles = ProductCostProfile.objects.filter(
        branch_id=branch_id,
        product_id__in=product_ids,
        verified_at__isnull=False,
    ).order_by('product_id', '-effective_from', '-id')
    for profile in profiles:
        profiles_by_product[profile.product_id].append(profile)

    def resolve(item):
        actual = actual_costs.get(item.id)
        if actual is not None and actual > ZERO:
            return actual, 'ACTUAL_STOCK'
        if not item.order.paid_at:
            return None, 'MISSING'
        sold_on = business_date(item.order.paid_at)
        profile = _profile_for(profiles_by_product[item.product_id], sold_on)
        if profile is None:
            return None, 'MISSING'
        if profile.treatment == ProductCostProfile.Treatment.ZERO:
            return ZERO, 'EXPLICIT_ZERO'
        if (
            profile.treatment == ProductCostProfile.Treatment.STANDARD
            and profile.standard_unit_cost is not None
            and profile.standard_unit_cost > ZERO
        ):
            return (
                Decimal(profile.standard_unit_cost) * Decimal(item.quantity),
                'VERIFIED_STANDARD',
            )
        return None, 'MISSING'

    return resolve


def _cost_source_label(counter):
    used = [source for source, count in counter.items() if count]
    if not used:
        return 'NO_ACTIVITY'
    if len(used) == 1:
        return used[0]
    if set(used) == {'MISSING'}:
        return 'MISSING'
    return 'MIXED'


def _serialize_bucket(bucket):
    gross_units = bucket['gross_units']
    net_units = gross_units - bucket['refunded_units']
    net_revenue = bucket['gross_revenue'] - bucket['refund_amount']
    known_net_cost = bucket['gross_cost'] - bucket['refund_cost_credit']
    complete = bool(bucket['cost_complete'])
    gross_complete = bool(bucket['gross_cost_complete'])
    net_profit = net_revenue - known_net_cost if complete else None
    sale_profit = (
        bucket['gross_revenue'] - bucket['gross_cost'] if gross_complete else None
    )
    average_price = (
        bucket['gross_revenue'] / Decimal(gross_units) if gross_units else None
    )
    average_cost = (
        bucket['gross_cost'] / Decimal(gross_units)
        if gross_units and gross_complete else None
    )
    profit_per_item = (
        sale_profit / Decimal(gross_units)
        if gross_units and sale_profit is not None else None
    )
    margin = _margin(net_profit, net_revenue)
    coverage = _rate(
        bucket['known_cost_basis_revenue'], bucket['cost_basis_revenue'],
    )
    row = {
        'product_id': bucket['product_id'],
        'product_name': bucket['product_name'],
        'category_id': bucket['category_id'],
        'category_name': bucket['category_name'],
        'units_sold': gross_units,
        'units_refunded': bucket['refunded_units'],
        'net_units': net_units,
        'orders_sold': len(bucket['orders']),
        'refund_events': len(bucket['refunds']),
        'selling_price_per_unit': (
            _cost_string(average_price) if average_price is not None else None
        ),
        'minimum_selling_price': (
            _cost_string(bucket['min_selling_price'])
            if bucket.get('min_selling_price') is not None else None
        ),
        'maximum_selling_price': (
            _cost_string(bucket['max_selling_price'])
            if bucket.get('max_selling_price') is not None else None
        ),
        'current_catalog_price': _money_string(bucket['current_catalog_price']),
        'gross_sales_revenue': _money_string(bucket['gross_revenue']),
        'refund_amount': _money_string(bucket['refund_amount']),
        'total_revenue': _money_string(net_revenue),
        'ingredient_cost_per_unit': (
            _cost_string(average_cost) if average_cost is not None else None
        ),
        'gross_ingredient_cost': (
            _money_string(bucket['gross_cost']) if gross_complete else None
        ),
        'ingredient_cost_credit': (
            _money_string(bucket['refund_cost_credit']) if complete else None
        ),
        'total_ingredient_cost': (
            _money_string(known_net_cost) if complete else None
        ),
        'gross_profit_per_item': (
            _cost_string(profit_per_item) if profit_per_item is not None else None
        ),
        'gross_profit': _money_string(net_profit) if net_profit is not None else None,
        'gross_profit_margin_pct': _rate_string(margin),
        'cost_source': _cost_source_label(bucket['cost_sources']),
        'cost_complete': complete,
        'cost_coverage_pct': _rate_string(coverage) or '100.00',
        '_sort_profit': net_profit,
        '_sort_margin': margin,
        '_sort_revenue': net_revenue,
    }
    return row


def _sort_rows(rows, sort):
    def missing_last(value, descending=False):
        if value is None:
            return (1, ZERO)
        value = Decimal(value)
        return (0, -value if descending else value)

    def key(row):
        stable = (row['product_name'].casefold(), row['product_id'])
        if sort == 'highest_units':
            return (-row['units_sold'], *stable)
        if sort == 'highest_revenue':
            return (-row['_sort_revenue'], *stable)
        if sort == 'highest_profit':
            return (*missing_last(row['_sort_profit'], True), *stable)
        if sort == 'lowest_profit':
            return (*missing_last(row['_sort_profit']), *stable)
        if sort == 'highest_profit_margin':
            return (*missing_last(row['_sort_margin'], True), *stable)
        return (*missing_last(row['_sort_margin']), *stable)

    return sorted(rows, key=key)


def _serialize_summary(buckets, *, sale_orders, refund_events):
    gross_units = sum(row['gross_units'] for row in buckets)
    refunded_units = sum(row['refunded_units'] for row in buckets)
    gross_revenue = sum((row['gross_revenue'] for row in buckets), ZERO)
    refunds = sum((row['refund_amount'] for row in buckets), ZERO)
    gross_cost = sum((row['gross_cost'] for row in buckets), ZERO)
    refund_credit = sum((row['refund_cost_credit'] for row in buckets), ZERO)
    complete = all(row['cost_complete'] for row in buckets)
    gross_complete = all(row['gross_cost_complete'] for row in buckets)
    net_revenue = gross_revenue - refunds
    known_cost = gross_cost - refund_credit
    profit = net_revenue - known_cost if complete else None
    basis = sum((row['cost_basis_revenue'] for row in buckets), ZERO)
    known_basis = sum((row['known_cost_basis_revenue'] for row in buckets), ZERO)
    return {
        'product_count': len(buckets),
        'order_count': len(sale_orders),
        'refund_event_count': len(refund_events),
        'total_units_sold': gross_units,
        'total_units_refunded': refunded_units,
        'net_units': gross_units - refunded_units,
        'gross_sales_revenue': _money_string(gross_revenue),
        'refund_amount': _money_string(refunds),
        'total_revenue': _money_string(net_revenue),
        'gross_ingredient_cost': _money_string(gross_cost) if gross_complete else None,
        'ingredient_cost_credit': _money_string(refund_credit) if complete else None,
        'total_ingredient_cost': _money_string(known_cost) if complete else None,
        'known_ingredient_cost': _money_string(known_cost),
        'total_gross_profit': _money_string(profit) if profit is not None else None,
        'gross_profit_margin_pct': _rate_string(_margin(profit, net_revenue)),
        'average_selling_price_per_unit': (
            _cost_string(gross_revenue / Decimal(gross_units)) if gross_units else None
        ),
        'average_ingredient_cost_per_unit': (
            _cost_string(gross_cost / Decimal(gross_units))
            if gross_units and gross_complete else None
        ),
        'cost_complete': complete,
        'cost_coverage_pct': _rate_string(_rate(known_basis, basis)) or '100.00',
        'products_missing_cost': sum(1 for row in buckets if not row['cost_complete']),
    }


def _serialize_period_bucket(bucket, *, key_name):
    net_revenue = bucket['gross_revenue'] - bucket['refund_amount']
    net_cost = bucket['gross_cost'] - bucket['refund_cost_credit']
    complete = bool(bucket['cost_complete'])
    profit = net_revenue - net_cost if complete else None
    return {
        key_name: bucket['label'],
        'orders': len(bucket['orders']),
        'refund_events': len(bucket['refunds']),
        'units_sold': bucket['gross_units'],
        'units_refunded': bucket['refunded_units'],
        'net_units': bucket['gross_units'] - bucket['refunded_units'],
        'gross_sales_revenue': _money_string(bucket['gross_revenue']),
        'refund_amount': _money_string(bucket['refund_amount']),
        'total_revenue': _money_string(net_revenue),
        'total_ingredient_cost': _money_string(net_cost) if complete else None,
        'gross_profit': _money_string(profit) if profit is not None else None,
        'gross_profit_margin_pct': _rate_string(_margin(profit, net_revenue)),
        'cost_complete': complete,
    }


def product_performance_report(
    *, branch_id=None, preset=None, date_from=None, date_to=None,
    datetime_from=None, datetime_to=None, from_at=None, to_at=None,
    tod_from=None, tod_to=None, category_id=None, product_id=None, search='',
    cashier_id=None, order_type=None, order_origin=None, payment_method=None,
    sort='highest_revenue', page=1, per_page=100, paginate=True, now=None,
):
    """Return one canonical dataset for on-screen and downloaded reports."""
    branch_id = resolve_branch_id(branch_id)
    preset, window = _preset_window(
        preset,
        date_from=date_from,
        date_to=date_to,
        datetime_from=datetime_from,
        datetime_to=datetime_to,
        from_at=from_at,
        to_at=to_at,
        tod_from=tod_from,
        tod_to=tod_to,
        now=now,
    )
    category_id = _positive_int(category_id, 'category_id')
    product_id = _positive_int(product_id, 'product_id')
    cashier_id = _positive_int(cashier_id, 'cashier_id')
    page = _positive_int(page, 'page', default=1)
    per_page = _positive_int(per_page, 'per_page', default=100, maximum=500)
    sort = _normalize_choice(sort, 'sort', SORT_OPTIONS, default='highest_revenue')
    order_type = _normalize_choice(order_type, 'order_type', ORDER_TYPES)
    order_origin = _normalize_choice(order_origin, 'order_origin', ORDER_ORIGINS)
    payment_method = _normalize_choice(
        payment_method, 'payment_method', PAYMENT_METHODS,
    )
    search = str(search or '').strip()
    if len(search) > 100:
        raise ProductPerformanceError(
            'search is too long', errors={'search': 'Maximum 100 characters'},
        )

    sales = window.filter(
        Order.objects.filter(
            is_deleted=False,
            is_paid=True,
            paid_at__isnull=False,
            branch_id=branch_id,
        ),
        'paid_at',
    )
    sales = _apply_order_filters(
        sales,
        cashier_id=cashier_id,
        order_type=order_type,
        order_origin=order_origin,
        payment_method=payment_method,
    )
    sale_items_qs = _apply_item_filters(
        OrderItem.objects.filter(is_deleted=False, order__in=sales),
        category_id=category_id,
        product_id=product_id,
        search=search,
    ).select_related('order', 'product', 'product__category').annotate(
        reporting_revenue=net_line_revenue(),
    ).order_by('order_id', 'id')

    refunds = window.filter(
        OrderRefund.objects.filter(is_deleted=False, branch_id=branch_id),
        'refunded_at',
    )
    refunds = _apply_order_filters(
        refunds,
        prefix='order__',
        cashier_id=cashier_id,
        order_type=order_type,
        order_origin=order_origin,
        payment_method=payment_method,
    )
    refund_items_qs = refund_item_events(
        _apply_item_filters(
            OrderItem.objects.all(),
            category_id=category_id,
            product_id=product_id,
            search=search,
        ),
        pk__in=refunds,
    ).select_related('order', 'product', 'product__category').annotate(
        reporting_refund_revenue=refund_line_revenue(REFUND_EVENT_ALIAS),
        reporting_refund_quantity=refund_line_quantity(REFUND_EVENT_ALIAS),
        reporting_refund_id=F(f'{REFUND_EVENT_ALIAS}__id'),
        reporting_refunded_at=F(f'{REFUND_EVENT_ALIAS}__refunded_at'),
        reporting_refund_source=F(f'{REFUND_EVENT_ALIAS}__source'),
    ).order_by(f'{REFUND_EVENT_ALIAS}__refunded_at', 'order_id', 'id')

    sale_line_count = sale_items_qs.count()
    refund_line_count = refund_items_qs.count()
    if sale_line_count + refund_line_count > MAX_EVENT_LINES:
        raise ProductPerformanceError(
            'This report contains too many sale/refund lines; choose a shorter range',
            code='REPORT_TOO_LARGE', status=413,
            errors={'range': f'Maximum {MAX_EVENT_LINES} event lines'},
        )

    sale_items = list(sale_items_qs)
    refund_items = list(refund_items_qs)
    unique_items = {item.id: item for item in (*sale_items, *refund_items)}
    resolve_cost = _line_cost_resolver(list(unique_items.values()), branch_id)

    product_buckets = {}
    daily = {}
    cursor = window.date_from
    while cursor <= window.date_to:
        daily[cursor] = _new_period_bucket(cursor.isoformat())
        cursor += timedelta(days=1)

    sale_order_ids = set()
    refund_ids = set()
    for item in sale_items:
        bucket = product_buckets.setdefault(
            item.product_id,
            _new_bucket(product=item.product, category=item.product.category),
        )
        revenue = Decimal(item.reporting_revenue or ZERO)
        line_cost, source = resolve_cost(item)
        _record_sale(
            bucket, item=item, revenue=revenue, line_cost=line_cost, source=source,
        )
        sold_day = business_date(item.order.paid_at)
        day_bucket = daily.setdefault(sold_day, _new_period_bucket(sold_day.isoformat()))
        _record_sale(
            day_bucket, item=item, revenue=revenue, line_cost=line_cost, source=source,
        )
        sale_order_ids.add(item.order_id)

    for item in refund_items:
        bucket = product_buckets.setdefault(
            item.product_id,
            _new_bucket(product=item.product, category=item.product.category),
        )
        revenue = Decimal(item.reporting_refund_revenue or ZERO)
        quantity = int(item.reporting_refund_quantity or 0)
        line_cost, source = resolve_cost(item)
        refund_id = item.reporting_refund_id
        refund_source = item.reporting_refund_source
        _record_refund(
            bucket,
            item=item,
            refund_id=refund_id,
            refund_source=refund_source,
            revenue=revenue,
            quantity=quantity,
            line_cost=line_cost,
            source=source,
        )
        refund_day = business_date(item.reporting_refunded_at)
        day_bucket = daily.setdefault(
            refund_day, _new_period_bucket(refund_day.isoformat()),
        )
        _record_refund(
            day_bucket,
            item=item,
            refund_id=refund_id,
            refund_source=refund_source,
            revenue=revenue,
            quantity=quantity,
            line_cost=line_cost,
            source=source,
        )
        refund_ids.add(refund_id)

    buckets = list(product_buckets.values())
    rows = _sort_rows([_serialize_bucket(bucket) for bucket in buckets], sort)
    for rank, row in enumerate(rows, start=1):
        row['rank'] = rank

    category_buckets = {}
    for source in buckets:
        key = source['category_id']
        target = category_buckets.setdefault(
            key, _new_period_bucket(source['category_name']),
        )
        for field in (
            'gross_units', 'refunded_units', 'gross_revenue', 'refund_amount',
            'gross_cost', 'refund_cost_credit',
        ):
            target[field] += source[field]
        target['gross_cost_complete'] &= source['gross_cost_complete']
        target['cost_complete'] &= source['cost_complete']
        target['orders'].update(source['orders'])
        target['refunds'].update(source['refunds'])
        target.setdefault('products', set()).add(source['product_id'])
        target['category_id'] = key
    categories = []
    for target in category_buckets.values():
        row = _serialize_period_bucket(target, key_name='category_name')
        row['category_id'] = target['category_id']
        row['product_count'] = len(target['products'])
        categories.append(row)
    categories.sort(
        key=lambda row: (-Decimal(row['total_revenue']), row['category_name'].casefold())
    )

    clean_rows = []
    for row in rows:
        clean_rows.append({
            key: value for key, value in row.items() if not key.startswith('_sort_')
        })
    total_products = len(clean_rows)
    total_pages = max(1, (total_products + per_page - 1) // per_page)
    if paginate:
        start = (page - 1) * per_page
        visible_rows = clean_rows[start:start + per_page]
    else:
        visible_rows = clean_rows
        page = 1
        per_page = max(total_products, 1)
        total_pages = 1

    generated_at = timezone.now()
    return {
        'status': 'PROVISIONAL',
        'currency': 'UZS',
        'branch_id': branch_id,
        'range': window.metadata(preset=preset),
        'filters': {
            'category_id': category_id,
            'product_id': product_id,
            'search': search,
            'cashier_id': cashier_id,
            'order_type': order_type,
            'order_origin': order_origin,
            'payment_method': payment_method,
            'sort': sort,
        },
        'summary': _serialize_summary(
            buckets, sale_orders=sale_order_ids, refund_events=refund_ids,
        ),
        'products': visible_rows,
        'categories': categories,
        'daily': [
            _serialize_period_bucket(daily[key], key_name='business_date')
            for key in sorted(daily)
        ],
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total_products,
            'total_pages': total_pages,
        },
        'options': {
            'presets': list(PRESETS),
            'sorts': list(SORT_OPTIONS),
            'formats': ['xlsx', 'pdf', 'csv'],
        },
        'coverage': {
            'cost_complete': all(row['cost_complete'] for row in buckets),
            'missing_cost_products': [
                {
                    'product_id': row['product_id'],
                    'product_name': row['product_name'],
                }
                for row in buckets if not row['cost_complete']
            ],
            'policy': (
                'Exact immutable SALE_OUT cost per order line; verified '
                'effective-dated standard cost fallback; no current recipe-cost '
                'substitution for historical sales.'
            ),
        },
        'source_policy': {
            'sales_clock': 'Order.paid_at',
            'refund_clock': 'OrderRefund.refunded_at',
            'revenue': 'Discount-adjusted product-line revenue',
            'ingredient_cost': (
                'StockTransaction.SALE_OUT total_cost linked to OrderItem, then '
                'verified ProductCostProfile effective on the business date'
            ),
            'refund_cost': (
                'COGS is credited only for terminal ORDER_CANCEL returns; '
                'provider/tender-only refunds reduce revenue without inventing '
                'an inventory return.'
            ),
        },
        'event_counts': {
            'sale_lines': sale_line_count,
            'refund_lines': refund_line_count,
        },
        'generated_at': generated_at.isoformat(),
    }
