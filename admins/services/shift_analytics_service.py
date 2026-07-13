"""Deep shift analytics for cashier and kitchen/chef shifts.

Pure derivations over Shift / Order / OrderItem / CashReconciliation (+ HR
Attendance when present). No new models. The two entry points
``cashier_shift_analytics`` and ``kitchen_shift_analytics`` return a rich,
fully-broken-down payload (totals, averages, distributions, leaderboards,
punctuality / lateness, cash accuracy) so the manager dashboard can answer
essentially any question about a shift window — not just totals.

Money is attributed by ``paid_at`` within the shift window (matching
ShiftService.end_shift and cash reconciliation), order volume by
``created_at``. Cash bundles legacy NULL payment_method with CASH, exactly
like end_shift, so historical shifts don't read as zero cash.
"""
import datetime
import logging
from decimal import Decimal

from django.db.models import Avg, Count, DecimalField, DurationField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

logger = logging.getLogger(__name__)

CENTS = Decimal('0.01')
# An order taking longer than this (kitchen: created -> ready) is "slow".
DEFAULT_TARGET_PREP_SECONDS = 15 * 60
_DEC = DecimalField(max_digits=18, decimal_places=2)


# ───────────────────────── small helpers ─────────────────────────

def _q2(value):
    return Decimal(value or 0).quantize(CENTS)


def _money(value):
    return str(_q2(value))


def _hours(start, end):
    return max((end - start).total_seconds() / 3600.0, 0.0)


def _pct(part, whole):
    return round(part / whole * 100, 2) if whole else 0.0


def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _user_name(user):
    if not user:
        return None
    return f'{user.first_name} {user.last_name}'.strip() or user.email


def _attendance_map(user_ids, date_from, date_to):
    """(user_id, date) -> attendance summary, when the HR app has records.

    Returns {} silently if HR isn't available or nothing is recorded, so the
    analytics still work on installs that don't use HR attendance.
    """
    out = {}
    try:
        from hr.models import Employee, Attendance
    except Exception:
        return out
    try:
        emp_to_user = dict(
            Employee.objects.filter(user_id__in=list(user_ids))
            .values_list('id', 'user_id')
        )
        if not emp_to_user:
            return out
        rows = Attendance.objects.filter(
            employee_id__in=list(emp_to_user.keys()),
            date__gte=date_from, date__lte=date_to,
        )
        for a in rows:
            uid = emp_to_user.get(a.employee_id)
            if uid is None:
                continue
            out[(uid, a.date)] = {
                'status': a.status,
                'check_in': a.check_in.isoformat() if a.check_in else None,
                'check_out': a.check_out.isoformat() if a.check_out else None,
                'work_hours': str(a.work_hours) if a.work_hours is not None else None,
                'overtime_hours': str(a.overtime_hours) if a.overtime_hours is not None else None,
            }
    except Exception:
        logger.exception('attendance map build failed')
    return out


def _punctuality(shift, att_map):
    """Scheduled vs actual start (lateness) + any HR attendance row.

    late_minutes > 0 means the shift started after the template's scheduled
    start; negative means early. None when no template is attached.
    """
    local_start = timezone.localtime(shift.start_time)
    info = {
        'actual_start': local_start.isoformat(),
        'scheduled_start': None,
        'late_minutes': None,
        'is_late': None,
        'attendance': att_map.get((shift.user_id, local_start.date())),
    }
    tmpl = shift.shift_template
    if tmpl and tmpl.start_time:
        sched_naive = datetime.datetime.combine(local_start.date(), tmpl.start_time)
        sched_dt = timezone.make_aware(sched_naive, timezone.get_current_timezone())
        late = round((shift.start_time - sched_dt).total_seconds() / 60)
        info['scheduled_start'] = sched_dt.isoformat()
        info['late_minutes'] = late
        info['is_late'] = late > 0
    return info


def _reconciliation(shift):
    try:
        rec = shift.reconciliation
    except Exception:
        return None
    if not rec or getattr(rec, 'is_deleted', False):
        return None
    return {
        'expected_cash': _money(rec.expected_cash),
        'actual_cash': _money(rec.actual_cash),
        'difference': _money(rec.difference),
        'is_short': rec.difference < 0,
        'is_over': rec.difference > 0,
        'notes': rec.notes,
        'reconciled_by': _user_name(rec.reconciled_by) if rec.reconciled_by else None,
        'reconciled_at': rec.created_at.isoformat() if rec.created_at else None,
    }


