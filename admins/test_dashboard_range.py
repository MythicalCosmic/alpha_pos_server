"""Date-range dashboard (GET /dashboard?from&to), sidebar-counts, and the
/orders/stats payment_breakdown."""
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def _u():
    from base.models import User
    return User.objects.create(email=f'd{secrets.token_hex(4)}@x.local', first_name='a',
                               last_name='b', role='CASHIER', status='ACTIVE', password='!')


def _order(method='CASH', total='100', cancelled=False, paid=True):
    from base.models import Order, OrderRefund
    order = Order.objects.create(
        user=_u(), cashier=_u(),
        status='CANCELED' if cancelled else 'COMPLETED',
        is_paid=paid, display_id=1, subtotal=total, total_amount=total,
        payment_method=(method if paid else None),
        paid_at=(timezone.now() if paid else None))
    if cancelled and paid:
        amount = Decimal(total)
        kwargs = {
            'cash_amount': amount,
            'drawer_cash_amount': amount,
        } if method == 'CASH' else {'card_amount': amount}
        OrderRefund.objects.create(
            order=order,
            branch_id=order.branch_id,
            amount=amount,
            source=OrderRefund.Source.ORDER_CANCEL,
            source_id=f'dashboard-range-cancel-{order.pk}',
            refunded_at=order.paid_at,
            **kwargs,
        )
    return order


def test_get_range_today_revenue_and_payment():
    from admins.services import dashboard_service
    _order('CASH', '100')
    _order('UZCARD', '50')
    _order('CASH', '30', cancelled=True)          # cancelled -> excluded
    data = dashboard_service.get_range()           # default = today
    assert data['orders'] == 3
    assert data['paid_orders'] == 3                # immutable gross paid headers
    assert Decimal(data['revenue']) == 150
    # Canonical tenders: UZCARD folds into `card`; no MIXED bucket.
    assert Decimal(data['payment_breakdown']['cash']) == 100
    assert Decimal(data['payment_breakdown']['card']) == 50
    assert Decimal(data['payment_breakdown']['card_detail']['UZCARD']) == 50


def test_get_range_window_excludes_other_days():
    from admins.services import dashboard_service
    from base.models import Order
    o = _order('CASH', '999')
    old = timezone.now() - timedelta(days=10)
    Order.objects.filter(pk=o.pk).update(created_at=old, paid_at=old)
    _order('CASH', '100')                          # today
    data = dashboard_service.get_range()            # today only
    assert Decimal(data['revenue']) == 100


def test_sidebar_counts():
    from admins.services import dashboard_service
    from base.models import Shift
    Shift.objects.create(user=_u(), start_time=timezone.now(), status='ACTIVE')
    _order('CASH', '200')
    data = dashboard_service.get_sidebar_counts()
    assert data['active_shifts'] == 1
    assert data['today_orders'] >= 1
    assert Decimal(data['today_revenue']) == 200


def test_order_stats_payment_breakdown():
    from admins.services.order_service import AdminOrderService
    _order('CASH', '100')
    _order('HUMO', '40')                            # -> card
    _order('PAYME', '25')                           # -> payme (own tender)
    body, st = AdminOrderService.get_order_stats()
    assert st == 200
    pb = body['data']['payment_breakdown']
    assert Decimal(pb['cash']) == 100
    assert Decimal(pb['card']) == 40
    assert Decimal(pb['payme']) == 25
    # buckets reconcile exactly to revenue
    assert Decimal(pb['cash']) + Decimal(pb['card']) + Decimal(pb['payme']) == 165
