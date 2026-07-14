"""Helpers for sale-at-paid_at / refund-at-refunded_at reporting."""
from decimal import Decimal

from django.db.models import Count, Q, Sum

from base.models import OrderRefund
from base.services.business_day import tod_filter
from base.services.revenue import net_line_revenue
from base.services.refund_lines import (
    REFUND_EVENT_ALIAS, refund_item_events as filtered_refund_item_events,
    refund_line_quantity, refund_line_revenue,
)


def refund_events(lo=None, hi=None, *, tod_from=None, tod_to=None, **filters):
    qs = OrderRefund.objects.filter(is_deleted=False, **filters)
    if lo is not None:
        qs = qs.filter(refunded_at__gte=lo)
    if hi is not None:
        qs = qs.filter(refunded_at__lt=hi)
    return tod_filter(qs, tod_from, tod_to, field='refunded_at')


def net_revenue(sale_qs, refund_qs):
    gross = sale_qs.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
    refunded = refund_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    return gross - refunded, gross, refunded


def net_grouped_items(sale_items, refund_items, group_fields):
    """Group product lines and subtract dated refund events.

    Money-only provider refunds are allocated proportionally across the frozen
    order lines. Unit counts reverse only on ORDER_CANCEL, because refunding one
    tender leg does not prove that any particular menu item was returned.
    """
    sales = sale_items.values(*group_fields).annotate(
        qty=Sum('quantity'), revenue=Sum(net_line_revenue()),
        orders=Count('order_id', distinct=True),
    )
    refunds = refund_items.values(*group_fields).annotate(
        qty=Sum(refund_line_quantity(REFUND_EVENT_ALIAS)),
        revenue=Sum(refund_line_revenue(REFUND_EVENT_ALIAS)),
        # A provider refund changes realized money, not whether the original
        # product sale occurred. Reverse the sold-order count only for the one
        # terminal cancellation event.
        orders=Count(
            'order_id',
            filter=Q(refund_event__source=OrderRefund.Source.ORDER_CANCEL),
            distinct=True,
        ),
    )

    def key(row):
        return tuple(row.get(field) for field in group_fields)

    merged = {key(row): dict(row) for row in sales}
    for row in refunds:
        target = merged.setdefault(key(row), {
            field: row.get(field) for field in group_fields
        })
        target['refund_qty'] = row['qty'] or 0
        target['refund_revenue'] = row['revenue'] or Decimal('0.00')
        target['refund_orders'] = row['orders'] or 0

    for row in merged.values():
        gross_qty = row.get('qty') or 0
        gross_revenue = row.get('revenue') or Decimal('0.00')
        gross_orders = row.get('orders') or 0
        row['gross_qty'] = gross_qty
        row['gross_revenue'] = gross_revenue
        row['gross_orders'] = gross_orders
        row.setdefault('refund_qty', 0)
        row.setdefault('refund_revenue', Decimal('0.00'))
        row.setdefault('refund_orders', 0)
        row['qty'] = gross_qty - row['refund_qty']
        row['revenue'] = gross_revenue - row['refund_revenue']
        row['orders'] = gross_orders - row['refund_orders']
    return list(merged.values())


def refund_item_events(lo, hi, *, tod_from=None, tod_to=None):
    qs = filtered_refund_item_events(
        refunded_at__gte=lo,
        refunded_at__lt=hi,
    )
    return tod_filter(
        qs, tod_from, tod_to, field=f'{REFUND_EVENT_ALIAS}__refunded_at',
    )