# ───────────────────────── per-shift rows ─────────────────────────

def _cashier_shift_row(shift, att_map):
    from base.models import Order, OrderItem

    start = shift.start_time
    end = shift.end_time or timezone.now()
    duration_min = max(int((end - start).total_seconds() / 60), 0)
    hours = _hours(start, end)

    # Volume by created_at; money by paid_at — same split end_shift uses.
    taken = Order.objects.filter(
        is_deleted=False, cashier_id=shift.user_id,
        created_at__gte=start, created_at__lte=end,
    )
    vol = taken.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status='COMPLETED')),
        cancelled=Count('id', filter=Q(status='CANCELED')),
        open=Count('id', filter=Q(status='OPEN')),
        preparing=Count('id', filter=Q(status='PREPARING')),
        ready=Count('id', filter=Q(status='READY')),
        hall=Count('id', filter=Q(order_type='HALL')),
        delivery=Count('id', filter=Q(order_type='DELIVERY')),
        pickup=Count('id', filter=Q(order_type='PICKUP')),
    )
    prep = taken.filter(
        ready_at__isnull=False, status__in=['READY', 'COMPLETED'],
    ).aggregate(avg=Avg(ExpressionWrapper(
        F('ready_at') - F('created_at'), output_field=DurationField())))['avg']
    avg_prep_seconds = int(prep.total_seconds()) if prep else None

    items = OrderItem.objects.filter(
        is_deleted=False, order__is_deleted=False, order__cashier_id=shift.user_id,
        order__created_at__gte=start, order__created_at__lte=end,
    ).exclude(order__status='CANCELED').aggregate(
        units=Coalesce(Sum('quantity'), 0),
        lines=Count('id'),
    )

    paid = Order.objects.filter(
        is_deleted=False, cashier_id=shift.user_id, is_paid=True,
        paid_at__gte=start, paid_at__lte=end,
    ).exclude(status='CANCELED')
    money = paid.aggregate(
        revenue=Coalesce(Sum('total_amount'), Decimal('0'), output_field=_DEC),
        paid_count=Count('id'),
        discount_total=Coalesce(Sum('discount_amount'), Decimal('0'), output_field=_DEC),
        discounted_orders=Count('id', filter=Q(discount_amount__gt=0) | Q(discount_percent__gt=0)),
        avg_discount_pct=Avg('discount_percent', filter=Q(discount_percent__gt=0)),
    )
    revenue = money['revenue'] or Decimal('0')
    paid_count = money['paid_count'] or 0
    # Tender split comes from base.services.tender, not from Order.payment_method:
    # a MIXED order used to contribute NOTHING to cash or card (its whole total sat
    # in a `MIXED` bucket), so cash+card never reconciled to revenue for any shift
    # containing a split payment. cash is derived (bill portion, not tendered).
    from base.services.tender import breakdown_for_orders
    _split, _detail = breakdown_for_orders(paid)
    cash, card, payme = _split['cash'], _split['card'], _split['payme']
    total = vol['total'] or 0

    return {
        'shift_id': shift.id,
        'user_id': shift.user_id,
        'user_name': _user_name(shift.user),
        'status': shift.status,
        'start_time': start.isoformat(),
        'end_time': shift.end_time.isoformat() if shift.end_time else None,
        'duration_minutes': duration_min,
        'orders': {
            'total': total,
            'completed': vol['completed'] or 0,
            'cancelled': vol['cancelled'] or 0,
            'open': vol['open'] or 0,
            'preparing': vol['preparing'] or 0,
            'ready': vol['ready'] or 0,
            'paid': paid_count,
            'cancel_rate_pct': _pct(vol['cancelled'] or 0, total),
            'by_type': {'hall': vol['hall'] or 0, 'delivery': vol['delivery'] or 0, 'pickup': vol['pickup'] or 0},
        },
        'items': {'units_sold': items['units'] or 0, 'line_items': items['lines'] or 0},
        'money': {
            'revenue': _money(revenue),
            'cash': _money(cash),
            'card': _money(card),          # Uzcard + Humo + Card
            'payme': _money(payme),        # own tender
            'avg_order_value': _money(revenue / paid_count) if paid_count else _money(0),
            # Canonical tenders. MIXED is never a bucket; cash+card+payme == revenue.
            'payment_mix': {
                'cash': _money(cash),
                'card': _money(card),
                'payme': _money(payme),
                **({'unknown': _money(_split['unknown'])} if _split['unknown'] else {}),
            },
            'card_detail': {k: _money(v) for k, v in _detail.items()},
        },
        'discounts': {
            'total_given': _money(money['discount_total']),
            'discounted_orders': money['discounted_orders'] or 0,
            'discount_rate_pct': _pct(money['discounted_orders'] or 0, total),
            'avg_discount_pct': round(float(money['avg_discount_pct']), 2) if money['avg_discount_pct'] else 0.0,
        },
        'speed': {
            'avg_prep_seconds': avg_prep_seconds,
            'orders_per_hour': round(total / hours, 2) if hours else 0.0,
            'revenue_per_hour': _money(revenue / Decimal(str(hours))) if hours else _money(0),
        },
        'punctuality': _punctuality(shift, att_map),
        'reconciliation': _reconciliation(shift),
    }


