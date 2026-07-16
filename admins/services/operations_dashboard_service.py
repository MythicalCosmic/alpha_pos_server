"""Operations dashboard (item 17): live table grid, order funnel, prep-by-category,
and orders-by-hour. Defaults to TODAY's business day (AppSettings.business_day_start);
an explicit ?from=&to= overrides. Pure derivations over Order / OrderItem / Table.
"""
from datetime import datetime

from django.db.models import Count, Q
from django.utils import timezone

_ACTIVE = ('OPEN', 'PREPARING', 'READY')
_FUNNEL = ('OPEN', 'PREPARING', 'READY', 'COMPLETED', 'CANCELED')
OPERATING_HOURS = list(range(7, 24)) + [0, 1, 2]


def _window(date_from, date_to):
    from base.services.business_day import business_date, day_window, range_window
    if date_from and date_to:
        try:
            d0 = datetime.strptime(date_from.strip(), '%Y-%m-%d').date()
            d1 = datetime.strptime(date_to.strip(), '%Y-%m-%d').date()
            return range_window(d0, d1)
        except (ValueError, TypeError, AttributeError):
            pass
    return day_window(business_date())


def operations_dashboard(date_from=None, date_to=None, tod_from=None, tod_to=None,
                         datetime_from=None, datetime_to=None,
                         from_at=None, to_at=None):
    from base.models import Order, OrderItem, Table
    from base.services.business_day import resolve_reporting_window
    window = resolve_reporting_window(
        date_from, date_to,
        tod_from=tod_from, tod_to=tod_to,
        datetime_from=datetime_from, datetime_to=datetime_to,
        from_at=from_at, to_at=to_at,
    )
    lo, hi = window.start_at, window.end_at
    # Base querysets for the window, restricted to the working-hours (tod) window
    # per day when tod_from/tod_to are given — every operations block derives from these.
    _o = window.filter(
        Order.objects.filter(is_deleted=False), 'created_at',
    )
    _oi = window.filter(
        OrderItem.objects.filter(is_deleted=False, order__is_deleted=False),
        'order__created_at',
    )

    # ── table grid: status DERIVED from this table's live orders in the window
    #    (ready if any READY, occupied if any OPEN/PREPARING, else free) ──
    rows = (
        _o.filter(table__isnull=False, status__in=_ACTIVE)
        .values('table_id')
        .annotate(
            total=Count('id'),
            ready=Count('id', filter=Q(status='READY')),
        )
    )
    by_table = {r['table_id']: r for r in rows}
    tables = (
        Table.objects.filter(is_deleted=False, is_active=True)
        .select_related('place')
        .order_by('place__name', 'sort_order', 'number')
    )
    table_grid = []
    for t in tables:
        r = by_table.get(t.id)
        if r and r['ready']:
            status = 'ready'
        elif r and r['total']:
            status = 'occupied'
        else:
            status = 'free'
        label = f"{t.place.name} · {t.number}" if t.place_id and t.place else str(t.number)
        table_grid.append({
            'id': t.id, 'label': label, 'status': status,
            'orders': r['total'] if r else 0,
        })

    # ── funnel: order pipeline counts in the window ──
    counts = {row['status']: row['c'] for row in (
        _o.values('status').annotate(c=Count('id'))
    )}
    funnel = [{'status': s, 'count': counts.get(s, 0)} for s in _FUNNEL]

    # ── prep by category: # order-items per category + avg prep of their orders ──
    cat_count, cat_order_prep = {}, {}
    for cat, oid, created, ready in (
        _oi.exclude(order__status='CANCELED')
        .values_list('product__category__name', 'order_id',
                     'order__created_at', 'order__ready_at')
    ):
        cat = cat or 'Uncategorized'
        cat_count[cat] = cat_count.get(cat, 0) + 1
        seen = cat_order_prep.setdefault(cat, {})
        if oid not in seen:
            seen[oid] = ((ready - created).total_seconds()
                         if ready and created and ready >= created else None)
    PREP_TARGET_MINS = 15.0  # kitchen SLA placeholder (no config field exists yet)
    prep_by_category = []
    for cat, count in sorted(cat_count.items(), key=lambda kv: -kv[1]):
        preps = [p for p in cat_order_prep.get(cat, {}).values() if p is not None]
        avg_secs = (sum(preps) / len(preps)) if preps else None
        prep_by_category.append({
            'category': cat,
            'count': count,
            'avg_prep_seconds': int(round(avg_secs)) if avg_secs is not None else None,
            # FE reads `mins` (avg prep, float minutes); 0 when no order in this
            # category has ready_at yet (matches the FE's undefined->0 fallback).
            # `target` is a placeholder SLA until an AppSettings field exists.
            'mins': round(avg_secs / 60.0, 1) if avg_secs is not None else 0,
            'target': PREP_TARGET_MINS,
        })

    # ── orders by hour (09..22), localtime hour (matches the sales heatmap) ──
    hour_counts = {hour: 0 for hour in OPERATING_HOURS}
    for (created,) in (
        _o.exclude(status='CANCELED').values_list('created_at')
    ):
        h = timezone.localtime(created).hour
        if h in hour_counts:
            hour_counts[h] += 1
    orders_by_hour = [
        {'hour': f'{hour:02d}', 'orders': hour_counts[hour]}
        for hour in OPERATING_HOURS
    ]

    return {
        'range': window.metadata(),
        'tableGrid': table_grid,
        'funnel': funnel,
        'prepByCategory': prep_by_category,
        'ordersByHour': orders_by_hour,
    }
