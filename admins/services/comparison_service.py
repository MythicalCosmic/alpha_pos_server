"""Compare-Periods analytics — every sales metric for two date ranges side by side.

Backs the single endpoint GET /api/admins/analytics/comparison/. The FE picks two
ranges (A = primary, B = baseline) and this returns every block the page renders:
KPIs with deltas, an overlaid revenue timeseries, category / product breakdowns,
top gainers & losers, hour / weekday distributions plus a 2-D hour×weekday matrix,
payment-method and order-type splits, and optional per-branch / per-cashier splits.

Conventions (matching the rest of admins/services):
- Money is INTEGER so'm — UZS has no minor unit. Values are ints, not strings.
- Realized sales, revenue, tenders and product units are attributed to paid_at.
  Operational demand heatmaps remain attributed to created_at in the caller's
  timezone (default Asia/Tashkent).
- Order revenue = Sum(total_amount) (the canonical figure used everywhere else).
  gross_revenue = net + discounts (== Sum(subtotal)) is derived from total_amount +
  discount_amount so it never depends on `subtotal` being back-filled on old rows.
- Line-item revenue proportionally allocates Order.discount_amount, matching the
  product analytics and reconciling discounted orders.
- Refunds are separate events attributed to refunded_at. Net revenue and product
  movement subtract those events without erasing the original paid_at sale.
- Every aggregation happens in the DB (annotate/aggregate + Trunc/Extract), two
  filtered passes — one per period.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import (
    Count, DateTimeField, ExpressionWrapper, F, Q, Sum,
)
from django.db.models.functions import (
    ExtractHour, ExtractIsoWeekDay, TruncDay, TruncMonth, TruncWeek,
)
from django.utils import timezone as djtz

from base.models import Order, OrderItem
from admins.services.refund_reporting import (
    net_grouped_items, refund_events, refund_item_events,
)

# A paid row remains a historical sale even if it is later cancelled/refunded.
_SALES = Q(is_deleted=False, is_paid=True)

_TRUNC = {'day': TruncDay, 'week': TruncWeek, 'month': TruncMonth}


def _som(value):
    """Money/coun as an integer so'm (UZS has no minor unit)."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pct(a, b):
    """delta_pct = (a-b)/b*100, rounded to 1dp.
    b == 0 and a > 0  -> None  (FE renders "New")
    a == b == 0       -> 0.0
    """
    if not b:
        return None if a else 0.0
    return round((a - b) / b * 100.0, 1)


def _kpi(a, b, is_up_good, money=True):
    """One KPI cell: {a, b, delta, delta_pct, is_up_good}. Money values are ints;
    ratio values (avg_items_per_order) keep 2 decimals."""
    if money:
        a, b = _som(a), _som(b)
        delta = a - b
    else:
        a, b = round(float(a or 0), 2), round(float(b or 0), 2)
        delta = round(a - b, 2)
    return {'a': a, 'b': b, 'delta': delta,
            'delta_pct': _pct(a, b), 'is_up_good': is_up_good}


def _window(start_date, end_date, tz=None):
    """Business-day window [start@cutover, (end+1)@cutover) — the 03:00-cutover
    operating day shared with the dashboard, so a Compare period reconciles with
    the dashboard totals for the same dates instead of using a plain calendar day.
    `tz` is ignored (business_day uses the configured TIME_ZONE); it stays in the
    signature for call-site compatibility."""
    from base.services.business_day import range_window
    return range_window(start_date, end_date)


def _local_date(dt, tz):
    """Local calendar date of a truncated-bucket datetime, in tz."""
    if dt is None:
        return None
    return djtz.localtime(dt, tz).date() if djtz.is_aware(dt) else dt.date()


