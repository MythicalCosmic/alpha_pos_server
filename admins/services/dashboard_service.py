"""Owner dashboard: one-call snapshot of today's operating state.

Bundles today's revenue, top products today, low-stock count, open orders,
and who's currently clocked in. Designed for the future owner mobile app
so a single fetch fills the home screen.

Each piece is best-effort — a low-stock query crash shouldn't take down
the revenue widget. Failures are logged and the field is set to None.
"""
import logging
from decimal import Decimal

from django.db.models import (
    Avg, Count, DecimalField, DurationField, ExpressionWrapper, F, Q, Sum,
)
from django.db.models.functions import Coalesce, ExtractHour
from django.utils import timezone

logger = logging.getLogger(__name__)

# Concrete tender types broken out on the "today" payment breakdown. Legacy
# orders with a NULL payment_method are bundled into CASH (matches the shift
# reconciliation convention).
_PAYMENT_METHODS = ('CASH', 'UZCARD', 'HUMO', 'PAYME', 'MIXED')


def _safe(label, fn, default):
    """Run a today-widget query best-effort: log + fall back on failure so one
    broken aggregate never takes down the whole dashboard."""
    try:
        return fn()
    except Exception:
        logger.exception('%s failed', label)
        return default


def _uzs(value):
    """Money as an integer-so'm string (UZS has no minor unit). Consistent across
    empty (None -> '0') and non-empty (Decimal('150000.00') -> '150000') — the POS
    domain format the dashboard FE expects, instead of a backend-dependent mix of
    '0' and '150000.00'."""
    try:
        return str(int(value or 0))
    except (TypeError, ValueError):
        return '0'


def _today_window():
    # "Today" = the current BUSINESS day (cutover at AppSettings.business_day_start,
    # default 03:00), so a 01:00 sale still counts toward the night before.
    from base.services.business_day import today_window
    return today_window()


def _today_revenue():
    from base.models import Order
    start, end = _today_window()
    agg = Order.objects.filter(
        is_deleted=False, is_paid=True,
        created_at__gte=start, created_at__lt=end,
    ).exclude(status='CANCELED').aggregate(
        total=Sum('total_amount'),
        orders=Count('id'),
    )
    return {
        'revenue': _uzs(agg['total']),
        'paid_orders': agg['orders'] or 0,
    }


def _today_orders_breakdown():
    from base.models import Order
    start, end = _today_window()
    agg = Order.objects.filter(
        is_deleted=False, created_at__gte=start, created_at__lt=end,
    ).aggregate(
        total=Count('id'),
        cancelled=Count('id', filter=Q(status='CANCELED')),
        open_=Count('id', filter=Q(status__in=['OPEN', 'PREPARING', 'READY'])),
    )
    return {
        'orders': agg['total'] or 0,
        'cancelled': agg['cancelled'] or 0,
        'open': agg['open_'] or 0,
    }


def _top_products_today(limit=5):
    from base.models import OrderItem
    start, end = _today_window()
    line_total = ExpressionWrapper(
        F('price') * F('quantity'),
        output_field=DecimalField(max_digits=18, decimal_places=2),
    )
    rows = (
        OrderItem.objects.filter(
            order__is_deleted=False,
            order__created_at__gte=start, order__created_at__lt=end,
        )
        # Cancelled orders never sold — keep them out of "top products today".
        .exclude(order__status='CANCELED')
        .annotate(line_total=line_total)
        .values('product_id', 'product__name')
        .annotate(
            quantity=Sum('quantity'),
            revenue=Sum('line_total'),
        )
        .order_by('-quantity')[:limit]
    )
    return [
        {
            'product_id': r['product_id'],
            'product_name': r['product__name'],
            'quantity': int(r['quantity'] or 0),
            'revenue': str(r['revenue'] or 0),
        }
        for r in rows
    ]


def _today_payment_breakdown():
    """Paid revenue today split by tender (NULL method counts as CASH)."""
    from base.models import Order
    start, end = _today_window()
    paid = Order.objects.filter(
        is_deleted=False, is_paid=True,
        created_at__gte=start, created_at__lt=end,
    ).exclude(status='CANCELED')
    agg = paid.aggregate(
        CASH=Coalesce(Sum('total_amount', filter=Q(payment_method='CASH') | Q(payment_method__isnull=True)),
                      Decimal('0.00'), output_field=DecimalField()),
        UZCARD=Coalesce(Sum('total_amount', filter=Q(payment_method='UZCARD')),
                        Decimal('0.00'), output_field=DecimalField()),
        HUMO=Coalesce(Sum('total_amount', filter=Q(payment_method='HUMO')),
                      Decimal('0.00'), output_field=DecimalField()),
        PAYME=Coalesce(Sum('total_amount', filter=Q(payment_method='PAYME')),
                       Decimal('0.00'), output_field=DecimalField()),
        MIXED=Coalesce(Sum('total_amount', filter=Q(payment_method='MIXED')),
                       Decimal('0.00'), output_field=DecimalField()),
    )
    return {m: _uzs(agg[m]) for m in _PAYMENT_METHODS}