def _kitchen_shift_row(shift, att_map, target_prep_seconds):
    from base.models import Order, OrderItem

    start = shift.start_time
    end = shift.end_time or timezone.now()
    duration_min = max(int((end - start).total_seconds() / 60), 0)
    hours = _hours(start, end)

    # Kitchen output is window-based: all (non-cancelled) orders created during
    # the shift. Per-item chef attribution isn't tracked (no prepared_by FK),
    # so this measures the kitchen's throughput while this person was on.
    orders = Order.objects.filter(
        is_deleted=False, created_at__gte=start, created_at__lte=end,
    ).exclude(status='CANCELED')
    total = orders.count()
    readied = orders.filter(ready_at__isnull=False)

    durations = [
        (r - c).total_seconds()
        for c, r in readied.values_list('created_at', 'ready_at')
        if r and c
    ]
    readied_count = len(durations)
    slow = sum(1 for d in durations if d > target_prep_seconds)
    avg_prep = int(sum(durations) / readied_count) if readied_count else None
    med_prep = int(_median(durations)) if durations else None

    items = OrderItem.objects.filter(
        is_deleted=False, order__is_deleted=False,
        order__created_at__gte=start, order__created_at__lte=end,
        ready_at__isnull=False,
    ).exclude(order__status='CANCELED').aggregate(
        units=Coalesce(Sum('quantity'), 0), lines=Count('id'))

    return {
        'shift_id': shift.id,
        'user_id': shift.user_id,
        'user_name': _user_name(shift.user),
        'status': shift.status,
        'start_time': start.isoformat(),
        'end_time': shift.end_time.isoformat() if shift.end_time else None,
        'duration_minutes': duration_min,
        'orders_in_window': total,
        'orders_readied': readied_count,
        'orders_pending': total - readied_count,
        'completion_rate_pct': _pct(readied_count, total),
        'items_prepared': {'units': items['units'] or 0, 'line_items': items['lines'] or 0},
        'prep_time': {
            'avg_seconds': avg_prep,
            'median_seconds': med_prep,
            'fastest_seconds': int(min(durations)) if durations else None,
            'slowest_seconds': int(max(durations)) if durations else None,
            'slow_orders': slow,
            'slow_rate_pct': _pct(slow, readied_count),
            'target_seconds': target_prep_seconds,
        },
        'throughput': {
            'orders_per_hour': round(readied_count / hours, 2) if hours else 0.0,
            'items_per_hour': round((items['units'] or 0) / hours, 2) if hours else 0.0,
        },
        'punctuality': _punctuality(shift, att_map),
    }


# ───────────────────── distribution / leaderboard ─────────────────────