def _period_raw(start_date, end_date, branch_id, tz, granularity):
    """All per-period aggregates as plain dicts, ready to merge with the other
    period. Keeps every query DB-side; ~12 small aggregations."""
    lo, hi = _window(start_date, end_date, tz)

    settled_orders = Order.objects.filter(
        _SALES,
        paid_at__gte=lo,
        paid_at__lt=hi,
    )
    operational_orders = Order.objects.filter(
        is_deleted=False,
        created_at__gte=lo,
        created_at__lt=hi,
    ).exclude(status='CANCELED')
    if branch_id:
        settled_orders = settled_orders.filter(branch_id=branch_id)
        operational_orders = operational_orders.filter(branch_id=branch_id)

    refunds = refund_events(lo, hi)
    if branch_id:
        refunds = refunds.filter(branch_id=branch_id)

    items = OrderItem.objects.filter(
        is_deleted=False, order__is_deleted=False, order__is_paid=True,
        order__paid_at__gte=lo, order__paid_at__lt=hi,
    )
    if branch_id:
        items = items.filter(order__branch_id=branch_id)
    refund_items = refund_item_events(lo, hi)
    if branch_id:
        refund_items = refund_items.filter(order__branch_id=branch_id)

    # -- headline scalars ---------------------------------------------------
    k = settled_orders.aggregate(
        net=Sum('total_amount'),
        discounts=Sum('discount_amount'),
        orders=Count('id'),
    )
    sale_net = _som(k['net'])
    refund_amount = _som(refunds.aggregate(v=Sum('amount'))['v'])
    net = sale_net - refund_amount
    discounts = _som(k['discounts'])
    settled_count = k['orders'] or 0
    operational_count = operational_orders.count()
    gross = sale_net + discounts                  # pre-discount sale revenue
    gross_items = int(items.aggregate(q=Sum('quantity'))['q'] or 0)
    refunded_items = int(refund_items.aggregate(q=Sum('quantity'))['q'] or 0)
    n_items = gross_items - refunded_items

    # -- revenue timeseries (bucket -> so'm) --------------------------------
    trunc = _TRUNC.get(granularity, TruncDay)
    from base.services.business_day import business_day_start
    cutover = business_day_start()
    offset = timedelta(
        hours=cutover.hour,
        minutes=cutover.minute,
        seconds=cutover.second,
    )
    settlement_clock = ExpressionWrapper(
        F('paid_at') - offset,
        output_field=DateTimeField(),
    )
    ts = {}
    for r in (settled_orders.annotate(bucket=trunc(settlement_clock, tzinfo=tz))
                    .values('bucket').annotate(v=Sum('total_amount'))):
        d = _local_date(r['bucket'], tz)
        if d is not None:
            ts[d] = _som(r['v'])
    refund_clock = ExpressionWrapper(
        F('refunded_at') - offset,
        output_field=DateTimeField(),
    )
    for r in (refunds.annotate(bucket=trunc(refund_clock, tzinfo=tz))
                     .values('bucket').annotate(v=Sum('amount'))):
        d = _local_date(r['bucket'], tz)
        if d is not None:
            ts[d] = ts.get(d, 0) - _som(r['v'])

    # -- categories / products ---------------------------------------------
    cat = {}
    for r in net_grouped_items(
        items, refund_items,
        ('product__category_id', 'product__category__name'),
    ):
        cat[r['product__category_id']] = {
            'name': r['product__category__name'],
            'revenue': _som(r['revenue']), 'qty': int(r['qty'] or 0),
            'gross_revenue': _som(r['gross_revenue']),
            'refund_amount': _som(r['refund_revenue']),
        }
    prod = {}
    for r in net_grouped_items(
        items, refund_items,
        ('product_id', 'product__name', 'product__category__name'),
    ):
        prod[r['product_id']] = {
            'name': r['product__name'], 'category': r['product__category__name'],
            'revenue': _som(r['revenue']), 'qty': int(r['qty'] or 0),
            'gross_revenue': _som(r['gross_revenue']),
            'refund_amount': _som(r['refund_revenue']),
        }

    # -- hour / weekday / hour×weekday (order counts) -----------------------
    by_hour = {r['h']: r['c'] for r in
               operational_orders.annotate(h=ExtractHour('created_at', tzinfo=tz))
                                 .values('h').annotate(c=Count('id'))}
    # ISO weekday is 1=Mon..7=Sun -> shift to 0=Mon..6=Sun.
    by_wd = {r['wd'] - 1: r['c'] for r in
             operational_orders.annotate(wd=ExtractIsoWeekDay('created_at', tzinfo=tz))
                               .values('wd').annotate(c=Count('id'))}
    hw = {}
    for r in (operational_orders.annotate(
            h=ExtractHour('created_at', tzinfo=tz),
            wd=ExtractIsoWeekDay('created_at', tzinfo=tz),
        ).values('h', 'wd').annotate(c=Count('id'))):
        hw[(r['wd'] - 1, r['h'])] = r['c']

    # -- payment methods / order types (revenue) ----------------------------
    # Canonical tenders (cash / card / payme). A MIXED order is attributed to its
    # real tenders instead of a `MIXED` bucket; cash is the bill portion.
    from base.services.tender import net_breakdown
    _tsplit, _ = net_breakdown(settled_orders, refunds)
    pay = {k: _som(_tsplit[k]) for k in ('cash', 'card', 'payme')}
    if _tsplit['unknown']:
        pay['unknown'] = _som(_tsplit['unknown'])
    otype = {}
    for r in settled_orders.values('order_type').annotate(v=Sum('total_amount')):
        key = r['order_type'] or 'HALL'
        otype[key] = otype.get(key, 0) + _som(r['v'])
    for r in refunds.values('order__order_type').annotate(v=Sum('amount')):
        key = r['order__order_type'] or 'HALL'
        otype[key] = otype.get(key, 0) - _som(r['v'])

    # -- branch / cashier ---------------------------------------------------
    branch = {r['branch_id']: _som(r['v']) for r in
              settled_orders.values('branch_id').annotate(v=Sum('total_amount'))}
    for r in refunds.values('branch_id').annotate(v=Sum('amount')):
        key = r['branch_id'] or ''
        branch[key] = branch.get(key, 0) - _som(r['v'])
    cashier = {}
    for r in (settled_orders.values(
            'cashier_id', 'cashier__first_name', 'cashier__last_name',
        ).annotate(v=Sum('total_amount'))):
        cid = r['cashier_id']
        if cid is None:
            continue
        name = f"{r['cashier__first_name'] or ''} {r['cashier__last_name'] or ''}".strip()
        cashier[cid] = {'name': name or f'#{cid}', 'revenue': _som(r['v'])}
    for r in refunds.values(
        'cashier_id', 'cashier__first_name', 'cashier__last_name',
    ).annotate(v=Sum('amount')):
        cid = r['cashier_id']
        if cid is None:
            continue
        name = f"{r['cashier__first_name'] or ''} {r['cashier__last_name'] or ''}".strip()
        target = cashier.setdefault(cid, {
            'name': name or f'#{cid}', 'revenue': 0,
        })
        target['revenue'] -= _som(r['v'])

    return {
        'net': net, 'gross': gross, 'sale_net': sale_net,
        'refunds': refund_amount, 'discounts': discounts,
        'orders': operational_count, 'items': n_items,
        'aov': (net / settled_count) if settled_count else 0,
        'aipo': (n_items / settled_count) if settled_count else 0,
        'ts': ts, 'cat': cat, 'prod': prod,
        'by_hour': by_hour, 'by_wd': by_wd, 'hw': hw,
        'pay': pay, 'otype': otype, 'branch': branch, 'cashier': cashier,
    }


