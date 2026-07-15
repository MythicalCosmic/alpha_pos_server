"""Products dashboard analytics: overview, per-category, Pareto, and trends.

Pure derivations over Order / OrderItem — no new models. Every window is bounded
on the BUSINESS day (AppSettings.business_day_start, default 03:00) so a 01:00 sale
counts toward the night before, consistent with the dashboard and order stats.
"""
from decimal import Decimal

from django.db.models import Count, Q, Sum
from base.services.revenue import net_line_revenue
from base.services.refund_lines import (
    REFUND_EVENT_ALIAS, refund_line_quantity, refund_line_revenue,
)

_LINE_TOTAL = net_line_revenue()


def _uzs(value):
    """Money as an integer-so'm string (UZS has no minor unit)."""
    try:
        return str(int(value or 0))
    except (TypeError, ValueError):
        return '0'


def _window(date_from, date_to):
    from base.services.business_day import range_window
    return range_window(date_from, date_to)


def _sold_items(date_from, date_to, tod_from=None, tod_to=None):
    """Gross sale-line events in the paid_at business window."""
    from base.models import OrderItem
    from base.services.business_day import tod_filter
    lo, hi = _window(date_from, date_to)
    qs = OrderItem.objects.filter(
        is_deleted=False, order__is_deleted=False, order__is_paid=True,
        order__paid_at__gte=lo, order__paid_at__lt=hi,
    )
    return tod_filter(qs, tod_from, tod_to, field='order__paid_at')


def _refunded_items(date_from, date_to, tod_from=None, tod_to=None):
    """Reversed sale-line events in the refunded_at business window."""
    from admins.services.refund_reporting import refund_item_events
    lo, hi = _window(date_from, date_to)
    return refund_item_events(
        lo, hi, tod_from=tod_from, tod_to=tod_to,
    )


def products_overview(date_from, date_to, tod_from=None, tod_to=None):
    """Headline product KPIs over the window + top sellers / slow movers."""
    items = _sold_items(date_from, date_to, tod_from, tod_to)
    refunds = _refunded_items(date_from, date_to, tod_from, tod_to)
    from admins.services.refund_reporting import net_grouped_items
    ranked_list = net_grouped_items(
        items, refunds, ('product_id', 'product__name'),
    )
    ranked_list.sort(key=lambda r: (-(r['revenue'] or 0), r['product_id'] or 0))
    top = ranked_list[:10]
    # Slow movers = lowest-revenue products that STILL sold at least once.
    slow = sorted(ranked_list, key=lambda r: (r['revenue'] or Decimal('0')))[:10]

    def _row(r):
        return {
            'product_id': r['product_id'],
            'product_name': r['product__name'],
            'qty_sold': int(r['qty'] or 0),
            'revenue': _uzs(r['revenue']),
            'gross_qty_sold': int(r['gross_qty'] or 0),
            'refunded_qty': int(r['refund_qty'] or 0),
            'gross_revenue': _uzs(r['gross_revenue']),
            'refund_amount': _uzs(r['refund_revenue']),
        }

    gross = items.aggregate(
        units=Sum('quantity'), revenue=Sum(_LINE_TOTAL),
        products=Count('product_id', distinct=True), lines=Count('id'),
        orders=Count('order_id', distinct=True),
    )
    reversed_ = refunds.aggregate(
        units=Sum(refund_line_quantity(REFUND_EVENT_ALIAS)),
        revenue=Sum(refund_line_revenue(REFUND_EVENT_ALIAS)),
        lines=Count(
            'id', filter=Q(refund_event__source='ORDER_CANCEL'),
        ),
        orders=Count(
            'order_id',
            filter=Q(refund_event__source='ORDER_CANCEL'),
            distinct=True,
        ),
    )
    gross_revenue = gross['revenue'] or Decimal('0')
    refund_amount = reversed_['revenue'] or Decimal('0')
    revenue = gross_revenue - refund_amount
    lines = (gross['lines'] or 0) - (reversed_['lines'] or 0)
    return {
        'range': {'from': date_from.isoformat(), 'to': date_to.isoformat()},
        'window_days': (date_to - date_from).days + 1,
        'total_revenue': _uzs(revenue),
        'gross_revenue': _uzs(gross_revenue),
        'refund_amount': _uzs(refund_amount),
        'total_units': int((gross['units'] or 0) - (reversed_['units'] or 0)),
        'gross_units': int(gross['units'] or 0),
        'refunded_units': int(reversed_['units'] or 0),
        'distinct_products_sold': len([
            row for row in ranked_list if row['qty'] or row['revenue']
        ]),
        'order_lines': lines,
        'orders': (gross['orders'] or 0) - (reversed_['orders'] or 0),
        'gross_orders': gross['orders'] or 0,
        'refunded_orders': reversed_['orders'] or 0,
        'avg_line_revenue': _uzs(revenue / lines) if lines else '0',
        'top_products': [_row(r) for r in top],
        'slowest_products': [_row(r) for r in slow],
    }


