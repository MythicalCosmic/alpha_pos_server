from decimal import Decimal

from django.db import migrations


PROVIDER_METHOD = {'CASH': 'CASH', 'CARD': 'UZCARD', 'QR': 'PAYME'}
CARD_METHODS = {'UZCARD', 'HUMO', 'CARD'}


def migrate_refunded_payments(apps, schema_editor):
    """Move mutable REFUNDED rows into the immutable shared refund ledger."""
    Payment = apps.get_model('couriers', 'CourierPayment')
    OrderRefund = apps.get_model('base', 'OrderRefund')
    Order = apps.get_model('base', 'Order')
    OrderPayment = apps.get_model('base', 'OrderPayment')

    affected = set()
    rows = Payment.objects.filter(status='REFUNDED').order_by(
        'refunded_at', 'created_at', 'pk',
    )
    for payment in rows.iterator(chunk_size=500):
        source_id = payment.external_id or f'legacy-payment:{payment.pk}'
        method = PROVIDER_METHOD.get(payment.provider)
        amount = Decimal(payment.amount or 0)
        if amount > 0 and method:
            values = {
                'order_id': payment.order_id,
                'shift_id': None,
                'cashier_id': None,
                'amount': amount,
                'cash_amount': amount if method == 'CASH' else Decimal('0'),
                'drawer_cash_amount': Decimal('0'),
                'card_amount': amount if method in CARD_METHODS else Decimal('0'),
                'payme_amount': amount if method == 'PAYME' else Decimal('0'),
                'unknown_amount': Decimal('0'),
                'card_detail': {
                    key: str(amount if method == key else Decimal('0'))
                    for key in ('UZCARD', 'HUMO', 'CARD')
                },
                'refunded_at': (
                    payment.refunded_at or payment.paid_at or payment.created_at
                ),
                'register_command': False,
                'reason': f'Legacy courier payment #{payment.pk} refund (migrated)',
                'branch_id': payment.branch_id or payment.order.branch_id or '',
                'synced_at': None,
            }
            OrderRefund.objects.get_or_create(
                source='COURIER_PAYMENT', source_id=source_id,
                defaults=values,
            )
        # The positive provider event is historical sale evidence. Its old
        # refunded_at is now copied to OrderRefund and is cleared here so there
        # is exactly one authoritative negative event.
        Payment.objects.filter(pk=payment.pk).update(
            status='PAID', refunded_at=None,
        )
        affected.add(payment.order_id)

    # Old runtime code erased the rolled-up paid header after changing the
    # payment status. Rebuild it from the now-restored positive evidence.
    for order in Order.objects.filter(pk__in=affected).iterator(chunk_size=500):
        due = Decimal(order.total_amount or 0)
        methods = set()
        stamps = []
        till_noncash = Decimal('0')
        till_cash = Decimal('0')
        for method, amount, created_at in OrderPayment.objects.filter(
            order_id=order.pk, is_deleted=False,
        ).values_list('method', 'amount', 'created_at'):
            method = (method or 'CASH').upper()
            amount = Decimal(amount or 0)
            if method == 'CASH':
                till_cash += max(amount, Decimal('0'))
            elif method in CARD_METHODS or method == 'PAYME':
                till_noncash += max(amount, Decimal('0'))
                if amount > 0:
                    methods.add(method)
            if created_at:
                stamps.append(created_at)
        till_total = till_noncash + min(
            till_cash, max(due - till_noncash, Decimal('0')),
        )
        if till_cash and till_total > till_noncash:
            methods.add('CASH')

        courier_total = Decimal('0')
        for provider, amount, paid_at, created_at in Payment.objects.filter(
            order_id=order.pk, status='PAID',
        ).values_list('provider', 'amount', 'paid_at', 'created_at'):
            amount = Decimal(amount or 0)
            courier_total += max(amount, Decimal('0'))
            method = PROVIDER_METHOD.get(provider)
            if method and amount > 0:
                methods.add(method)
            if paid_at or created_at:
                stamps.append(paid_at or created_at)

        if due > 0 and till_total + courier_total >= due:
            rolled = next(iter(methods)) if len(methods) == 1 else 'MIXED'
            Order.objects.filter(pk=order.pk).update(
                is_paid=True,
                payment_method=rolled,
                paid_at=max(stamps) if stamps else order.created_at,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0043_order_refund_ledger'),
        ('couriers', '0003_courierpayment_external_id_unique'),
    ]

    operations = [
        migrations.RunPython(
            migrate_refunded_payments,
            migrations.RunPython.noop,
        ),
    ]