def _series(ts, start_date, end_date, granularity):
    """1-based relative index sequence with zero-filled gaps so FE can overlay A
    and B on the same axis. `date` is the real calendar date of each bucket."""
    out, i = [], 1
    if granularity == 'month':
        cur = start_date.replace(day=1)
        while cur <= end_date:
            out.append({'index': i, 'date': cur.isoformat(), 'value': ts.get(cur, 0)})
            y, m = (cur.year + 1, 1) if cur.month == 12 else (cur.year, cur.month + 1)
            cur, i = date(y, m, 1), i + 1
    elif granularity == 'week':
        cur = start_date - timedelta(days=start_date.weekday())   # back to Monday
        while cur <= end_date:
            out.append({'index': i, 'date': cur.isoformat(), 'value': ts.get(cur, 0)})
            cur, i = cur + timedelta(days=7), i + 1
    else:  # day
        cur = start_date
        while cur <= end_date:
            out.append({'index': i, 'date': cur.isoformat(), 'value': ts.get(cur, 0)})
            cur, i = cur + timedelta(days=1), i + 1
    return out


def _shares(d, key_name):
    """[{<key_name>, value, share}] sorted by value desc; share is a 0..1 fraction."""
    total = sum(d.values())
    return [{key_name: k, 'value': v,
             'share': round(v / total, 4) if total else 0.0}
            for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)]


