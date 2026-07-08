"""Fiscal receipt tender declaration.

The FISCAL partition is CASH vs ALL-NON-CASH (Payme is electronic money -> card).
received_cash must be the BILL's cash portion, never the tendered cash (which
includes the customer's change), and the two fields must reconcile to `total`.
"""
import secrets
from decimal import Decimal

import pytest

from fiscalization.services.builder import build_receipt_payload

pytestmark = pytest.mark.django_db
D = Decimal
TENANT = {'tin': '123456789', 'vat_percent': 0}


def _order(total, method, lines=()):
    from base.models import Order, OrderPayment, User
    u = User.objects.create(email=f'{secrets.token_hex(4)}@x.local', first_name='U',
                            last_name='X', role='CASHIER', status='ACTIVE', password='!')
    o = Order.objects.create(user=u, cashier=u, order_type='HALL', status='COMPLETED',
                             is_paid=True, payment_method=method,
                             total_amount=D(total), subtotal=D(total),
                             display_id=Order.objects.count() + 1)
    for m, a in lines:
        OrderPayment.objects.create(order=o, method=m, amount=D(a))
    return o


def _p(order):
    return build_receipt_payload(order, TENANT)


def test_pure_cash():
    p = _p(_order(60000, 'CASH', [('CASH', 60000)]))
    assert p['received_cash'] == 6000000 and p['received_card'] == 0
    assert p['received_cash'] + p['received_card'] == p['total']


def test_mixed_declares_the_real_split_not_100pct_card():
    # bill 53,000 = 35,000 cash + 18,000 uzcard. OLD code declared 100% card.
    p = _p(_order(53000, 'MIXED', [('CASH', 35000), ('UZCARD', 18000)]))
    assert p['received_card'] == 1800000
    assert p['received_cash'] == 3500000
    assert p['received_cash'] + p['received_card'] == p['total'] == 5300000


def test_payme_is_declared_as_card_never_cash():
    p = _p(_order(10000, 'PAYME', [('PAYME', 10000)]))
    assert p['received_card'] == 1000000, p
    assert p['received_cash'] == 0, p


def test_cash_over_tender_does_not_break_the_receipt():
    # customer hands 100,000 for a 5,000 bill; OP stores the tendered 100,000.
    p = _p(_order(5000, 'CASH', [('CASH', 100000)]))
    assert p['received_cash'] == 500000 and p['received_card'] == 0
    assert p['received_cash'] + p['received_card'] == p['total']   # OFD would reject otherwise


def test_card_value_declared_as_card():
    p = _p(_order(40000, 'CARD', [('CARD', 40000)]))
    assert p['received_card'] == 4000000 and p['received_cash'] == 0


def test_unresolvable_mixed_refuses_to_fiscalize():
    # MIXED with no payment lines -> guessing would misdeclare to the tax authority.
    with pytest.raises(ValueError, match='cannot fiscalize'):
        _p(_order(100000, 'MIXED', lines=()))


def test_order_without_lines_falls_back_to_rolled_up_method():
    p = _p(_order(70000, 'UZCARD', lines=()))
    assert p['received_card'] == 7000000 and p['received_cash'] == 0
