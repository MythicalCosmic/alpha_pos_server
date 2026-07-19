from decimal import Decimal, InvalidOperation

from django.db import migrations


PROVIDER_TO_METHOD = {
    'CASH': 'CASH',
    'CARD': 'UZCARD',
    'QR': 'PAYME',
}


def _fail(payment, message):
    raise RuntimeError(
        f'Cannot backfill courier payment #{payment.pk}: {message}',
    )


def _ensure_legacy_refund(OrderRefund, payment, *, order, branch_id,
                          source_id, method, amount):
    """Pair an old mutable REFUNDED status with one immutable negative event."""
    refunded_at = payment.refunded_at or payment.paid_at or payment.created_at
    if refunded_at is None:
        _fail(payment, 'refund timestamp is missing')
    zero = Decimal('0')
    expected = {
        'order_id': order.pk,
        'branch_id': branch_id,
        'amount': amount,
        'cash_amount': amount if method == 'CASH' else zero,
        'drawer_cash_amount': zero,
        'card_amount': amount if method in {'UZCARD', 'HUMO', 'CARD'} else zero,
        'payme_amount': amount if method == 'PAYME' else zero,
        'unknown_amount': zero,
        'card_detail': {
            key: str(amount if method == key else zero)
            for key in ('UZCARD', 'HUMO', 'CARD')
        },
        'refunded_at': refunded_at,
        'register_command': False,
        'is_deleted': False,
    }
    refund = OrderRefund.objects.filter(
        source='COURIER_PAYMENT', source_id=source_id,
    ).first()
    if refund is None:
        OrderRefund.objects.create(
            shift_id=None,
            cashier_id=None,
            source='COURIER_PAYMENT',
            source_id=source_id,
            reason=(
                f'Legacy courier payment #{payment.pk} refund '
                '(external evidence backfill)'
            ),
            synced_at=None,
            **{name: value for name, value in expected.items()
               if name != 'is_deleted'},
        )
        return

    actual = {
        'order_id': refund.order_id,
        'branch_id': str(refund.branch_id or '').strip(),
        'amount': Decimal(str(refund.amount)),
        'cash_amount': Decimal(str(refund.cash_amount)),
        'drawer_cash_amount': Decimal(str(refund.drawer_cash_amount)),
        'card_amount': Decimal(str(refund.card_amount)),
        'payme_amount': Decimal(str(refund.payme_amount)),
        'unknown_amount': Decimal(str(refund.unknown_amount)),
        'card_detail': refund.card_detail,
        'refunded_at': refund.refunded_at,
        'register_command': refund.register_command,
        'is_deleted': refund.is_deleted,
    }
    conflicts = [
        name for name, value in expected.items() if actual[name] != value
    ]
    if conflicts:
        _fail(
            payment,
            'immutable refund evidence conflicts on ' + ', '.join(conflicts),
        )


def backfill_external_payment_evidence(apps, schema_editor):
    """Freeze all historical positive courier collections for sync.

    The migration is atomic by default. It intentionally raises on ambiguous
    ownership or a reused event identity instead of publishing guessed money.
    """
    CourierPayment = apps.get_model('couriers', 'CourierPayment')
    ExternalOrderPayment = apps.get_model('base', 'ExternalOrderPayment')
    OrderRefund = apps.get_model('base', 'OrderRefund')

    payments = (
        CourierPayment.objects.filter(status__in=['PAID', 'REFUNDED'])
        .select_related('order')
        .order_by('created_at', 'pk')
    )
    for payment in payments.iterator(chunk_size=500):
        order = payment.order
        branch_id = str(order.branch_id or '').strip()
        payment_branch = str(payment.branch_id or '').strip()
        source_id = str(payment.external_id or '')
        method = PROVIDER_TO_METHOD.get(payment.provider)
        occurred_at = payment.paid_at or payment.created_at
        try:
            amount = Decimal(str(payment.amount))
        except (InvalidOperation, TypeError, ValueError):
            _fail(payment, 'amount is invalid')

        if not branch_id:
            _fail(payment, 'order has no branch ownership')
        if payment_branch and payment_branch != branch_id:
            _fail(payment, 'payment and order branches differ')
        if not source_id.strip():
            _fail(payment, 'external_id is blank')
        if method is None:
            _fail(payment, f'provider {payment.provider!r} is unsupported')
        if not amount.is_finite() or amount <= 0:
            _fail(payment, 'amount is not positive')
        if occurred_at is None:
            _fail(payment, 'occurrence timestamp is missing')

        matches = list(
            ExternalOrderPayment.objects.filter(
                branch_id=branch_id,
                source='COURIER', source_id=source_id,
            ).order_by('pk')[:2]
        )
        if len(matches) > 1:
            _fail(payment, 'event identity is duplicated within the branch')
        if matches:
            evidence = matches[0]
            expected = {
                'branch_id': branch_id,
                'order_id': order.pk,
                'source': 'COURIER',
                'source_id': source_id,
                'method': method,
                'amount': amount,
                'occurred_at': occurred_at,
                'is_deleted': False,
            }
            actual = {
                'branch_id': str(evidence.branch_id or '').strip(),
                'order_id': evidence.order_id,
                'source': evidence.source,
                'source_id': evidence.source_id,
                'method': evidence.method,
                'amount': Decimal(str(evidence.amount)),
                'occurred_at': evidence.occurred_at,
                'is_deleted': evidence.is_deleted,
            }
            conflicts = [
                name for name, value in expected.items()
                if actual[name] != value
            ]
            if conflicts:
                _fail(
                    payment,
                    'immutable evidence conflicts on ' + ', '.join(conflicts),
                )
        else:
            ExternalOrderPayment.objects.create(
                order_id=order.pk,
                source='COURIER',
                source_id=source_id,
                method=method,
                amount=amount,
                occurred_at=occurred_at,
                branch_id=branch_id,
            )

        if payment.status == 'REFUNDED':
            _ensure_legacy_refund(
                OrderRefund, payment, order=order, branch_id=branch_id,
                source_id=source_id, method=method, amount=amount,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0052_external_order_payment'),
        ('couriers', '0008_canonical_unique_courier_phone'),
    ]

    operations = [
        migrations.RunPython(
            backfill_external_payment_evidence,
            migrations.RunPython.noop,
        ),
    ]