def _hourly_daily(shifts):
    """Orders + revenue distributed by hour-of-day and by calendar date,
    across every shift in the set. Answers 'when are we busy?'."""
    from base.models import Order
    if not shifts:
        return {'by_hour': [], 'by_date': [], 'peak_hour': None}

    user_ids = {s.user_id for s in shifts}
    window_start = min(s.start_time for s in shifts)
    window_end = max((s.end_time or timezone.now()) for s in shifts)

    rows = Order.objects.filter(
        is_deleted=False, cashier_id__in=list(user_ids),
        created_at__gte=window_start, created_at__lte=window_end,
    ).exclude(status='CANCELED').values_list('created_at', 'total_amount', 'is_paid')

    by_hour = {h: {'orders': 0, 'revenue': Decimal('0')} for h in range(24)}
    by_date = {}
    for created_at, total_amount, is_paid in rows:
        local = timezone.localtime(created_at)
        h = local.hour
        by_hour[h]['orders'] += 1
        d = local.date().isoformat()
        slot = by_date.setdefault(d, {'orders': 0, 'revenue': Decimal('0')})
        slot['orders'] += 1
        if is_paid and total_amount:
            by_hour[h]['revenue'] += total_amount
            slot['revenue'] += total_amount

    hour_list = [
        {'hour': h, 'orders': by_hour[h]['orders'], 'revenue': _money(by_hour[h]['revenue'])}
        for h in range(24)
    ]
    peak = max(hour_list, key=lambda x: x['orders']) if any(x['orders'] for x in hour_list) else None
    date_list = [
        {'date': d, 'orders': by_date[d]['orders'], 'revenue': _money(by_date[d]['revenue'])}
        for d in sorted(by_date)
    ]
    return {'by_hour': hour_list, 'by_date': date_list, 'peak_hour': peak['hour'] if peak else None}


def _cashier_leaderboard(rows):
    """Per-cashier rollup across their shifts, with ranks on the metrics a
    manager actually compares people on."""
    agg = {}
    for r in rows:
        a = agg.setdefault(r['user_id'], {
            'user_id': r['user_id'], 'user_name': r['user_name'],
            'shifts': 0, 'orders': 0, 'paid': 0, 'revenue': Decimal('0'),
            'cash': Decimal('0'), 'cancelled': 0,
            'late_shifts': 0, 'late_minutes_total': 0, 'cash_variance': Decimal('0'),
            '_prep': [],
        })
        a['shifts'] += 1
        a['orders'] += r['orders']['total']
        a['paid'] += r['orders']['paid']
        a['revenue'] += Decimal(r['money']['revenue'])
        a['cash'] += Decimal(r['money']['cash'])
        a['cancelled'] += r['orders']['cancelled']
        if r['punctuality']['is_late']:
            a['late_shifts'] += 1
            a['late_minutes_total'] += max(r['punctuality']['late_minutes'] or 0, 0)
        if r['reconciliation']:
            a['cash_variance'] += Decimal(r['reconciliation']['difference'])
        if r['speed']['avg_prep_seconds'] is not None:
            a['_prep'].append(r['speed']['avg_prep_seconds'])

    board = []
    for a in agg.values():
        prep = a.pop('_prep')
        board.append({
            'user_id': a['user_id'],
            'user_name': a['user_name'],
            'shifts': a['shifts'],
            'orders': a['orders'],
            'revenue': _money(a['revenue']),
            'cash': _money(a['cash']),
            # AOV = paid revenue / PAID order count (not total orders — that
            # denominator included unpaid/cancelled tickets and read low).
            'avg_order_value': _money(a['revenue'] / a['paid']) if a['paid'] else _money(0),
            'cancelled': a['cancelled'],
            'cancel_rate_pct': _pct(a['cancelled'], a['orders']),
            'late_shifts': a['late_shifts'],
            'late_minutes_total': a['late_minutes_total'],
            'cash_variance': _money(a['cash_variance']),
            'avg_prep_seconds': int(sum(prep) / len(prep)) if prep else None,
        })
    board.sort(key=lambda x: Decimal(x['revenue']), reverse=True)
    for i, row in enumerate(board, 1):
        row['revenue_rank'] = i
    return board


# ───────────────────────── public entry points ─────────────────────────

def _shifts_in_range(date_from, date_to, role, user_id=None):
    from base.models import Shift
    qs = (
        Shift.objects.filter(
            is_deleted=False,
            start_time__date__gte=date_from,
            start_time__date__lte=date_to,
        )
        .select_related('user', 'shift_template', 'reconciliation')
        .order_by('start_time')
    )
    if role:
        qs = qs.filter(user__role=role)
    if user_id:
        qs = qs.filter(user_id=user_id)
    return list(qs)