def products_categories(date_from, date_to, tod_from=None, tod_to=None):
    """Units + revenue per category over the window, with each category's share
    of total revenue."""
    items = _sold_items(date_from, date_to, tod_from, tod_to)
    refunds = _refunded_items(date_from, date_to, tod_from, tod_to)
    from admins.services.refund_reporting import net_grouped_items
    rows = net_grouped_items(
        items, refunds,
        ('product__category_id', 'product__category__name'),
    )
    rows.sort(key=lambda r: -(r['revenue'] or 0))
    total = sum((r['revenue'] or Decimal('0')) for r in rows) or Decimal('0')
    out = []
    for r in rows:
        rev = r['revenue'] or Decimal('0')
        out.append({
            'category_id': r['product__category_id'],
            'category': r['product__category__name'],
            'units': int(r['qty'] or 0),
            'revenue': _uzs(rev),
            'gross_units': int(r['gross_qty'] or 0),
            'refunded_units': int(r['refund_qty'] or 0),
            'gross_revenue': _uzs(r['gross_revenue']),
            'refund_amount': _uzs(r['refund_revenue']),
            'pct_of_revenue': float((rev / total * 100).quantize(Decimal('0.1'))) if total else 0.0,
        })
    return {
        'range': {'from': date_from.isoformat(), 'to': date_to.isoformat()},
        'total_revenue': _uzs(total),
        'categories': out,
    }


def products_pareto(date_from, date_to, tod_from=None, tod_to=None):
    """Pareto (80/20) of products by revenue: rank descending with cumulative
    share, classifying the 'vital few' (A = up to 80% of revenue, B = next 15%,
    C = the long tail)."""
    items = _sold_items(date_from, date_to, tod_from, tod_to)
    refunds = _refunded_items(date_from, date_to, tod_from, tod_to)
    from admins.services.refund_reporting import net_grouped_items
    rows = net_grouped_items(
        items, refunds, ('product_id', 'product__name'),
    )
    rows.sort(key=lambda r: -(r['revenue'] or 0))
    total = sum((r['revenue'] or Decimal('0')) for r in rows) or Decimal('0')

    products = []
    cumulative = Decimal('0')
    counts = {'A': 0, 'B': 0, 'C': 0}
    for r in rows:
        rev = r['revenue'] or Decimal('0')
        pct = (rev / total * 100) if total else Decimal('0')
        # Classify by the cumulative share BEFORE this item, so the product that
        # CROSSES the 80% line is still counted among the "vital few" (A) — without
        # this, a single product worth >80% of revenue lands in B.
        prev = cumulative
        cumulative += pct
        if prev < 80:
            klass = 'A'
        elif prev < 95:
            klass = 'B'
        else:
            klass = 'C'
        counts[klass] += 1
        products.append({
            'product_id': r['product_id'],
            'product_name': r['product__name'],
            'qty_sold': int(r['qty'] or 0),
            'revenue': _uzs(rev),
            'gross_qty_sold': int(r['gross_qty'] or 0),
            'refunded_qty': int(r['refund_qty'] or 0),
            'gross_revenue': _uzs(r['gross_revenue']),
            'refund_amount': _uzs(r['refund_revenue']),
            'pct_of_revenue': float(pct.quantize(Decimal('0.01'))),
            'cumulative_pct': float(cumulative.quantize(Decimal('0.01'))),
            'class': klass,
        })

    n = len(products)
    return {
        'range': {'from': date_from.isoformat(), 'to': date_to.isoformat()},
        'total_revenue': _uzs(total),
        'products': products,
        'summary': {
            'total_products': n,
            'vital_few': counts['A'],
            'vital_few_pct_of_products': round(counts['A'] / n * 100, 1) if n else 0.0,
            'A_items': counts['A'], 'B_items': counts['B'], 'C_items': counts['C'],
        },
    }


