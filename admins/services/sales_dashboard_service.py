"""Sales dashboard (admin panel): revenue/expense series, last-period comparison,
hour-of-week heatmap, and per-day channel mix.

All windows bound on the BUSINESS day (AppSettings.business_day_start, default
03:00) — a 01:00 sale counts toward the night before. Pure derivations over
Order / OrderItem / CashboxExpense; no new models.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

# Margin proxy: most products have no recipe/cost link, so true COGS is unknown.
# grossMargin is reported as 1 - this fraction until per-product costs are wired.
DEFAULT_COGS_FRACTION = Decimal('0.35')

HM_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
HM_HOURS = [f'{hour:02d}' for hour in (
    list(range(7, 24)) + [0, 1, 2]
)]
_HOUR_INDEX = {int(h): i for i, h in enumerate(HM_HOURS)}


def _uzs(value):
    try:
        return str(int(value or 0))
    except (TypeError, ValueError):
        return '0'


def _days_in(d_from, d_to):
    out, d = [], d_from
    while d <= d_to:
        out.append(d)
        d += timedelta(days=1)
    return out


def resolve_range(range_token=None, date_from=None, date_to=None):
    """Resolve the requested window to (d_from, d_to) on the BUSINESS calendar.
    Tokens: today, 7d, 30d, <N>d; or explicit from/to (YYYY-MM-DD)."""
    from base.services.business_day import business_date
    today = business_date()
    if date_from and date_to:
        try:
            d0 = datetime.strptime(date_from.strip(), '%Y-%m-%d').date()
            d1 = datetime.strptime(date_to.strip(), '%Y-%m-%d').date()
        except (ValueError, TypeError, AttributeError):
            return today, today
        return (d1, d0) if d1 < d0 else (d0, d1)
    tok = (range_token or '30d').strip().lower()
    if tok == 'today':
        return today, today
    n = 30
    if tok.endswith('d') and tok[:-1].isdigit():
        n = max(1, min(int(tok[:-1]), 366))
    return today - timedelta(days=n - 1), today


def _series(d_from, d_to, tod_from=None, tod_to=None, *, window=None):
    """Aligned revenue/expense/channel series over one resolved window.

    Business mode has one bucket per selected operating date. Exact/custom mode
    uses consecutive 24-hour buckets from ``start_at`` plus one final partial
    bucket. Its immediately preceding equal-duration window therefore always
    produces the same number of points even when it crosses a different number
    of calendar dates.
    """
    from base.models import Order
    from cashbox.models import CashboxExpense
    from base.services.business_day import business_day_start, range_window, tod_filter

    start = business_day_start()
    offset = timedelta(hours=start.hour, minutes=start.minute, seconds=start.second)
    lo, hi = (
        (window.start_at, window.end_at)
        if window is not None else range_window(d_from, d_to)
    )
    custom = window is not None and window.mode == 'custom'
    if custom:
        bucket_starts = []
        cursor = lo
        while cursor < hi:
            bucket_starts.append(cursor)
            cursor += timedelta(days=1)
        labels = [timezone.localtime(value).isoformat() for value in bucket_starts]

        def bucket_index(moment):
            elapsed = (moment - lo).total_seconds()
            index = int(elapsed // timedelta(days=1).total_seconds())
            return index if 0 <= index < len(labels) else None
    else:
        days = _days_in(d_from, d_to)
        labels = [day.isoformat() for day in days]
        idx = {day: i for i, day in enumerate(days)}

        def bucket_index(moment):
            local = timezone.localtime(moment)
            return idx.get((local - offset).date())

    revenue = [Decimal('0.00')] * len(labels)
    gross_revenue = [Decimal('0.00')] * len(labels)
    refunds = [Decimal('0.00')] * len(labels)
    expense = [Decimal('0.00')] * len(labels)
    channels = [{'hall': 0, 'delivery': 0, 'pickup': 0} for _ in labels]
    heat = [[0] * len(HM_HOURS) for _ in HM_DAYS]
    _chan = {'HALL': 'hall', 'DELIVERY': 'delivery', 'PICKUP': 'pickup'}

    def scoped(qs, field):
        if window is not None:
            return window.filter(qs, field)
        return tod_filter(qs, tod_from, tod_to, field=field)

    # Operational channel/heat activity is placed by created_at.
    _oqs = scoped(Order.objects.filter(
        is_deleted=False, created_at__gte=lo, created_at__lt=hi,
    ), 'created_at')
    for created_at, otype, status in (
        _oqs.values_list('created_at', 'order_type', 'status')
    ):
        local = timezone.localtime(created_at)
        i = bucket_index(created_at)
        if i is None:
            continue
        cancelled = status == 'CANCELED'
        if not cancelled:
            ch = _chan.get(otype)
            if ch:
                channels[i][ch] += 1
            # heatmap by clock weekday (Mon=0) + hour, hours 09-22 only
            hcol = _HOUR_INDEX.get(local.hour)
            if hcol is not None:
                heat[local.weekday()][hcol] += 1

    # Revenue is a settlement event. A ticket opened before the cutover but
    # paid afterwards belongs to the later money bucket, not its placement day.
    _pqs = scoped(Order.objects.filter(
        is_deleted=False, is_paid=True,
        paid_at__gte=lo, paid_at__lt=hi,
    ), 'paid_at')
    for paid_at, total in _pqs.values_list('paid_at', 'total_amount'):
        i = bucket_index(paid_at)
        if i is not None:
            revenue[i] += (total or Decimal('0'))
            gross_revenue[i] += (total or Decimal('0'))

    from admins.services.refund_reporting import refund_events
    refund_qs = refund_events(lo, hi)
    if window is not None:
        refund_qs = window.filter(refund_qs, 'refunded_at')
    else:
        refund_qs = tod_filter(
            refund_qs, tod_from, tod_to, field='refunded_at',
        )
    for refunded_at, amount in refund_qs.values_list(
        'refunded_at', 'amount',
    ):
        i = bucket_index(refunded_at)
        if i is not None:
            value = amount or Decimal('0')
            revenue[i] -= value
            refunds[i] += value

    _eqs = scoped(CashboxExpense.objects.filter(
        is_deleted=False, created_at__gte=lo, created_at__lt=hi,
    ), 'created_at')
    for created_at, amount in _eqs.values_list('created_at', 'amount'):
        i = bucket_index(created_at)
        if i is not None:
            expense[i] += (amount or Decimal('0'))

    return labels, revenue, gross_revenue, refunds, expense, channels, heat


def _series_hourly(bday, tod_from=None, tod_to=None):
    """Per-HOUR revenue/expense/channels for ONE business day, for a single-day
    (granularity=hour) selection. Buckets cover the 20 operating hours ordered
    07:00..02:00, so the quiet 03:00-07:00 gap is never charted."""
    from base.models import Order
    from cashbox.models import CashboxExpense
    from base.services.business_day import (
        business_day_start, day_window, business_day_hour_order, tod_filter)

    start = business_day_start()
    lo, hi = day_window(bday, start)
    hours = business_day_hour_order(start)
    hpos = {h: i for i, h in enumerate(hours)}
    labels = [f'{h:02d}:00' for h in hours]

    revenue = [Decimal('0.00')] * len(hours)
    gross_revenue = [Decimal('0.00')] * len(hours)
    refunds = [Decimal('0.00')] * len(hours)
    expense = [Decimal('0.00')] * len(hours)
    channels = [
        {'hall': 0, 'delivery': 0, 'pickup': 0} for _ in hours
    ]
    heat = [[0] * len(HM_HOURS) for _ in HM_DAYS]
    _chan = {'HALL': 'hall', 'DELIVERY': 'delivery', 'PICKUP': 'pickup'}

    _oqs = tod_filter(Order.objects.filter(
        is_deleted=False, created_at__gte=lo, created_at__lt=hi), tod_from, tod_to)
    for created_at, otype, status in _oqs.values_list(
            'created_at', 'order_type', 'status'):
        local = timezone.localtime(created_at)
        i = hpos.get(local.hour)
        if i is None:
            continue
        cancelled = status == 'CANCELED'
        if not cancelled:
            ch = _chan.get(otype)
            if ch:
                channels[i][ch] += 1
            hcol = _HOUR_INDEX.get(local.hour)
            if hcol is not None:
                heat[local.weekday()][hcol] += 1

    _pqs = tod_filter(Order.objects.filter(
        is_deleted=False, is_paid=True,
        paid_at__gte=lo, paid_at__lt=hi,
    ), tod_from, tod_to, field='paid_at')
    for paid_at, total in _pqs.values_list('paid_at', 'total_amount'):
        i = hpos.get(timezone.localtime(paid_at).hour)
        if i is not None:
            revenue[i] += (total or Decimal('0'))
            gross_revenue[i] += (total or Decimal('0'))

    from admins.services.refund_reporting import refund_events
    for refunded_at, amount in refund_events(
        lo, hi, tod_from=tod_from, tod_to=tod_to,
    ).values_list('refunded_at', 'amount'):
        i = hpos.get(timezone.localtime(refunded_at).hour)
        if i is not None:
            value = amount or Decimal('0')
            revenue[i] -= value
            refunds[i] += value

    _eqs = tod_filter(CashboxExpense.objects.filter(
        is_deleted=False, created_at__gte=lo, created_at__lt=hi), tod_from, tod_to)
    for created_at, amount in _eqs.values_list('created_at', 'amount'):
        i = hpos.get(timezone.localtime(created_at).hour)
        if i is not None:
            expense[i] += (amount or Decimal('0'))

    return labels, revenue, gross_revenue, refunds, expense, channels, heat


def sales_dashboard(range_token=None, date_from=None, date_to=None,
                    tod_from=None, tod_to=None, granularity=None,
                    datetime_from=None, datetime_to=None,
                    from_at=None, to_at=None):
    from base.services.business_day import resolve_reporting_window
    # Range tokens are simply a convenient way to choose operating dates; ISO
    # datetime parameters always take precedence and remain exact.
    if date_from not in (None, '') or date_to not in (None, ''):
        # Preserve the raw explicit values so the canonical resolver can reject
        # malformed dates instead of resolve_range silently falling back today.
        resolved_from, resolved_to = date_from, date_to
    else:
        resolved_from, resolved_to = resolve_range(range_token)
    window = resolve_reporting_window(
        resolved_from, resolved_to,
        tod_from=tod_from, tod_to=tod_to,
        datetime_from=datetime_from, datetime_to=datetime_to,
        from_at=from_at, to_at=to_at,
    )
    d_from, d_to = window.date_from, window.date_to

    # granularity=hour on a SINGLE business day -> per-HOUR buckets so the chart can
    # draw a 24-point line (one daily point can't). Otherwise per-business-day.
    hourly = (
        (granularity or '').strip().lower() == 'hour'
        and d_from == d_to
        and window.mode == 'business'
    )
    if hourly:
        labels, revenue, gross_revenue, refunds, expense, channels, heat = _series_hourly(d_from)
        # Comparison line = the PREVIOUS business day's hourly revenue (day-over-day).
        previous_window = window.previous()
        prev_labels, prev_rev, _, _, prev_expense, prev_channels, _ = _series_hourly(
            previous_window.date_from,
        )
        span = len(labels)
        day_labels = labels
        channel_labels = labels
    else:
        day_labels, revenue, gross_revenue, refunds, expense, channels, heat = _series(
            d_from, d_to, window=window,
        )
        span = len(day_labels)
        previous_window = window.previous()
        prev_labels, prev_rev, _, _, prev_expense, prev_channels, _ = _series(
            previous_window.date_from,
            previous_window.date_to,
            window=previous_window,
        )
        channel_labels = day_labels

    month_revenue = sum(revenue, Decimal('0.00'))
    month_gross_revenue = sum(gross_revenue, Decimal('0.00'))
    refund_amount = sum(refunds, Decimal('0.00'))
    return {
        'range': window.metadata(
            days=span, granularity='hour' if hourly else 'day',
        ),
        'monthRevenue': _uzs(month_revenue),
        'monthGrossRevenue': _uzs(month_gross_revenue),
        'refundAmount': _uzs(refund_amount),
        # Estimated gross margin (1 - assumed COGS fraction) until recipe costs wired.
        'grossMargin': float(Decimal('1') - DEFAULT_COGS_FRACTION),
        'revenue30': [_uzs(v) for v in revenue],
        'grossRevenue30': [_uzs(v) for v in gross_revenue],
        'refund30': [_uzs(v) for v in refunds],
        'expense30': [_uzs(v) for v in expense],
        'lastMonthRev': [_uzs(v) for v in prev_rev],
        'dayLabels': day_labels,
        'HM_DAYS': HM_DAYS,
        'HM_HOURS': HM_HOURS,
        'heatMatrix': heat,
        'channelDays': [{
            'day': channel_labels[i],
            'hall': channels[i]['hall'],
            'delivery': channels[i]['delivery'],
            'pickup': channels[i]['pickup'],
        } for i in range(span)],
        'previous_period': {
            'range': previous_window.metadata(
                days=len(prev_labels), granularity='hour' if hourly else 'day',
            ),
            'revenue': _uzs(sum(prev_rev, Decimal('0.00'))),
            'expenses': _uzs(sum(prev_expense, Decimal('0.00'))),
            'orders': sum(
                row['hall'] + row['delivery'] + row['pickup']
                for row in prev_channels
            ),
            'labels': prev_labels,
            'revenue_series': [_uzs(value) for value in prev_rev],
            'expense_series': [_uzs(value) for value in prev_expense],
            'order_series': [
                row['hall'] + row['delivery'] + row['pickup']
                for row in prev_channels
            ],
        },
    }


def sales_expenses(date_from=None, date_to=None, tod_from=None, tod_to=None,
                   datetime_from=None, datetime_to=None,
                   from_at=None, to_at=None, page=1, per_page=50):
    """Itemized CashboxExpense rows for the exact Sales dashboard window."""
    from django.core.paginator import Paginator
    from django.db.models import Sum
    from base.services.business_day import resolve_reporting_window
    from cashbox.models import CashboxExpense

    window = resolve_reporting_window(
        date_from, date_to,
        tod_from=tod_from, tod_to=tod_to,
        datetime_from=datetime_from, datetime_to=datetime_to,
        from_at=from_at, to_at=to_at,
    )
    qs = (
        window.filter(CashboxExpense.objects.filter(
            is_deleted=False,
        ), 'created_at')
        .select_related('category', 'shift__user')
        .order_by('-created_at', '-id')
    )
    total = qs.aggregate(value=Sum('amount'))['value'] or Decimal('0')
    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(page)
    expenses = []
    for expense in page_obj.object_list:
        cashier = expense.shift.user if expense.shift_id and expense.shift else None
        expenses.append({
            'id': expense.id,
            'amount': _uzs(expense.amount),
            'category': expense.category.name if expense.category_id else None,
            'comment': CashboxExpense.visible_comment(expense.comment),
            'created_at': expense.created_at.isoformat(),
            'shift_id': expense.shift_id,
            'cashier_name': (
                f'{cashier.first_name} {cashier.last_name}'.strip()
                if cashier else None
            ),
        })
    return {
        'range': window.metadata(),
        'total_expense': _uzs(total),
        'expenses': expenses,
        'pagination': {
            'page': page_obj.number,
            'per_page': per_page,
            'total': paginator.count,
            'pages': paginator.num_pages,
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
        },
    }