def compare_periods(a_start, a_end, b_start, b_end, granularity='day',
                    branch_id=None, tz_name='Asia/Tashkent'):
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — unknown tz falls back to the house default
        tz = ZoneInfo('Asia/Tashkent')
    if granularity not in _TRUNC:
        granularity = 'day'

    A = _period_raw(a_start, a_end, branch_id, tz, granularity)
    B = _period_raw(b_start, b_end, branch_id, tz, granularity)

    # -- KPIs ---------------------------------------------------------------
    kpis = {
        'gross_revenue': _kpi(A['gross'], B['gross'], True),
        'net_revenue': _kpi(A['net'], B['net'], True),
        'refunds': _kpi(A['refunds'], B['refunds'], False),
        'orders': _kpi(A['orders'], B['orders'], True),
        'items_sold': _kpi(A['items'], B['items'], True),
        'aov': _kpi(A['aov'], B['aov'], True),
        'avg_items_per_order': _kpi(A['aipo'], B['aipo'], True, money=False),
        'discounts': _kpi(A['discounts'], B['discounts'], False),
    }

    # -- categories (union, sorted by A revenue) ----------------------------
    categories = []
    for cid in set(A['cat']) | set(B['cat']):
        a, b = A['cat'].get(cid, {}), B['cat'].get(cid, {})
        ar, br = a.get('revenue', 0), b.get('revenue', 0)
        categories.append({
            'id': cid, 'name': a.get('name') or b.get('name') or 'Uncategorized',
            'a_revenue': ar, 'b_revenue': br,
            'a_qty': a.get('qty', 0), 'b_qty': b.get('qty', 0),
            'a_gross_revenue': a.get('gross_revenue', 0),
            'b_gross_revenue': b.get('gross_revenue', 0),
            'a_refund_amount': a.get('refund_amount', 0),
            'b_refund_amount': b.get('refund_amount', 0),
            'delta_pct': _pct(ar, br),
        })
    categories.sort(key=lambda c: c['a_revenue'], reverse=True)

    # -- products (union) -> top 50 by A revenue + Others bucket ------------
    prows = []
    for pid in set(A['prod']) | set(B['prod']):
        a, b = A['prod'].get(pid, {}), B['prod'].get(pid, {})
        ar, br = a.get('revenue', 0), b.get('revenue', 0)
        prows.append({
            'id': pid, 'name': a.get('name') or b.get('name'),
            'category': a.get('category') or b.get('category'),
            'a_qty': a.get('qty', 0), 'b_qty': b.get('qty', 0),
            'a_gross_revenue': a.get('gross_revenue', 0),
            'b_gross_revenue': b.get('gross_revenue', 0),
            'a_refund_amount': a.get('refund_amount', 0),
            'b_refund_amount': b.get('refund_amount', 0),
            'a_revenue': ar, 'b_revenue': br, 'delta_pct': _pct(ar, br),
        })
    prows.sort(key=lambda r: r['a_revenue'], reverse=True)
    products = prows[:50]
    others = prows[50:]
    if others:
        oa = sum(r['a_revenue'] for r in others)
        ob = sum(r['b_revenue'] for r in others)
        products.append({
            'id': None, 'name': 'Others', 'category': None,
            'a_qty': sum(r['a_qty'] for r in others),
            'b_qty': sum(r['b_qty'] for r in others),
            'a_gross_revenue': sum(r['a_gross_revenue'] for r in others),
            'b_gross_revenue': sum(r['b_gross_revenue'] for r in others),
            'a_refund_amount': sum(r['a_refund_amount'] for r in others),
            'b_refund_amount': sum(r['b_refund_amount'] for r in others),
            'a_revenue': oa, 'b_revenue': ob, 'delta_pct': _pct(oa, ob),
        })

    # -- gainers / losers by revenue delta (over ALL products) --------------
    def _mover(r):
        return {'name': r['name'], 'a': r['a_revenue'], 'b': r['b_revenue'],
                'delta': r['a_revenue'] - r['b_revenue'],
                'delta_pct': _pct(r['a_revenue'], r['b_revenue'])}

    by_delta = sorted(prows, key=lambda r: r['a_revenue'] - r['b_revenue'],
                      reverse=True)
    top_gainers = [_mover(r) for r in by_delta[:10] if r['a_revenue'] - r['b_revenue'] > 0]
    top_losers = [_mover(r) for r in reversed(by_delta[-10:])
                  if r['a_revenue'] - r['b_revenue'] < 0]

    # -- hour / weekday / matrix (zero-filled) ------------------------------
    by_hour = {
        'a': [{'hour': h, 'value': A['by_hour'].get(h, 0)} for h in range(24)],
        'b': [{'hour': h, 'value': B['by_hour'].get(h, 0)} for h in range(24)],
    }
    by_weekday = {
        'a': [{'weekday': w, 'value': A['by_wd'].get(w, 0)} for w in range(7)],
        'b': [{'weekday': w, 'value': B['by_wd'].get(w, 0)} for w in range(7)],
    }
    hour_weekday = {
        'a': [[A['hw'].get((w, h), 0) for h in range(24)] for w in range(7)],
        'b': [[B['hw'].get((w, h), 0) for h in range(24)] for w in range(7)],
    }

    # -- branch (only when multi-branch and not already filtered) -----------
    by_branch = []
    branch_ids = set(A['branch']) | set(B['branch'])
    if not branch_id and len(branch_ids) > 1:
        for bid in branch_ids:
            av, bv = A['branch'].get(bid, 0), B['branch'].get(bid, 0)
            by_branch.append({'id': bid or '', 'name': bid or 'unknown',
                              'a': av, 'b': bv, 'delta_pct': _pct(av, bv)})
        by_branch.sort(key=lambda x: x['a'], reverse=True)

    # -- cashier (only when present) ----------------------------------------
    by_cashier = []
    for cid in set(A['cashier']) | set(B['cashier']):
        a, b = A['cashier'].get(cid, {}), B['cashier'].get(cid, {})
        av, bv = a.get('revenue', 0), b.get('revenue', 0)
        by_cashier.append({'id': cid, 'name': a.get('name') or b.get('name') or f'#{cid}',
                           'a': av, 'b': bv, 'delta_pct': _pct(av, bv)})
    by_cashier.sort(key=lambda x: x['a'], reverse=True)

    return {
        'period_a': {'start': a_start.isoformat(), 'end': a_end.isoformat(),
                     'days': (a_end - a_start).days + 1},
        'period_b': {'start': b_start.isoformat(), 'end': b_end.isoformat(),
                     'days': (b_end - b_start).days + 1},
        'kpis': kpis,
        'revenue_timeseries': {
            'granularity': granularity,
            'a': _series(A['ts'], a_start, a_end, granularity),
            'b': _series(B['ts'], b_start, b_end, granularity),
        },
        'categories': categories,
        'products': products,
        'top_gainers': top_gainers,
        'top_losers': top_losers,
        'by_hour': by_hour,
        'by_weekday': by_weekday,
        'hour_weekday': hour_weekday,
        'payment_methods': {'a': _shares(A['pay'], 'method'),
                            'b': _shares(B['pay'], 'method')},
        'order_types': {'a': _shares(A['otype'], 'type'),
                        'b': _shares(B['otype'], 'type')},
        'by_branch': by_branch,
        'by_cashier': by_cashier,
    }