def _today_category_stats():
    """Units + revenue today grouped by product category."""
    from base.models import OrderItem
    start, end = _today_window()
    line_total = ExpressionWrapper(
        F('price') * F('quantity'),
        output_field=DecimalField(max_digits=18, decimal_places=2),
    )
    rows = (
        OrderItem.objects.filter(
            order__is_deleted=False,
            order__created_at__gte=start, order__created_at__lt=end,
        )
        .exclude(order__status='CANCELED')
        .values('product__category_id', 'product__category__name')
        # Alias the unit count as `units`, NOT `quantity`: the revenue expression
        # references F('quantity'), and reusing the column name as an aggregate alias
        # makes Django resolve that F() to the aggregate -> "is an aggregate"
        # FieldError (silently emptied category stats on Postgres).
        .annotate(units=Sum('quantity'), revenue=Sum(line_total))
        .order_by('-revenue')
    )
    return [
        {
            'category_id': r['product__category_id'],
            'category': r['product__category__name'],
            'quantity': int(r['units'] or 0),
            'revenue': _uzs(r['revenue']),
        }
        for r in rows
    ]


def _today_units_sold():
    """Total product units sold today (excludes cancelled orders)."""
    from base.models import OrderItem
    start, end = _today_window()
    agg = OrderItem.objects.filter(
        order__is_deleted=False,
        order__created_at__gte=start, order__created_at__lt=end,
    ).exclude(order__status='CANCELED').aggregate(q=Sum('quantity'))
    return int(agg['q'] or 0)


def _today_peak_hour():
    """Hour (0-23) with the most orders today, or None if no orders yet."""
    from base.models import Order
    start, end = _today_window()
    rows = list(
        Order.objects.filter(
            is_deleted=False, created_at__gte=start, created_at__lt=end,
        )
        .annotate(hour=ExtractHour('created_at'))
        .values('hour').annotate(c=Count('id')).order_by('-c', 'hour')
    )
    return rows[0]['hour'] if rows else None


def _today_avg_prep_seconds():
    """Average kitchen prep time today: mean(ready_at - created_at) in seconds."""
    from base.models import Order
    start, end = _today_window()
    agg = Order.objects.filter(
        is_deleted=False, ready_at__isnull=False,
        created_at__gte=start, created_at__lt=end,
    ).exclude(status='CANCELED').aggregate(
        avg=Avg(ExpressionWrapper(F('ready_at') - F('created_at'),
                                  output_field=DurationField())),
    )
    return agg['avg'].total_seconds() if agg['avg'] else None


def _today_money_entered():
    """Cash the manager counted into the SAFE when closing shifts today
    (sum of today's reconciliation actual_cash) — the 'money entered when
    closing the shift' figure."""
    from base.models import CashReconciliation
    start, end = _today_window()
    agg = CashReconciliation.objects.filter(
        is_deleted=False, created_at__gte=start, created_at__lt=end,
    ).aggregate(total=Sum('actual_cash'))
    return _uzs(agg['total'])


def _low_stock_count():
    try:
        from stock.models import StockItem
        return StockItem.objects.filter(
            is_deleted=False, reorder_point__gt=0,
        ).annotate(
            total_qty=Sum('stock_levels__quantity'),
        ).filter(
            Q(total_qty__lt=F('reorder_point')) | Q(total_qty__isnull=True),
        ).count()
    except Exception:
        logger.exception('low-stock count failed')
        return None


def _clocked_in():
    try:
        from base.models import Shift
        shifts = (
            Shift.objects.filter(is_deleted=False, status='ACTIVE')
            .select_related('user')
            .order_by('start_time')
        )
        return [
            {
                'shift_id': s.id,
                'user_id': s.user_id,
                'name': (
                    f'{s.user.first_name} {s.user.last_name}'.strip()
                    if s.user else None
                ),
                'start_time': s.start_time.isoformat() if s.start_time else None,
            }
            for s in shifts
        ]
    except Exception:
        logger.exception('clocked-in fetch failed')
        return None


def get_today():
    """Bundle every today widget into one response dict.

    Each subkey can independently be None if its query failed; the
    front-end is expected to gracefully degrade.
    """
    revenue = _today_revenue()
    breakdown = _today_orders_breakdown()
    return {
        'today': {
            'revenue': revenue['revenue'],
            'paid_orders': revenue['paid_orders'],
            'orders': breakdown['orders'],
            'cancelled': breakdown['cancelled'],
            'open': breakdown['open'],
            # New: the operator-requested headline figures.
            'units_sold': _safe('today units_sold', _today_units_sold, 0),
            'peak_hour': _safe('today peak_hour', _today_peak_hour, None),
            'avg_prep_seconds': _safe('today avg_prep', _today_avg_prep_seconds, None),
            'money_entered': _safe('today money_entered', _today_money_entered, '0'),
        },
        'payment_breakdown_today': _safe(
            'today payment breakdown', _today_payment_breakdown,
            {m: '0' for m in _PAYMENT_METHODS}),
        'category_stats_today': _safe('today category stats', _today_category_stats, []),
        'top_products_today': _top_products_today(),
        'low_stock_count': _low_stock_count(),
        'clocked_in': _clocked_in(),
    }


