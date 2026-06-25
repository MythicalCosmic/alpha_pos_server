"""Products dashboard analytics: overview, per-category, Pareto, and trends.

Pure derivations over Order / OrderItem — no new models. Every window is bounded
on the BUSINESS day (AppSettings.business_day_start, default 03:00) so a 01:00 sale
counts toward the night before, consistent with the dashboard and order stats.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import (
    Count, DateTimeField, DecimalField, ExpressionWrapper, F, Q, Sum,
)
from django.db.models.functions import TruncDate

_LINE_TOTAL = ExpressionWrapper(
    F('price') * F('quantity'),
    output_field=DecimalField(max_digits=18, decimal_places=2),
)


def _uzs(value):
    """Money as an integer-so'm string (UZS has no minor unit)."""
    try:
        return str(int(value or 0))
    except (TypeError, ValueError):
        return '0'


def _window(date_from, date_to):
    from base.services.business_day import range_window
    return range_window(date_from, date_to)


def _sold_items(date_from, date_to):
    """OrderItem queryset for non-deleted, non-cancelled orders in the business
    window [date_from, date_to]."""
    from base.models import OrderItem
    lo, hi = _window(date_from, date_to)
    return (
        OrderItem.objects.filter(
            order__is_deleted=False,
            order__created_at__gte=lo, order__created_at__lt=hi,
        )
        .exclude(order__status='CANCELED')
    )


def products_overview(date_from, date_to):
    """Headline product KPIs over the window + top sellers / slow movers."""
    items = _sold_items(date_from, date_to)
    agg = items.aggregate(
        units=Sum('quantity'),
        revenue=Sum(_LINE_TOTAL),
        distinct_products=Count('product_id', distinct=True),
        lines=Count('id'),
        orders=Count('order_id', distinct=True),
    )
    ranked = (
        items.values('product_id', 'product__name')
        .annotate(qty=Sum('quantity'), revenue=Sum(_LINE_TOTAL))
        .order_by('-revenue')
    )
    ranked_list = list(ranked)
    top = ranked_list[:10]
    # Slow movers = lowest-revenue products that STILL sold at least once.
    slow = sorted(ranked_list, key=lambda r: (r['revenue'] or Decimal('0')))[:10]

    def _row(r):
        return {
            'product_id': r['product_id'],
            'product_name': r['product__name'],
            'qty_sold': int(r['qty'] or 0),
            'revenue': _uzs(r['revenue']),
        }

    revenue = agg['revenue'] or Decimal('0')
    lines = agg['lines'] or 0
    return {
        'range': {'from': date_from.isoformat(), 'to': date_to.isoformat()},
        'window_days': (date_to - date_from).days + 1,
        'total_revenue': _uzs(revenue),
        'total_units': int(agg['units'] or 0),
        'distinct_products_sold': agg['distinct_products'] or 0,
        'order_lines': lines,
        'orders': agg['orders'] or 0,
        'avg_line_revenue': _uzs(revenue / lines) if lines else '0',
        'top_products': [_row(r) for r in top],
        'slowest_products': [_row(r) for r in slow],
    }


def products_categories(date_from, date_to):
    """Units + revenue per category over the window, with each category's share
    of total revenue."""
    items = _sold_items(date_from, date_to)
    rows = list(
        items.values('product__category_id', 'product__category__name')
        # `units` alias (not `quantity`) so the line-total F('quantity') isn't
        # resolved to the aggregate -> FieldError.
        .annotate(units=Sum('quantity'), revenue=Sum(_LINE_TOTAL))
        .order_by('-revenue')
    )
    total = sum((r['revenue'] or Decimal('0')) for r in rows) or Decimal('0')
    out = []
    for r in rows:
        rev = r['revenue'] or Decimal('0')
        out.append({
            'category_id': r['product__category_id'],
            'category': r['product__category__name'],
            'units': int(r['units'] or 0),
            'revenue': _uzs(rev),
            'pct_of_revenue': float((rev / total * 100).quantize(Decimal('0.1'))) if total else 0.0,
        })
    return {
        'range': {'from': date_from.isoformat(), 'to': date_to.isoformat()},
        'total_revenue': _uzs(total),
        'categories': out,
    }


def products_pareto(date_from, date_to):
    """Pareto (80/20) of products by revenue: rank descending with cumulative
    share, classifying the 'vital few' (A = up to 80% of revenue, B = next 15%,
    C = the long tail)."""
    items = _sold_items(date_from, date_to)
    rows = list(
        items.values('product_id', 'product__name')
        .annotate(qty=Sum('quantity'), revenue=Sum(_LINE_TOTAL))
        .order_by('-revenue')
    )
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


def products_trends(date_from, date_to, top_n=5):
    """Daily sales trend (business-day buckets) for the window, plus a per-day
    series for the top-N products by revenue."""
    from base.services.business_day import business_day_start
    items = _sold_items(date_from, date_to)

    # Bucket by BUSINESS date: subtracting the cutover shifts pre-cutover sales
    # back a day, so date(created_at - start) == the business date.
    start = business_day_start()
    offset = timedelta(hours=start.hour, minutes=start.minute, seconds=start.second)
    bday = TruncDate(
        ExpressionWrapper(F('order__created_at') - offset, output_field=DateTimeField())
    )

    daily = list(
        items.annotate(bday=bday)
        .values('bday')
        .annotate(units=Sum('quantity'), revenue=Sum(_LINE_TOTAL))
        .order_by('bday')
    )
    series = [{
        'date': d['bday'].isoformat() if d['bday'] else None,
        'units': int(d['units'] or 0),
        'revenue': _uzs(d['revenue']),
    } for d in daily]

    # Top-N products by total revenue, then their per-business-day points.
    top = list(
        items.values('product_id', 'product__name')
        .annotate(revenue=Sum(_LINE_TOTAL))
        .order_by('-revenue')[:top_n]
    )
    top_ids = [t['product_id'] for t in top]
    per_product = {}
    if top_ids:
        for d in (
            items.filter(product_id__in=top_ids)
            .annotate(bday=bday)
            .values('product_id', 'bday')
            .annotate(qty=Sum('quantity'), revenue=Sum(_LINE_TOTAL))
            .order_by('product_id', 'bday')
        ):
            per_product.setdefault(d['product_id'], []).append({
                'date': d['bday'].isoformat() if d['bday'] else None,
                'qty': int(d['qty'] or 0),
                'revenue': _uzs(d['revenue']),
            })

    top_products_trend = [{
        'product_id': t['product_id'],
        'product_name': t['product__name'],
        'total_revenue': _uzs(t['revenue']),
        'points': per_product.get(t['product_id'], []),
    } for t in top]

    return {
        'range': {'from': date_from.isoformat(), 'to': date_to.isoformat()},
        'daily': series,
        'top_products_trend': top_products_trend,
    }