def cashier_shift_analytics(date_from, date_to, user_id=None):
    """Everything about cashier shifts over [date_from, date_to]."""
    shifts = _shifts_in_range(date_from, date_to, 'CASHIER', user_id)
    att = _attendance_map({s.user_id for s in shifts}, date_from, date_to)
    rows = [_cashier_shift_row(s, att) for s in shifts]

    # ── roll up the summary ──
    n = len(rows)
    status_counts = {'ACTIVE': 0, 'ENDED': 0, 'COMPLETED': 0, 'ABANDONED': 0}
    revenue = cash = card = discount_total = Decimal('0')
    mix = {k: Decimal('0') for k in ('cash', 'card', 'payme')}
    orders = cancelled = paid = units = discounted = duration_total = 0
    prep_vals, late_list = [], []
    on_time = late = reconciled = shorts = overs = 0
    variance_total = abs_variance_total = Decimal('0')
    worst_short = biggest_over = None

    for r in rows:
        status_counts[r['status']] = status_counts.get(r['status'], 0) + 1
        revenue += Decimal(r['money']['revenue'])
        cash += Decimal(r['money']['cash'])
        card += Decimal(r['money']['card'])
        for k in mix:
            mix[k] += Decimal(r['money']['payment_mix'][k])
        discount_total += Decimal(r['discounts']['total_given'])
        discounted += r['discounts']['discounted_orders']
        orders += r['orders']['total']
        cancelled += r['orders']['cancelled']
        paid += r['orders']['paid']
        units += r['items']['units_sold']
        duration_total += r['duration_minutes']
        if r['speed']['avg_prep_seconds'] is not None:
            prep_vals.append(r['speed']['avg_prep_seconds'])
        p = r['punctuality']
        if p['is_late'] is True:
            late += 1
            late_list.append({'shift_id': r['shift_id'], 'user_id': r['user_id'], 'user_name': r['user_name'], 'late_minutes': p['late_minutes'], 'start_time': r['start_time']})
        elif p['is_late'] is False:
            on_time += 1
        rec = r['reconciliation']
        if rec:
            reconciled += 1
            diff = Decimal(rec['difference'])
            variance_total += diff
            abs_variance_total += abs(diff)
            if diff < 0:
                shorts += 1
                if worst_short is None or diff < Decimal(worst_short['difference']):
                    worst_short = {'shift_id': r['shift_id'], 'user_name': r['user_name'], 'difference': rec['difference']}
            elif diff > 0:
                overs += 1
                if biggest_over is None or diff > Decimal(biggest_over['difference']):
                    biggest_over = {'shift_id': r['shift_id'], 'user_name': r['user_name'], 'difference': rec['difference']}

    late_list.sort(key=lambda x: x['late_minutes'], reverse=True)
    total_hours = round(duration_total / 60, 2)
    distinct = len({r['user_id'] for r in rows})

    summary = {
        'shift_count': n,
        'distinct_cashiers': distinct,
        'by_status': status_counts,
        'total_hours': total_hours,
        'avg_shift_minutes': round(duration_total / n, 1) if n else 0.0,
        'orders': {
            'total': orders,
            'paid': paid,
            'cancelled': cancelled,
            'cancel_rate_pct': _pct(cancelled, orders),
            'avg_per_shift': round(orders / n, 2) if n else 0.0,
            'units_sold': units,
        },
        'money': {
            'revenue': _money(revenue),
            'cash': _money(cash),
            'card': _money(card),
            'avg_per_shift': _money(revenue / n) if n else _money(0),
            'avg_order_value': _money(revenue / paid) if paid else _money(0),
            'revenue_per_hour': _money(revenue / Decimal(str(total_hours))) if total_hours else _money(0),
            'payment_mix': {k: _money(v) for k, v in mix.items()},
            'payment_mix_pct': {k: _pct(float(v), float(revenue)) for k, v in mix.items()} if revenue else {k: 0.0 for k in mix},
        },
        'discounts': {
            'total_given': _money(discount_total),
            'discounted_orders': discounted,
            'discount_rate_pct': _pct(discounted, orders),
        },
        'speed': {
            'avg_prep_seconds': int(sum(prep_vals) / len(prep_vals)) if prep_vals else None,
            'fastest_shift_avg_seconds': min(prep_vals) if prep_vals else None,
            'slowest_shift_avg_seconds': max(prep_vals) if prep_vals else None,
        },
        'punctuality': {
            'on_time_shifts': on_time,
            'late_shifts': late,
            'punctuality_rate_pct': _pct(on_time, on_time + late),
            'avg_late_minutes': round(sum(x['late_minutes'] for x in late_list) / late, 1) if late else 0.0,
            'max_late_minutes': late_list[0]['late_minutes'] if late_list else 0,
            'late_arrivals': late_list,
        },
        'cash_accuracy': {
            'shifts_reconciled': reconciled,
            'shifts_unreconciled': n - reconciled,
            'short_count': shorts,
            'over_count': overs,
            'exact_count': reconciled - shorts - overs,
            'net_variance': _money(variance_total),
            'total_abs_variance': _money(abs_variance_total),
            'avg_abs_variance': _money(abs_variance_total / reconciled) if reconciled else _money(0),
            'worst_shortage': worst_short,
            'biggest_overage': biggest_over,
        },
    }

    return {
        'scope': 'cashier',
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'filtered_user_id': user_id,
        'summary': summary,
        'leaderboard': _cashier_leaderboard(rows),
        'distribution': _hourly_daily(shifts),
        'shifts': rows,
    }


