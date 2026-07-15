"""Fast, deterministic prep forecasting from recent paid-order history.

The old endpoint made a synchronous general-purpose LLM request. On the
production OpenAI configuration, even this small JSON answer could consume a
large reasoning/output budget and outlive the edge proxy, producing a 502.
Prep quantities are a statistical calculation, so this request stays local,
explainable, and bounded.
"""
import math
from datetime import timedelta

from django.db.models import Exists, F, OuterRef, Sum
from django.utils import timezone


WINDOW_DAYS = 30
DEFAULT_TOP_N = 15


def gather_history(days=WINDOW_DAYS, top_n=DEFAULT_TOP_N):
    """Build the per-product, weekday, and hour aggregate used by forecasting."""
    from base.models import Order, OrderItem, OrderRefund

    cutoff = timezone.now() - timedelta(days=days)

    # Prep demand follows the original paid-sale cohort, not the dated refund
    # ledger used by revenue reports. A terminal cancellation removes the
    # original basket at its paid_at clock. Provider/tender refunds are money
    # adjustments and do not change how much kitchen demand occurred.
    terminal_cancellation = OrderRefund.objects.filter(
        order_id=OuterRef('order_id'),
        is_deleted=False,
        source=OrderRefund.Source.ORDER_CANCEL,
    )
    demand_items = (
        OrderItem.objects.filter(
            is_deleted=False,
            order__is_deleted=False,
            order__is_paid=True,
            order__paid_at__gte=cutoff,
        )
        .annotate(_terminally_cancelled=Exists(terminal_cancellation))
        .filter(_terminally_cancelled=False)
        # Legacy paid cancellations may predate the immutable refund ledger.
        .exclude(order__status=Order.Status.CANCELED)
    )

    top = (
        demand_items
        .values('product_id', 'product__name')
        .annotate(total_qty=Sum('quantity'))
        .order_by('-total_qty')
    )
    name_by_id = {row['product_id']: row['product__name'] for row in top}
    totals_by_id = {row['product_id']: int(row['total_qty'] or 0) for row in top}
    top_ids = [
        product_id
        for product_id, quantity in sorted(
            totals_by_id.items(), key=lambda item: (-item[1], item[0]),
        )
        if quantity > 0
    ][:top_n]
    totals_by_id = {product_id: totals_by_id[product_id] for product_id in top_ids}
    name_by_id = {product_id: name_by_id[product_id] for product_id in top_ids}

    # One aggregate keyed on (product, weekday, hour), avoiding an N+1 query.
    breakdown_rows = (
        demand_items.filter(product_id__in=top_ids)
        .values(
            'product_id',
            weekday=F('order__paid_at__week_day'),
            hour=F('order__paid_at__hour'),
        )
        .annotate(qty=Sum('quantity'))
    ) if top_ids else []

    weekday_map = {
        1: 'Sun', 2: 'Mon', 3: 'Tue', 4: 'Wed',
        5: 'Thu', 6: 'Fri', 7: 'Sat',
    }
    per_product_weekday = {product_id: {} for product_id in top_ids}
    per_product_hour = {product_id: {} for product_id in top_ids}
    for cell in breakdown_rows:
        product_id = cell['product_id']
        quantity = int(cell['qty'] or 0)
        weekday = weekday_map.get(cell['weekday'], str(cell['weekday']))
        per_product_weekday[product_id][weekday] = (
            per_product_weekday[product_id].get(weekday, 0) + quantity
        )
        hour = str(cell['hour'])
        per_product_hour[product_id][hour] = (
            per_product_hour[product_id].get(hour, 0) + quantity
        )

    products = [
        {
            'id': product_id,
            'name': name_by_id[product_id],
            'total_qty': totals_by_id[product_id],
            'by_weekday': per_product_weekday[product_id],
            'by_hour': per_product_hour[product_id],
        }
        for product_id in top_ids
    ]
    return {'window_days': days, 'products': products}


def _weekday_occurrences(days, weekday, end_date):
    """Count ``weekday`` dates in the inclusive trailing-day window."""
    days = max(1, int(days or WINDOW_DAYS))
    start_date = end_date - timedelta(days=days - 1)
    return sum(
        1
        for offset in range(days)
        if (start_date + timedelta(days=offset)).weekday() == weekday
    )


def _local_predictions(history, tomorrow):
    """Blend tomorrow's weekday average with the overall daily average.

    Weekday demand receives 75% of the weight when weekday history exists;
    the overall average stabilises sparse products. A 5% prep buffer is rounded
    upward to whole units. Runtime is O(products), with no provider dependency.
    """
    days = max(1, int(history.get('window_days') or WINDOW_DAYS))
    weekday_key = tomorrow.strftime('%a')
    occurrences = max(
        1,
        _weekday_occurrences(
            days,
            tomorrow.weekday(),
            tomorrow - timedelta(days=1),
        ),
    )
    predictions = []
    for product in history.get('products') or []:
        by_weekday = product.get('by_weekday') or {}
        weekday_qty = max(0, int(by_weekday.get(weekday_key) or 0))
        total_qty = max(0, int(product.get('total_qty') or 0))
        # Tolerate older/cached history payloads that omitted total_qty.
        if not total_qty and by_weekday:
            total_qty = sum(
                max(0, int(quantity or 0))
                for quantity in by_weekday.values()
            )

        daily_average = total_qty / days
        if weekday_qty:
            weekday_average = weekday_qty / occurrences
            expected = (weekday_average * 0.75) + (daily_average * 0.25)
            reason = (
                f'{weekday_key} average {weekday_average:.1f}; '
                f'{days}-day average {daily_average:.1f}; 5% buffer'
            )
        else:
            expected = daily_average
            reason = f'{days}-day average {daily_average:.1f}; 5% buffer'

        predictions.append({
            'product_id': product.get('id'),
            'product_name': product.get('name') or '',
            'suggested_qty': math.ceil(expected * 1.05) if expected > 0 else 0,
            'reason': reason,
        })
    return predictions


def forecast_tomorrow():
    """Return a bounded local forecast as ``(data, error)``."""
    history = gather_history()

    # Forecast the next operating day, not UTC now + 24h. Around midnight in
    # Tashkent the UTC calendar may still be yesterday; before the cutover the
    # current operating day is intentionally still the previous date.
    from base.services.business_day import business_date

    tomorrow = business_date() + timedelta(days=1)
    base = {
        'tomorrow': tomorrow.isoformat(),
        'predictions': _local_predictions(history, tomorrow),
        'method': 'historical_weekday_blend',
        'window_days': int(history.get('window_days') or WINDOW_DAYS),
    }
    if not history.get('products'):
        base['reason'] = 'no_history'
    return base, None