def _range_window(date_from, date_to):
    """Parse YYYY-MM-DD from/to into an aware [start, end) BUSINESS-DAY window
    (defaults to the current business date, swapped if reversed; end is the next
    business-day cutover after `to` so the whole operating day is included)."""
    from datetime import datetime
    from base.services.business_day import business_date, range_window

    def _d(s):
        try:
            return datetime.strptime((s or '').strip(), '%Y-%m-%d').date()
        except (ValueError, TypeError, AttributeError):
            return None

    default_day = business_date()
    d_from = _d(date_from) or default_day
    d_to = _d(date_to) or default_day
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    start, end = range_window(d_from, d_to)
    return d_from, d_to, start, end


def get_range(date_from=None, date_to=None):
    """Date-range dashboard (GET /dashboard?from=&to=): the headline figures over
    an arbitrary [from, to] window (defaults to today)."""
    from base.models import Order, OrderItem
    d_from, d_to, start, end = _range_window(date_from, date_to)
    sold = Order.objects.filter(is_deleted=False, created_at__gte=start, created_at__lt=end)
    paid = sold.filter(is_paid=True).exclude(status='CANCELED')
    rev = paid.aggregate(total=Sum('total_amount'), n=Count('id'))
    counts = sold.aggregate(total=Count('id'),
                            cancelled=Count('id', filter=Q(status='CANCELED')))
    pay = paid.aggregate(
        CASH=Coalesce(Sum('total_amount', filter=Q(payment_method='CASH') | Q(payment_method__isnull=True)),
                      Decimal('0.00'), output_field=DecimalField()),
        UZCARD=Coalesce(Sum('total_amount', filter=Q(payment_method='UZCARD')),
                        Decimal('0.00'), output_field=DecimalField()),
        HUMO=Coalesce(Sum('total_amount', filter=Q(payment_method='HUMO')),
                      Decimal('0.00'), output_field=DecimalField()),
        PAYME=Coalesce(Sum('total_amount', filter=Q(payment_method='PAYME')),
                       Decimal('0.00'), output_field=DecimalField()),
        MIXED=Coalesce(Sum('total_amount', filter=Q(payment_method='MIXED')),
                       Decimal('0.00'), output_field=DecimalField()),
    )
    line_total = ExpressionWrapper(
        F('price') * F('quantity'),
        output_field=DecimalField(max_digits=18, decimal_places=2))
    items = OrderItem.objects.filter(
        order__is_deleted=False, order__created_at__gte=start, order__created_at__lt=end,
    ).exclude(order__status='CANCELED')
    units = items.aggregate(q=Sum('quantity'))['q'] or 0
    top = list(items.annotate(lt=line_total).values('product_id', 'product__name')
               .annotate(quantity=Sum('quantity'), revenue=Sum('lt'))
               .order_by('-quantity')[:5])
    # Category breakdown over the SAME range window (mirrors _today_category_stats).
    # Alias the count as `units`, NOT `quantity`: Sum(line_total) references
    # F('quantity'), so a `quantity` aggregate alias triggers an "is an aggregate"
    # FieldError on Postgres and silently empties the category stats.
    cat_rows = list(
        items.values('product__category_id', 'product__category__name')
        .annotate(units=Sum('quantity'), revenue=Sum(line_total))
        .order_by('-revenue'))
    return {
        'range': {'from': d_from.isoformat(), 'to': d_to.isoformat()},
        'revenue': _uzs(rev['total']),
        'paid_orders': rev['n'] or 0,
        'orders': counts['total'] or 0,
        'cancelled': counts['cancelled'] or 0,
        'units_sold': int(units),
        'payment_breakdown': {m: _uzs(pay[m]) for m in _PAYMENT_METHODS},
        'top_products': [{
            'product_id': r['product_id'], 'product_name': r['product__name'],
            'quantity': int(r['quantity'] or 0), 'revenue': _uzs(r['revenue']),
        } for r in top],
        # Executive-tab category stats for the selected range (same shape as
        # /dashboard/today's category_stats_today).
        'category_stats': [{
            'category_id': r['product__category_id'],
            'category': r['product__category__name'],
            'quantity': int(r['units'] or 0),
            'revenue': _uzs(r['revenue']),
        } for r in cat_rows],
    }


def get_sidebar_counts():
    """One-call sidebar counters — active shifts + today's order count + today's
    revenue — replacing two separate 90s polls."""
    from base.models import Order, Shift
    start, end = _today_window()
    active = _safe('sidebar active_shifts',
                   lambda: Shift.objects.filter(is_deleted=False, status='ACTIVE').count(), 0)
    today = _safe('sidebar today', lambda: Order.objects.filter(
        is_deleted=False, created_at__gte=start, created_at__lt=end).aggregate(
            orders=Count('id'),
            revenue=Coalesce(
                Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')),
                Decimal('0.00'), output_field=DecimalField()),
        ), {'orders': 0, 'revenue': Decimal('0')})
    return {
        'active_shifts': active,
        'today_orders': today['orders'] or 0,
        'today_revenue': _uzs(today['revenue']),
    }
