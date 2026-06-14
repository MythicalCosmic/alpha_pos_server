"""Operational analytics: shift performance + menu engineering.

Both surfaces are pure derivations over Order / OrderItem / Shift — no
new models. Designed to be lean enough for the manager view of the owner
mobile app to call frequently.
"""
import logging
from decimal import Decimal

from django.db.models import (
    Count, DecimalField, ExpressionWrapper, F, Q, Sum,
)
from django.utils import timezone

logger = logging.getLogger(__name__)

# When a product has no recipe/cost link, we can't compute true margin.
# This is the assumed COGS fraction used so the matrix still classifies
# the product instead of dropping it. Tunable per deployment if you
# eventually wire the recipe-cost path through.
DEFAULT_COGS_FRACTION = Decimal('0.35')


def shift_performance(shift):
    """Return per-shift KPIs derived from orders during the shift window."""
    from base.models import Order

    start = shift.start_time
    end = shift.end_time or timezone.now()
    duration = end - start
    duration_minutes = max(int(duration.total_seconds() / 60), 0)

    qs = Order.objects.filter(
        is_deleted=False,
        cashier_id=shift.user_id,
        created_at__gte=start, created_at__lte=end,
    )
    counts = qs.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status='COMPLETED')),
        cancelled=Count('id', filter=Q(status='CANCELED')),
        paid=Count('id', filter=Q(is_paid=True)),
        revenue=Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')),
    )

    # Avg prep = (ready_at - created_at) over ready/completed orders, in SQL
    # instead of materialising every order row just to compute a mean.
    from django.db.models import Avg, DurationField, ExpressionWrapper, F
    prep_avg = qs.filter(
        ready_at__isnull=False, status__in=['READY', 'COMPLETED'],
    ).aggregate(
        avg=Avg(ExpressionWrapper(
            F('ready_at') - F('created_at'),
            output_field=DurationField(),
        )),
    )['avg']
    avg_prep_seconds = int(prep_avg.total_seconds()) if prep_avg else None

    total = counts['total'] or 0
    cancelled = counts['cancelled'] or 0
    cancel_rate = round(cancelled / total * 100, 2) if total else 0.0
    revenue = counts['revenue'] or Decimal('0')

    hours = duration.total_seconds() / 3600 if duration_minutes else 0
    orders_per_hour = round(total / hours, 2) if hours else 0.0
    revenue_per_hour = (revenue / Decimal(hours)).quantize(Decimal('0.01')) if hours else Decimal('0')

    return {
        'shift_id': shift.id,
        'user_id': shift.user_id,
        'user_name': (
            f'{shift.user.first_name} {shift.user.last_name}'.strip()
            if shift.user else None
        ),
        'status': shift.status,
        'start_time': start.isoformat(),
        'end_time': shift.end_time.isoformat() if shift.end_time else None,
        'duration_minutes': duration_minutes,
        'orders_total': total,
        'orders_completed': counts['completed'] or 0,
        'orders_cancelled': cancelled,
        'orders_paid': counts['paid'] or 0,
        'cancel_rate_pct': cancel_rate,
        'revenue': str(int(revenue)),   # integer so'm (UZS), backend-independent
        'avg_prep_seconds': avg_prep_seconds,
        'orders_per_hour': orders_per_hour,
        'revenue_per_hour': str(revenue_per_hour),
    }


def menu_engineering(date_from, date_to, cogs_fraction=DEFAULT_COGS_FRACTION):
    """Star/Plowhorse/Puzzle/Dog classification per product over a window.

    Popularity axis = total quantity sold over the window.
    Margin axis    = selling_price - (assumed COGS).

    Both axes are split at the mean. "High" = at-or-above the mean,
    "Low" = below. This intentionally avoids ABC-style cutoffs so a
    small menu still produces a meaningful matrix.

    Without a recipe/cost linkage on Product, true margin requires a
    proxy. cogs_fraction (default 0.35) is the assumed cost-of-goods as
    a fraction of price — tunable per request so the operator can stress-
    test what-ifs.
    """
    from base.models import OrderItem

    line_total = ExpressionWrapper(
        F('price') * F('quantity'),
        output_field=DecimalField(max_digits=18, decimal_places=2),
    )
    rows = (
        OrderItem.objects.filter(
            order__is_deleted=False,
            order__created_at__date__gte=date_from,
            order__created_at__date__lte=date_to,
        )
        # Exclude cancelled orders — they never sold, so they must not skew
        # menu-engineering quadrants (qty sold / revenue).
        .exclude(order__status='CANCELED')
        .annotate(line_total=line_total)
        .values('product_id', 'product__name', 'product__price')
        .annotate(
            qty_sold=Sum('quantity'),
            revenue=Sum('line_total'),
        )
    )

    items = []
    for r in rows:
        price = r['product__price'] or Decimal('0')
        qty = int(r['qty_sold'] or 0)
        revenue = r['revenue'] or Decimal('0')
        margin_per_unit = (price * (Decimal('1') - cogs_fraction)).quantize(Decimal('0.01'))
        margin_pct = (margin_per_unit / price * 100).quantize(Decimal('0.1')) if price else Decimal('0')
        profit = (margin_per_unit * qty).quantize(Decimal('0.01'))
        items.append({
            'product_id': r['product_id'],
            'product_name': r['product__name'],
            'price': str(price),
            'qty_sold': qty,
            'revenue': str(revenue),
            'margin_per_unit': str(margin_per_unit),
            'margin_pct': str(margin_pct),
            'profit': profit,
        })

    if not items:
        return {
            'items': [],
            'summary': {
                'cogs_fraction': str(cogs_fraction),
                'window_days': (date_to - date_from).days + 1,
                'stars': 0, 'plowhorses': 0, 'puzzles': 0, 'dogs': 0,
                'avg_qty': 0, 'avg_margin_per_unit': '0',
            },
        }

    avg_qty = sum(i['qty_sold'] for i in items) / len(items)
    # Use absolute margin per unit, not margin_pct. With a flat cogs
    # fraction, margin_pct is the same for every product (= 1 - cogs)
    # and the matrix collapses to a single bucket. Per-unit cash
    # margin separates a 100,000-so'm item from a 5,000-so'm item.
    avg_margin = sum(Decimal(i['margin_per_unit']) for i in items) / len(items)

    counts = {'Star': 0, 'Plowhorse': 0, 'Puzzle': 0, 'Dog': 0}
    for item in items:
        high_pop = item['qty_sold'] >= avg_qty
        high_margin = Decimal(item['margin_per_unit']) >= avg_margin
        if high_pop and high_margin:
            klass = 'Star'
        elif high_pop and not high_margin:
            klass = 'Plowhorse'
        elif not high_pop and high_margin:
            klass = 'Puzzle'
        else:
            klass = 'Dog'
        item['class'] = klass
        item['profit'] = str(item['profit'])
        counts[klass] += 1

    items.sort(key=lambda x: (x['class'] != 'Star', -int(x['qty_sold'])))

    return {
        'items': items,
        'summary': {
            'cogs_fraction': str(cogs_fraction),
            'window_days': (date_to - date_from).days + 1,
            'stars': counts['Star'],
            'plowhorses': counts['Plowhorse'],
            'puzzles': counts['Puzzle'],
            'dogs': counts['Dog'],
            'avg_qty': round(avg_qty, 2),
            'avg_margin_per_unit': str(avg_margin.quantize(Decimal('0.01'))),
        },
    }
