"""Sales dashboard (admin panel): revenue/expense series, last-period comparison,
hour-of-week heatmap, and per-day channel mix.

All windows bound on the BUSINESS day (AppSettings.business_day_start, default
03:00) — a 01:00 sale counts toward the night before. Pure derivations over
Order / OrderItem / CashboxExpense; no new models.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone

# Margin proxy: most products have no recipe/cost link, so true COGS is unknown.
# grossMargin is reported as 1 - this fraction until per-product costs are wired.
DEFAULT_COGS_FRACTION = Decimal('0.35')

_LINE_TOTAL = ExpressionWrapper(
    F('price') * F('quantity'),
    output_field=DecimalField(max_digits=18, decimal_places=2),
)

HM_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
HM_HOURS = ['09', '10', '11', '12', '13', '14', '15', '16',
            '17', '18', '19', '20', '21', '22']
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


def _series(d_from, d_to, tod_from=None, tod_to=None):
    """Per-business-day revenue + expense + channel counts + the hour-of-week
    heatmap, over [d_from, d_to]. One pass over orders, one over expenses.
    tod_from/tod_to restrict to a working-hours window within each day."""
    from base.models import Order
    from cashbox.models import CashboxExpense
    from base.services.business_day import business_day_start, range_window, tod_filter

    start = business_day_start()
    offset = timedelta(hours=start.hour, minutes=start.minute, seconds=start.second)
    lo, hi = range_window(d_from, d_to)
    days = _days_in(d_from, d_to)
    idx = {d: i for i, d in enumerate(days)}

    revenue = [Decimal('0.00')] * len(days)
    expense = [Decimal('0.00')] * len(days)
    channels = [{'hall': 0, 'delivery': 0, 'pickup': 0} for _ in days]
    heat = [[0] * len(HM_HOURS) for _ in HM_DAYS]
    _chan = {'HALL': 'hall', 'DELIVERY': 'delivery', 'PICKUP': 'pickup'}

    # Orders: revenue (paid, non-cancelled) by business day; channel mix + heatmap
    # over ALL non-cancelled orders by placement time.
    _oqs = tod_filter(Order.objects.filter(
        is_deleted=False, created_at__gte=lo, created_at__lt=hi), tod_from, tod_to)
    for created_at, total, method, otype, is_paid, status in (
        _oqs.values_list('created_at', 'total_amount', 'payment_method',
                         'order_type', 'is_paid', 'status')
    ):
        local = timezone.localtime(created_at)
        bday = (local - offset).date()
        i = idx.get(bday)
        if i is None:
            continue
        cancelled = status == 'CANCELED'
        if is_paid and not cancelled:
            revenue[i] += (total or Decimal('0'))
        if not cancelled:
            ch = _chan.get(otype)
            if ch:
                channels[i][ch] += 1
            # heatmap by clock weekday (Mon=0) + hour, hours 09-22 only
            hcol = _HOUR_INDEX.get(local.hour)
            if hcol is not None:
                heat[local.weekday()][hcol] += 1

    _eqs = tod_filter(CashboxExpense.objects.filter(
        is_deleted=False, created_at__gte=lo, created_at__lt=hi), tod_from, tod_to)
    for created_at, amount in _eqs.values_list('created_at', 'amount'):
        bday = (timezone.localtime(created_at) - offset).date()
        i = idx.get(bday)
        if i is not None:
            expense[i] += (amount or Decimal('0'))

    return days, revenue, expense, channels, heat


def _series_hourly(bday, tod_from=None, tod_to=None):
    """Per-HOUR revenue/expense/channels for ONE business day, for a single-day
    (granularity=hour) selection. The 24 buckets are ordered starting at the
    business-day cutover (labels '03:00'..'02:00'), so a single-day chart draws a
    24-point line instead of one dot."""
    from base.models import Order
    from cashbox.models import CashboxExpense
    from base.services.business_day import (
        business_day_start, day_window, business_day_hour_order, tod_filter)

    start = business_day_start()
    lo, hi = day_window(bday, start)
    hours = business_day_hour_order(start)          # e.g. [3,4,...,23,0,1,2]
    hpos = {h: i for i, h in enumerate(hours)}
    labels = [f'{h:02d}:00' for h in hours]

    revenue = [Decimal('0.00')] * 24
    expense = [Decimal('0.00')] * 24
    channels = [{'hall': 0, 'delivery': 0, 'pickup': 0} for _ in range(24)]
    heat = [[0] * len(HM_HOURS) for _ in HM_DAYS]
    _chan = {'HALL': 'hall', 'DELIVERY': 'delivery', 'PICKUP': 'pickup'}

    _oqs = tod_filter(Order.objects.filter(
        is_deleted=False, created_at__gte=lo, created_at__lt=hi), tod_from, tod_to)
    for created_at, total, otype, is_paid, status in _oqs.values_list(
            'created_at', 'total_amount', 'order_type', 'is_paid', 'status'):
        local = timezone.localtime(created_at)
        i = hpos.get(local.hour)
        if i is None:
            continue
        cancelled = status == 'CANCELED'
        if is_paid and not cancelled:
            revenue[i] += (total or Decimal('0'))
        if not cancelled:
            ch = _chan.get(otype)
            if ch:
                channels[i][ch] += 1
            hcol = _HOUR_INDEX.get(local.hour)
            if hcol is not None:
                heat[local.weekday()][hcol] += 1

    _eqs = tod_filter(CashboxExpense.objects.filter(
        is_deleted=False, created_at__gte=lo, created_at__lt=hi), tod_from, tod_to)
    for created_at, amount in _eqs.values_list('created_at', 'amount'):
        i = hpos.get(timezone.localtime(created_at).hour)
        if i is not None:
            expense[i] += (amount or Decimal('0'))

    return labels, revenue, expense, channels, heat


def sales_dashboard(range_token=None, date_from=None, date_to=None,
                    tod_from=None, tod_to=None, granularity=None):
    from base.services.business_day import parse_hhmm
    d_from, d_to = resolve_range(range_token, date_from, date_to)
    tf, tt = parse_hhmm(tod_from), parse_hhmm(tod_to)

    # granularity=hour on a SINGLE business day -> per-HOUR buckets so the chart can
    # draw a 24-point line (one daily point can't). Otherwise per-business-day.
    hourly = (granularity or '').strip().lower() == 'hour' and d_from == d_to
    if hourly:
        labels, revenue, expense, channels, heat = _series_hourly(d_from, tf, tt)
        # Comparison line = the PREVIOUS business day's hourly revenue (day-over-day).
        _, prev_rev, _, _, _ = _series_hourly(d_from - timedelta(days=1), tf, tt)
        span = 24
        day_labels = labels                 # "03:00".."02:00"
        channel_labels = labels
    else:
        days, revenue, expense, channels, heat = _series(d_from, d_to, tf, tt)
        span = len(days)
        # Preceding window of equal length, aligned day-for-day, for the comparison line.
        prev_from = d_from - timedelta(days=span)
        prev_to = d_from - timedelta(days=1)
        _, prev_rev, _, _, _ = _series(prev_from, prev_to, tf, tt)
        day_labels = [d.isoformat() for d in days]
        channel_labels = day_labels

    month_revenue = sum(revenue, Decimal('0.00'))
    return {
        'range': {'from': d_from.isoformat(), 'to': d_to.isoformat(),
                  'days': span, 'granularity': 'hour' if hourly else 'day'},
        'monthRevenue': _uzs(month_revenue),
        # Estimated gross margin (1 - assumed COGS fraction) until recipe costs wired.
        'grossMargin': float(Decimal('1') - DEFAULT_COGS_FRACTION),
        'revenue30': [_uzs(v) for v in revenue],
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
    }