def products_trends(date_from, date_to, top_n=5, tod_from=None, tod_to=None):
    """Daily sales trend (business-day buckets) for the window, plus a per-day
    series for the top-N products by revenue."""
    from base.services.business_day import business_day_date_expr
    items = _sold_items(date_from, date_to, tod_from, tod_to)
    refunds = _refunded_items(date_from, date_to, tod_from, tod_to)
    from admins.services.refund_reporting import net_grouped_items

    # Completed product sales follow settlement, not ticket creation. The
    # business-date expression also moves a pre-cutover paid_at back one day.
    sale_bday = business_day_date_expr('order__paid_at')
    refund_bday = business_day_date_expr(
        f'{REFUND_EVENT_ALIAS}__refunded_at'
    )

    daily = net_grouped_items(
        items.annotate(bday=sale_bday),
        refunds.annotate(bday=refund_bday),
        ('bday',),
    )
    daily.sort(key=lambda row: row['bday'])
    series = [{
        'date': d['bday'].isoformat() if d['bday'] else None,
        'units': int(d['qty'] or 0),
        'revenue': _uzs(d['revenue']),
        'gross_units': int(d['gross_qty'] or 0),
        'refunded_units': int(d['refund_qty'] or 0),
        'gross_revenue': _uzs(d['gross_revenue']),
        'refund_amount': _uzs(d['refund_revenue']),
    } for d in daily]

    # Top-N products by total revenue, then their per-business-day points.
    top = net_grouped_items(
        items, refunds, ('product_id', 'product__name'),
    )
    top.sort(key=lambda row: (-(row['revenue'] or 0), row['product_id'] or 0))
    top = top[:top_n]
    top_ids = [t['product_id'] for t in top]
    per_product = {}
    if top_ids:
        points = net_grouped_items(
            items.filter(product_id__in=top_ids).annotate(bday=sale_bday),
            refunds.filter(product_id__in=top_ids).annotate(bday=refund_bday),
            ('product_id', 'bday'),
        )
        points.sort(key=lambda row: (row['product_id'], row['bday']))
        for d in points:
            per_product.setdefault(d['product_id'], []).append({
                'date': d['bday'].isoformat() if d['bday'] else None,
                'qty': int(d['qty'] or 0),
                'revenue': _uzs(d['revenue']),
                'gross_qty': int(d['gross_qty'] or 0),
                'refunded_qty': int(d['refund_qty'] or 0),
                'gross_revenue': _uzs(d['gross_revenue']),
                'refund_amount': _uzs(d['refund_revenue']),
            })

    top_products_trend = [{
        'product_id': t['product_id'],
        'product_name': t['product__name'],
        'total_revenue': _uzs(t['revenue']),
        'gross_revenue': _uzs(t['gross_revenue']),
        'refund_amount': _uzs(t['refund_revenue']),
        'points': per_product.get(t['product_id'], []),
    } for t in top]

    return {
        'range': {'from': date_from.isoformat(), 'to': date_to.isoformat()},
        'daily': series,
        'top_products_trend': top_products_trend,
    }


