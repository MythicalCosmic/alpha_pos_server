"""A checkout must remain intact when an ordinary admin edit overlaps it."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
import json
from threading import Event, Thread, current_thread

import pytest
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from admins.services.order_service import AdminOrderService
from base.models import OrderPayment, Shift
from base.repositories.order import OrderRepository


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('edit', ['increase_quantity', 'decrease_quantity', 'note'])
def test_checkout_survives_overlapping_admin_edit(
    edit, monkeypatch, settings, cashier_user, order_factory,
):
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL concurrent transactions and row locks')
    settings.DEPLOYMENT_MODE = 'cloud'
    order = order_factory(cashier=cashier_user)
    item = order.items.get()
    item.price = Decimal('10000')
    item.quantity = 2
    item.save(update_fields=['price', 'quantity'])
    order.subtotal = order.total_amount = Decimal('20000')
    order.save(update_fields=['subtotal', 'total_amount'])
    Shift.objects.create(
        user=cashier_user,
        status='ACTIVE',
        branch_id=order.branch_id,
        start_time=timezone.now() - timedelta(minutes=1),
    )

    payment_locked = Event()
    allow_payment = Event()
    payment_finished = Event()
    results, failures = {}, []
    original_read = OrderRepository.get_by_id
    original_locked_read = OrderRepository.get_for_update

    def unlocked_read(*args, **kwargs):
        snapshot = original_read(*args, **kwargs)
        if current_thread().name == 'order-edit':
            # The ordinary SELECT can see the unpaid version while checkout
            # holds the row lock. Finish checkout before that edit writes.
            allow_payment.set()
            assert payment_finished.wait(10), 'checkout did not finish'
        return snapshot

    def locked_read(*args, **kwargs):
        if current_thread().name == 'order-edit':
            allow_payment.set()
        snapshot = original_locked_read(*args, **kwargs)
        if current_thread().name == 'order-payment':
            payment_locked.set()
            assert allow_payment.wait(10), 'edit did not attempt its read'
        return snapshot

    monkeypatch.setattr(OrderRepository, 'get_by_id', staticmethod(unlocked_read))
    monkeypatch.setattr(OrderRepository, 'get_for_update', staticmethod(locked_read))

    def run(name, operation):
        close_old_connections()
        try:
            results[name] = operation()
        except BaseException as exc:
            failures.append(exc)
        finally:
            if name == 'payment':
                payment_finished.set()
            close_old_connections()

    def edit_order():
        if edit == 'note':
            return AdminOrderService.update_order(order.id, description='No onions')
        return AdminOrderService.update_order_item(
            order.id, item.id, 3 if edit == 'increase_quantity' else 1,
        )

    payment_thread = Thread(
        name='order-payment', target=run,
        args=('payment', lambda: AdminOrderService.mark_as_paid(
            order.id, payment_method='UZCARD', discount_percent=10,
        )),
    )
    edit_thread = Thread(name='order-edit', target=run, args=('edit', edit_order))
    payment_thread.start()
    try:
        assert payment_locked.wait(10), 'checkout did not lock the order'
        edit_thread.start()
        edit_thread.join(20)
    finally:
        allow_payment.set()
        payment_thread.join(20)
        if edit_thread.ident is not None:
            edit_thread.join(20)
    assert not payment_thread.is_alive() and not edit_thread.is_alive()
    assert failures == []
    assert results['payment'][1] == 200, results
    order.refresh_from_db()
    item.refresh_from_db()
    collected = sum(OrderPayment.objects.filter(order=order).values_list('amount', flat=True))
    assert (order.is_paid, order.total_amount, item.quantity) == (
        True, collected, 2,
    ), f'{edit}: collected {collected}, order stores {order.total_amount}, paid={order.is_paid}'
    assert collected == Decimal('18000.00')
    assert order.paid_at is not None and order.payment_action_id is not None
    if edit == 'note':
        assert results['edit'][1] == 200, results
        assert order.description == 'No onions'
    else:
        assert results['edit'][1] == 400, results

    # Exercise the standalone audit's PostgreSQL read-only snapshot after the
    # two real transactions have committed, not inside a pytest rollback block.
    output = StringIO()
    with CaptureQueriesContext(connection) as queries:
        call_command(
            'audit_tender_attribution', '--branch', order.branch_id,
            '--json', stdout=output,
        )
    assert json.loads(output.getvalue())['internally_consistent'] is True
    assert any('REPEATABLE READ, READ ONLY' in row['sql'] for row in queries)
    assert not any(
        row['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE '))
        for row in queries
    )
