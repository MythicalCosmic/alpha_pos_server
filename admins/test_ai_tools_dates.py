"""Regression for the live (OpenAI tool-use) AI path — ai_tools_service:
- query_db must refuse an order-level money Sum across a to-many join (fan-out).
- get_overview 'today' revenue must be business-day + paid + non-cancelled only.
"""
import json
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def _exec(name, args):
    from stock.services.ai_tools_service import AIToolbox
    r = AIToolbox.execute(name, args)
    return json.loads(r) if isinstance(r, str) else r


def _user():
    import secrets
    from base.models import User
    return User.objects.create(email=f'{secrets.token_hex(4)}@x.local', first_name='U',
                               last_name='X', role='CASHIER', status='ACTIVE', password='!')


def _order(total, is_paid=True, status='COMPLETED'):
    """Order created now (inside the current business day)."""
    from base.models import Order
    u = _user()
    o = Order.objects.create(
        user=u, cashier=u, order_type='HALL', status=status, is_paid=is_paid,
        payment_method='CASH' if is_paid else None,
        total_amount=Decimal(total), subtotal=Decimal(total),
        display_id=Order.objects.count() + 1)
    if is_paid:
        Order.objects.filter(pk=o.pk).update(paid_at=timezone.now())
    return o


def test_query_db_blocks_join_fanout():
    _order(100000)
    res = _exec('query_db', {
        'model': 'order',
        'filters': {'items__product__name__icontains': 'Pizza'},
        'aggregate': {'rev': 'sum:total_amount'},
    })
    assert 'error' in res and 'fan-out' in res['error'].lower(), res


def test_query_db_allows_orderitem_side_sum():
    # Summing on the item side (quantity) is fine — no order-level fan-out.
    res = _exec('query_db', {
        'model': 'orderitem',
        'aggregate': {'units': 'sum:quantity'},
    })
    assert 'error' not in res, res


def test_overview_today_revenue_excludes_unpaid_and_cancelled():
    _order(100000, is_paid=True, status='COMPLETED')    # counts
    _order(50000, is_paid=False, status='OPEN')          # unpaid -> excluded
    _order(30000, is_paid=True, status='CANCELED')       # cancelled -> excluded
    res = _exec('get_overview', {})
    ts = res['today_sales']
    assert ts['paid_revenue_uzs'] == 100000.0, ts
    assert ts['orders'] == 3 and ts['paid_orders'] == 2, ts