def shift_handover_report(shift):
    """Everything a manager needs when a cashier ends a shift and hands over.

    The full per-shift KPIs (money cash/card, payment mix, discounts, averages,
    speed, punctuality, cash reconciliation) PLUS every receipt, a what-sold
    product breakdown, and the shift's peak hours.
    """
    from base.models import Order, OrderItem

    start = shift.start_time
    end = shift.end_time or timezone.now()
    att = _attendance_map(
        {shift.user_id},
        timezone.localtime(start).date(),
        timezone.localtime(end).date(),
    )
    row = _cashier_shift_row(shift, att)

    base_qs = Order.objects.filter(
        is_deleted=False, cashier_id=shift.user_id,
        created_at__gte=start, created_at__lte=end,
    )

    # Every receipt taken during the shift.
    receipts = []
    for o in (
        base_qs.annotate(
            line_items=Count('items', distinct=True),
            units=Coalesce(Sum('items__quantity'), 0),
        ).order_by('created_at')
    ):
        receipts.append({
            'order_id': o.id,
            'display_id': o.display_id,
            'status': o.status,
            'order_type': o.order_type,
            'is_paid': o.is_paid,
            'payment_method': o.payment_method,
            'total_amount': _money(o.total_amount),
            'discount_amount': _money(o.discount_amount),
            'discount_percent': str(o.discount_percent or 0),
            'line_items': o.line_items,
            'units': o.units or 0,
            'created_at': o.created_at.isoformat() if o.created_at else None,
            'paid_at': o.paid_at.isoformat() if o.paid_at else None,
        })

    # What sold: per-product quantity, how many orders it appeared in, revenue.
    from base.services.revenue import net_line_revenue
    line_total = net_line_revenue()
    product_rows = (
        OrderItem.objects.filter(
            is_deleted=False, order__in=base_qs, order__is_paid=True,
        ).exclude(order__status='CANCELED')
        .values('product_id', 'product__name')
        .annotate(
            units_sold=Coalesce(Sum('quantity'), 0),
            times_sold=Count('order', distinct=True),
            revenue=Coalesce(Sum(line_total), Decimal('0'), output_field=_DEC),
        )
        .order_by('-units_sold')
    )
    products = [
        {
            'product_id': r['product_id'],
            'name': r['product__name'],
            'units_sold': r['units_sold'] or 0,
            'times_sold': r['times_sold'] or 0,        # distinct orders it was in
            'revenue': _money(r['revenue']),
        }
        for r in product_rows
    ]

    distribution = _hourly_daily([shift])  # by_hour / peak_hour for this shift

    # Per-type settlement (P2): expected (system) / counted / confirmed /
    # difference per tender type, frozen at close.
    from cashbox.models import ShiftPaymentTotal
    settlement = [{
        'method': spt.method,
        'expected': _money(spt.expected_amount),
        'counted': _money(spt.counted_amount),
        'confirmed': _money(spt.confirmed_amount),
        'difference': _money(spt.difference),
    } for spt in ShiftPaymentTotal.objects.filter(shift=shift, is_deleted=False)]

    # Cash paid OUT of the drawer this shift, grouped by category (P4).
    from cashbox.models import CashboxExpense
    exp_rows = (
        CashboxExpense.objects.filter(shift=shift, is_deleted=False)
        .values('category__name')
        .annotate(total=Coalesce(Sum('amount'), Decimal('0'), output_field=_DEC),
                  count=Count('id'))
    )
    cash_expenses = [{
        'category': r['category__name'] or 'Uncategorized',
        'total': _money(r['total']),
        'count': r['count'],
    } for r in exp_rows]

    return {
        'shift': row,                 # full KPIs incl. money.cash / money.card / payment_mix
        'cashier': {'id': shift.user_id, 'name': _user_name(shift.user)},
        'settlement': settlement,     # per-type expected/counted/confirmed/diff
        'cash_expenses': cash_expenses,
        'receipts': receipts,
        'receipt_count': len(receipts),
        'products': products,
        'best_seller': products[0] if products else None,
        'distribution': distribution,
        'peak_hour': distribution.get('peak_hour'),
    }