def products_affinity(date_from, date_to, limit=10):
    """Market-basket co-occurrence (item 16): which products are bought together.

    Returns the top-N products (by the number of PAID orders each appears in) and the
    co-occurrence count for every pair where BOTH products are in the top-N. In the
    pairs, `a`/`b` are INDICES into products[] (not product ids), a<b, count>0. The
    FE computes lift = count*N / (A.orders*B.orders) using totalOrders + each product's
    `orders`. Business-day windowed; one query + a Python pass over the baskets."""
    from itertools import combinations
    from base.models import OrderItem, Product

    limit = max(1, min(int(limit or 10), 25))
    lo, hi = _window(date_from, date_to)

    # Sale baskets and refund baskets are distinct dated events. If both happen
    # inside the selected window they cancel without rewriting either fact.
    rows = (
        OrderItem.objects.filter(
            is_deleted=False, order__is_deleted=False, order__is_paid=True,
            order__paid_at__gte=lo, order__paid_at__lt=hi,
        )
        .values_list('order_id', 'product_id')
    )
    refund_rows = (
        _refunded_items(date_from, date_to)
        .filter(refund_event__source='ORDER_CANCEL')
        .values_list('order_id', 'product_id')
    )

    baskets = {}  # order_id -> {product_id, ...} (distinct products per order)
    for oid, pid in rows:
        if pid is not None:
            baskets.setdefault(oid, set()).add(pid)
    refund_baskets = {}
    for oid, pid in refund_rows:
        # Affinity describes the sale cohort selected above.  A cancellation
        # from a different reporting window is a valid negative money event,
        # but there is no basket in this cohort for it to neutralize.  Counting
        # it here produced negative totalOrders/product counts on refund-only
        # days and invalid lift denominators.
        if oid in baskets and pid is not None:
            refund_baskets.setdefault(oid, set()).add(pid)

    total_orders = len(baskets) - len(refund_baskets)
    product_orders = {}   # pid -> # orders it appears in
    pair_counts = {}      # (min_pid, max_pid) -> co-occurrence count
    def apply_baskets(event_baskets, sign):
        for pids in event_baskets.values():
            for pid in pids:
                product_orders[pid] = product_orders.get(pid, 0) + sign
            for a, b in combinations(sorted(pids), 2):
                pair_counts[(a, b)] = pair_counts.get((a, b), 0) + sign

    apply_baskets(baskets, 1)
    apply_baskets(refund_baskets, -1)

    # Top-N products by order appearances (desc; stable tie-break by id).
    top_ids = [pid for pid, count in
               sorted(product_orders.items(), key=lambda kv: (-kv[1], kv[0]))
               if count > 0][:limit]
    index = {pid: i for i, pid in enumerate(top_ids)}

    detail = {p['id']: p for p in Product.objects.filter(id__in=top_ids)
              .values('id', 'name', 'price', 'colors')}
    products = []
    for pid in top_ids:
        d = detail.get(pid, {})
        cols = d.get('colors') or []
        products.append({
            'id': pid,
            'name': d.get('name'),
            'color': cols[0] if isinstance(cols, list) and cols else None,
            'orders': product_orders[pid],
            'price': _uzs(d.get('price')),
        })

    # Keep only pairs whose BOTH endpoints are top-N; remap product ids -> indices (a<b).
    pairs = []
    for (a_pid, b_pid), cnt in pair_counts.items():
        if cnt > 0 and a_pid in index and b_pid in index:
            ia, ib = index[a_pid], index[b_pid]
            if ia > ib:
                ia, ib = ib, ia
            pairs.append({'a': ia, 'b': ib, 'count': cnt})
    pairs.sort(key=lambda p: (-p['count'], p['a'], p['b']))

    return {
        'range': {'from': date_from.isoformat(), 'to': date_to.isoformat()},
        'products': products,
        'pairs': pairs,
        'totalOrders': total_orders,
    }
