from uuid import uuid4

import pytest


pytestmark = pytest.mark.django_db


def _paid_order(*, method, action_id):
    from base.models import Order, User

    cashier = User.objects.create(
        email=f'{method.lower()}-{action_id}@test.local',
        first_name='Action',
        last_name='Report',
        password='!',
        role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE,
        branch_id='branch1',
    )
    return Order.objects.create(
        user=cashier,
        cashier=cashier,
        branch_id='branch1',
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=method,
        payment_action_id=action_id,
        subtotal='100.00',
        total_amount='100.00',
    )


@pytest.mark.parametrize('method', ['CASH', 'HUMO'])
def test_order_report_marks_action_header_without_children_unknown(method):
    from admins.services.order_service import _payments_payload

    order = _paid_order(method=method, action_id=uuid4())

    payload = _payments_payload(order)

    assert payload == {
        'cash': '0.00',
        'card': '0.00',
        'payme': '0.00',
        'unknown': '100.00',
    }


def test_order_report_accepts_matching_action_child_evidence():
    from base.models import OrderPayment
    from admins.services.order_service import _payments_payload

    action_id = uuid4()
    order = _paid_order(method='HUMO', action_id=action_id)
    OrderPayment.objects.create(
        order=order,
        branch_id=order.branch_id,
        method='HUMO',
        amount='100.00',
        payment_action_id=action_id,
        line_index=0,
    )

    payload = _payments_payload(order)

    assert payload == {
        'cash': '0.00',
        'card': '100.00',
        'payme': '0.00',
        'card_detail': {'HUMO': '100.00'},
    }