def kitchen_shift_analytics(date_from, date_to, user_id=None, role='WAITER',
                            target_prep_seconds=DEFAULT_TARGET_PREP_SECONDS):
    """Everything about kitchen/chef shifts over [date_from, date_to].

    No dedicated chef role exists yet, so `role` selects which staff are
    treated as kitchen (default WAITER). Prep metrics are window-based since
    per-item chef attribution isn't tracked — see module docstring.
    """
    shifts = _shifts_in_range(date_from, date_to, role, user_id)
    att = _attendance_map({s.user_id for s in shifts}, date_from, date_to)
    rows = [_kitchen_shift_row(s, att, target_prep_seconds) for s in shifts]

    n = len(rows)
    status_counts = {'ACTIVE': 0, 'ENDED': 0, 'COMPLETED': 0, 'ABANDONED': 0}
    orders_window = readied = pending = units = duration_total = slow = 0
    prep_vals, late_list = [], []
    on_time = late = 0

    for r in rows:
        status_counts[r['status']] = status_counts.get(r['status'], 0) + 1
        orders_window += r['orders_in_window']
        readied += r['orders_readied']
        pending += r['orders_pending']
        units += r['items_prepared']['units']
        duration_total += r['duration_minutes']
        slow += r['prep_time']['slow_orders']
        if r['prep_time']['avg_seconds'] is not None:
            prep_vals.append(r['prep_time']['avg_seconds'])
        p = r['punctuality']
        if p['is_late'] is True:
            late += 1
            late_list.append({'shift_id': r['shift_id'], 'user_id': r['user_id'], 'user_name': r['user_name'], 'late_minutes': p['late_minutes'], 'start_time': r['start_time']})
        elif p['is_late'] is False:
            on_time += 1

    late_list.sort(key=lambda x: x['late_minutes'], reverse=True)
    total_hours = round(duration_total / 60, 2)

    summary = {
        'shift_count': n,
        'distinct_staff': len({r['user_id'] for r in rows}),
        'role': role,
        'by_status': status_counts,
        'total_hours': total_hours,
        'avg_shift_minutes': round(duration_total / n, 1) if n else 0.0,
        'orders_in_window': orders_window,
        'orders_readied': readied,
        'orders_pending': pending,
        'completion_rate_pct': _pct(readied, orders_window),
        'items_prepared': units,
        'items_per_hour': round(units / total_hours, 2) if total_hours else 0.0,
        'prep_time': {
            'avg_seconds': int(sum(prep_vals) / len(prep_vals)) if prep_vals else None,
            'best_shift_avg_seconds': min(prep_vals) if prep_vals else None,
            'worst_shift_avg_seconds': max(prep_vals) if prep_vals else None,
            'slow_orders': slow,
            'slow_rate_pct': _pct(slow, readied),
            'target_seconds': target_prep_seconds,
        },
        'punctuality': {
            'on_time_shifts': on_time,
            'late_shifts': late,
            'punctuality_rate_pct': _pct(on_time, on_time + late),
            'avg_late_minutes': round(sum(x['late_minutes'] for x in late_list) / late, 1) if late else 0.0,
            'max_late_minutes': late_list[0]['late_minutes'] if late_list else 0,
            'late_arrivals': late_list,
        },
    }

    return {
        'scope': 'kitchen',
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'filtered_user_id': user_id,
        'summary': summary,
        'distribution': _hourly_daily(shifts),
        'shifts': rows,
    }
