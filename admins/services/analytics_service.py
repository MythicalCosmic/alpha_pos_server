"""Operational analytics: shift performance + menu engineering.

Both surfaces are pure derivations over Order / OrderItem / Shift — no
new models. Designed to be lean enough for the manager view of the owner
mobile app to call frequently.
"""
import logging
from decimal import Decimal

from django.db.models import (
    Count, ExpressionWrapper, F, Q, Sum,
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
    from base.models import Order, OrderItem, OrderRefund

    start = shift.start_time
    end = shift.end_time or timezone.now()
    duration = end - start
    duration_minutes = max(int(duration.total_seconds() / 60), 0)

    operational = Order.objects.filter(
        is_deleted=False,
        cashier_id=shift.user_id,
        branch_id=shift.branch_id,
        created_at__gte=start, created_at__lt=end,
    )
    counts = operational.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status='COMPLETED')),
        cancelled=Count('id', filter=Q(status='CANCELED')),
    )
    settled = (
        Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            branch_id=shift.branch_id,
            is_paid=True,
            paid_at__gte=start, paid_at__lt=end,
        )
        .aggregate(paid=Count('id'), revenue=Sum('total_amount'))
    )
    refunds = OrderRefund.objects.filter(
        is_deleted=False,
        shift=shift,
        branch_id=shift.branch_id,
        refunded_at__gte=start,
        refunded_at__lt=end,
    ).aggregate(count=Count('id'), amount=Sum('amount'))

    # Avg prep = (ready_at - created_at) over ready/completed orders, in SQL
    # instead of materialising every order row just to compute a mean.
    from django.db.models import Avg, DurationField, ExpressionWrapper, F
    prep_avg = operational.filter(
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
    gross_revenue = settled['revenue'] or Decimal('0')
    refund_amount = refunds['amount'] or Decimal('0')
    revenue = gross_revenue - refund_amount

    hours = duration.total_seconds() / 3600 if duration_minutes else 0
    orders_per_hour = round(total / hours, 2) if hours else 0.0
    revenue_per_hour = (revenue / Decimal(hours)).quantize(Decimal('0.01')) if hours else Decimal('0')

    # Shift-detail "top products" must use the selected shift's cashier and its
    # real half-open settlement window. The previous client fallback queried a
    # broad date range, so another cashier's products (and unpaid/cancelled
    # baskets) could appear on this shift.
    from base.services.revenue import net_line_revenue
    top_rows = list(
        OrderItem.objects.filter(
            is_deleted=False,
            order__is_deleted=False,
            order__cashier_id=shift.user_id,
            order__branch_id=shift.branch_id,
            order__is_paid=True,
            order__paid_at__gte=start,
            order__paid_at__lt=end,
        )
        .exclude(order__status=Order.Status.CANCELED)
        .values('product_id', 'product__name')
        .annotate(
            total_quantity=Sum('quantity'),
            total_revenue=Sum(net_line_revenue()),
            order_count=Count('order_id', distinct=True),
        )
        .order_by('-total_quantity', 'product__name', 'product_id')[:5]
    )
    top_products = [{
        'product_id': row['product_id'],
        'product_name': row['product__name'],
        'name': row['product__name'],
        'total_quantity': int(row['total_quantity'] or 0),
        'quantity': int(row['total_quantity'] or 0),
        'total_revenue': str(row['total_revenue'] or 0),
        'revenue': str(row['total_revenue'] or 0),
        'order_count': row['order_count'],
    } for row in top_rows]

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
        'orders_paid': settled['paid'] or 0,
        'orders_refunded': refunds['count'] or 0,
        'cancel_rate_pct': cancel_rate,
        'gross_revenue': str(int(gross_revenue)),
        'refund_amount': str(int(refund_amount)),
        'revenue': str(int(revenue)),   # net revenue, integer so'm (UZS)
        'avg_prep_seconds': avg_prep_seconds,
        'orders_per_hour': orders_per_hour,
        'revenue_per_hour': str(revenue_per_hour),
        'top_products': top_products,
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
    from base.services.business_day import range_window
    from admins.services.refund_reporting import (
        net_grouped_items, refund_item_events,
    )

    lo, hi = range_window(date_from, date_to)

    sale_items = (
        OrderItem.objects.filter(
            is_deleted=False, order__is_deleted=False, order__is_paid=True,
            order__paid_at__gte=lo, order__paid_at__lt=hi,
        )
        # Exclude cancelled orders — they never sold, so they must not skew
        # menu-engineering quadrants (qty sold / revenue).
    )
    rows = net_grouped_items(
        sale_items,
        refund_item_events(lo, hi),
        ('product_id', 'product__name', 'product__price'),
    )

    items = []
    for r in rows:
        price = r['product__price'] or Decimal('0')
        qty = int(r['qty'] or 0)
        revenue = r['revenue'] or Decimal('0')
        # Discounts reduce realized margin. COGS remains based on the frozen
        # selling price, while net revenue is the canonical proportional
        # allocation from the order header.
        net_unit_revenue = (revenue / qty) if qty else Decimal('0')
        unit_cogs = price * cogs_fraction
        margin_per_unit = (net_unit_revenue - unit_cogs).quantize(Decimal('0.01'))
        margin_pct = (
            margin_per_unit / net_unit_revenue * 100
        ).quantize(Decimal('0.1')) if net_unit_revenue else Decimal('0')
        profit = (revenue - unit_cogs * qty).quantize(Decimal('0.01'))
        items.append({
            'product_id': r['product_id'],
            'product_name': r['product__name'],
            'price': str(price),
            'qty_sold': qty,
            'revenue': str(revenue),
            'gross_qty_sold': int(r['gross_qty'] or 0),
            'refunded_qty': int(r['refund_qty'] or 0),
            'gross_revenue': str(r['gross_revenue'] or 0),
            'refund_amount': str(r['refund_revenue'] or 0),
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


def staff_performance(date_from, date_to, tod_from=None, tod_to=None):
    """Per-staff KPIs over the BUSINESS-day window [date_from, date_to]: order
    volume, completion/cancellation, paid revenue, units sold, and shifts/hours
    worked. Powers the admin-panel Staff dashboard (GET /staff/performance?range=).
    """
    from base.models import Order, OrderItem, OrderRefund, Shift
    from base.services.business_day import range_window, tod_filter, parse_hhmm

    lo, hi = range_window(date_from, date_to)
    tf, tt = parse_hhmm(tod_from), parse_hhmm(tod_to)

    operational_rows = list(
        tod_filter(Order.objects.filter(
            is_deleted=False, cashier__isnull=False,
            created_at__gte=lo, created_at__lt=hi), tf, tt)
        .values('cashier_id', 'cashier__first_name', 'cashier__last_name', 'cashier__role')
        .annotate(
            orders_total=Count('id'),
            completed=Count('id', filter=Q(status='COMPLETED')),
            cancelled=Count('id', filter=Q(status='CANCELED')),
        )
    )

    # Settlement metrics have their own event clock. Keeping them on the
    # created-at queryset silently assigns a late payment to the day the ticket
    # was opened, which breaks revenue, AOV and tender reconciliation around the
    # business-day cutover.
    paid_rows = list(
        tod_filter(
            Order.objects.filter(
                is_deleted=False,
                cashier__isnull=False,
                is_paid=True,
                paid_at__gte=lo,
                paid_at__lt=hi,
            ),
            tf,
            tt,
            field='paid_at',
        )
        .values('cashier_id', 'cashier__first_name', 'cashier__last_name', 'cashier__role')
        .annotate(paid=Count('id'), revenue=Sum('total_amount'))
    )

    refund_rows = list(
        tod_filter(
            OrderRefund.objects.filter(
                is_deleted=False,
                cashier__isnull=False,
                refunded_at__gte=lo,
                refunded_at__lt=hi,
            ),
            tf,
            tt,
            field='refunded_at',
        )
        .values('cashier_id', 'cashier__first_name', 'cashier__last_name', 'cashier__role')
        .annotate(refunded=Count('id'), refund_amount=Sum('amount'))
    )

    units_map = {
        r['order__cashier_id']: int(r['u'] or 0)
        for r in (
            tod_filter(OrderItem.objects.filter(
                is_deleted=False, order__is_deleted=False, order__is_paid=True,
                order__paid_at__gte=lo, order__paid_at__lt=hi),
                tf, tt, field='order__paid_at')
            .values('order__cashier_id')
            .annotate(u=Sum('quantity'))
        )
    }
    from base.services.refund_lines import (
        REFUND_EVENT_ALIAS, refund_item_events, refund_line_quantity,
    )
    refund_unit_items = refund_item_events(
        cashier__isnull=False,
        refunded_at__gte=lo,
        refunded_at__lt=hi,
    )
    refund_unit_items = tod_filter(
        refund_unit_items, tf, tt,
        field=f'{REFUND_EVENT_ALIAS}__refunded_at',
    )
    refund_units_map = {
        r[f'{REFUND_EVENT_ALIAS}__cashier_id']: int(r['u'] or 0)
        for r in (
            refund_unit_items
            .values(f'{REFUND_EVENT_ALIAS}__cashier_id')
            .annotate(u=Sum(refund_line_quantity(REFUND_EVENT_ALIAS)))
        )
    }

    # Shifts that STARTED inside the window, with worked hours (open shifts run
    # up to "now"). Aggregated in Python so an open shift's running duration is
    # handled the same way shift_performance does it.
    shift_map = {}
    for s in Shift.objects.filter(is_deleted=False, start_time__gte=lo, start_time__lt=hi):
        end = s.end_time or timezone.now()
        secs = max((end - s.start_time).total_seconds(), 0)
        agg = shift_map.setdefault(s.user_id, {'shifts': 0, 'seconds': 0.0})
        agg['shifts'] += 1
        agg['seconds'] += secs

    operational_map = {r['cashier_id']: r for r in operational_rows}
    paid_map = {r['cashier_id']: r for r in paid_rows}
    refund_map = {r['cashier_id']: r for r in refund_rows}
    staff = []
    for cid in set(operational_map) | set(paid_map) | set(refund_map):
        operational_row = operational_map.get(cid, {})
        paid_row = paid_map.get(cid, {})
        refund_row = refund_map.get(cid, {})
        identity = operational_row or paid_row or refund_row
        orders_total = operational_row.get('orders_total') or 0
        cancelled = operational_row.get('cancelled') or 0
        paid = paid_row.get('paid') or 0
        gross_revenue = paid_row.get('revenue') or Decimal('0')
        refund_amount = refund_row.get('refund_amount') or Decimal('0')
        revenue = gross_revenue - refund_amount
        sm = shift_map.get(cid, {'shifts': 0, 'seconds': 0.0})
        avg_order = (revenue / paid).quantize(Decimal('0.01')) if paid else Decimal('0')
        staff.append({
            'user_id': cid,
            'name': (
                f"{identity.get('cashier__first_name') or ''} "
                f"{identity.get('cashier__last_name') or ''}"
            ).strip(),
            'role': identity.get('cashier__role'),
            'orders_total': orders_total,
            'orders_completed': operational_row.get('completed') or 0,
            'orders_cancelled': cancelled,
            'orders_paid': paid,
            'orders_refunded': refund_row.get('refunded') or 0,
            'cancel_rate_pct': round(cancelled / orders_total * 100, 2) if orders_total else 0.0,
            'gross_revenue': str(int(gross_revenue)),
            'refund_amount': str(int(refund_amount)),
            'revenue': str(int(revenue)),
            'avg_order_value': str(int(avg_order)),
            'gross_units_sold': units_map.get(cid, 0),
            'refunded_units': refund_units_map.get(cid, 0),
            'units_sold': units_map.get(cid, 0) - refund_units_map.get(cid, 0),
            'shifts_worked': sm['shifts'],
            'hours_worked': round(sm['seconds'] / 3600, 2),
        })

    staff.sort(
        key=lambda row: (
            -Decimal(row['revenue']),
            -row['orders_total'],
            row['user_id'],
        )
    )

    total_revenue = sum((Decimal(s['revenue']) for s in staff), Decimal('0'))
    total_orders = sum(s['orders_total'] for s in staff)
    return {
        'range': {'from': date_from.isoformat(), 'to': date_to.isoformat()},
        'window_days': (date_to - date_from).days + 1,
        'staff': staff,
        'summary': {
            'staff_count': len(staff),
            'total_orders': total_orders,
            'total_revenue': str(int(total_revenue)),
            'top_performer': staff[0]['name'] if staff else None,
        },
    }
