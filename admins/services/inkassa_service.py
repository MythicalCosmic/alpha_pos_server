import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum, Count
from django.utils import timezone

from base.models import CashRegister, Inkassa, Order, OrderItem, OrderRefund
from base.helpers.response import ServiceResponse
from base.repositories import CashRegisterRepository
from base.services.revenue import net_line_revenue

logger = logging.getLogger(__name__)


def _resolve_register(branch_id=None, *, for_update=False):
    """Resolve a single operational branch register.

    Local editions always use their configured branch. On the cloud an explicit
    branch wins; for backward compatibility a sole non-cloud branch is selected
    automatically. Multiple operational branches are intentionally ambiguous
    and require ``branch_id`` instead of silently collecting the wrong drawer.
    """
    requested = str(branch_id or '').strip()
    node_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
    mode = getattr(settings, 'DEPLOYMENT_MODE', 'local')

    if not requested and mode != 'cloud':
        requested = node_branch
    if not requested:
        active = CashRegister.objects.filter(is_deleted=False)
        operational = active.exclude(branch_id=node_branch) if node_branch else active
        branch_ids = list(
            operational.order_by().values_list('branch_id', flat=True).distinct()[:2]
        )
        if len(branch_ids) == 1:
            requested = branch_ids[0]
        else:
            all_ids = list(
                active.order_by().values_list('branch_id', flat=True).distinct()[:2]
            )
            if len(all_ids) == 1:
                requested = all_ids[0]
            elif len(branch_ids) > 1 or len(all_ids) > 1:
                return None, ServiceResponse.validation_error(
                    errors={'branch_id': 'Required when more than one branch has a register'},
                    message='Choose a branch register',
                )
            else:
                requested = node_branch or 'cloud'

    register = CashRegisterRepository.get_or_create_current(
        requested, for_update=for_update,
    )
    return register, None


class AdminInkassaService:

    @staticmethod
    def get_balance(branch_id=None):
        register, error = _resolve_register(branch_id)
        if error:
            return error
        pending = Inkassa.pending_register_amount(register)
        available = (register.current_balance or Decimal('0')) - pending
        return ServiceResponse.success(data={
            'branch_id': register.branch_id,
            'balance': str(available),
            'reported_balance': str(register.current_balance),
            'pending_cash_commands': str(pending),
            'last_updated': register.last_updated.isoformat(),
        })

    @staticmethod
    def get_stats(branch_id=None):
        # Match every other money surface: "today" is the configured business
        # day (03:00 by default), not calendar midnight.
        from base.services.business_day import today_window
        today_start, today_end = today_window()

        # A sale remains a positive event at paid_at even when its later
        # operational status becomes CANCELED. Its dated OrderRefund is the
        # separate negative event; status filtering erases the original sale.
        today_orders = Order.objects.filter(
            is_deleted=False, is_paid=True,
            paid_at__gte=today_start, paid_at__lt=today_end,
        )
        today_refunds = OrderRefund.objects.filter(
            is_deleted=False,
            refunded_at__gte=today_start, refunded_at__lt=today_end,
        )
        if branch_id:
            today_orders = today_orders.filter(branch_id=branch_id)
            today_refunds = today_refunds.filter(branch_id=branch_id)
        today_agg = today_orders.aggregate(
            total_revenue=Sum('total_amount'),
            order_count=Count('id'),
        )
        from base.services.order_refund import refund_totals
        today_refund_agg = refund_totals(today_refunds)
        today_revenue = (
            today_agg['total_revenue'] or Decimal('0')
        ) - today_refund_agg['amount']

        cashier_sales = (
            today_orders
            .values('cashier__id', 'cashier__first_name', 'cashier__last_name')
            .annotate(
                total_revenue=Sum('total_amount'),
                order_count=Count('id'),
            )
        )
        cashier_refunds = (
            today_refunds
            .values('cashier__id', 'cashier__first_name', 'cashier__last_name')
            .annotate(
                total_refunds=Sum('amount'),
                refund_count=Count('id'),
            )
        )
        cashier_perf_by_id = {}
        for row in cashier_sales:
            cashier_id = row['cashier__id']
            cashier_perf_by_id[cashier_id] = {
                'cashier_id': cashier_id,
                'cashier_name': (
                    f"{row['cashier__first_name'] or ''} "
                    f"{row['cashier__last_name'] or ''}"
                ).strip(),
                'total_revenue': row['total_revenue'] or Decimal('0'),
                'order_count': row['order_count'] or 0,
                'refund_count': 0,
            }
        for row in cashier_refunds:
            cashier_id = row['cashier__id']
            current = cashier_perf_by_id.setdefault(cashier_id, {
                'cashier_id': cashier_id,
                'cashier_name': (
                    f"{row['cashier__first_name'] or ''} "
                    f"{row['cashier__last_name'] or ''}"
                ).strip(),
                'total_revenue': Decimal('0'),
                'order_count': 0,
                'refund_count': 0,
            })
            current['total_revenue'] -= row['total_refunds'] or Decimal('0')
            current['refund_count'] += row['refund_count'] or 0
        cashier_perf = sorted(
            cashier_perf_by_id.values(),
            key=lambda row: (
                -row['total_revenue'], row['cashier_name'], row['cashier_id'] or 0,
            ),
        )

        product_items = OrderItem.objects.filter(
            order__is_deleted=False,
            order__is_paid=True,
            order__paid_at__gte=today_start,
            order__paid_at__lt=today_end,
            is_deleted=False,
        )
        if branch_id:
            product_items = product_items.filter(order__branch_id=branch_id)
        product_sales = (
            product_items
            .values('product__id', 'product__name')
            .annotate(
                total_quantity=Sum('quantity'),
                total_revenue=Sum(net_line_revenue()),
            )
        )
        from base.services.refund_lines import (
            REFUND_EVENT_ALIAS, refund_item_events, refund_line_quantity,
            refund_line_revenue,
        )
        refunded_items = refund_item_events(
            refunded_at__gte=today_start,
            refunded_at__lt=today_end,
        )
        if branch_id:
            refunded_items = refunded_items.filter(
                order__branch_id=branch_id,
            )
        product_refunds = (
            refunded_items
            .values('product__id', 'product__name')
            .annotate(
                total_quantity=Sum(
                    refund_line_quantity(REFUND_EVENT_ALIAS)
                ),
                total_revenue=Sum(
                    refund_line_revenue(REFUND_EVENT_ALIAS)
                ),
            )
        )
        product_totals = {}
        for row in product_sales:
            product_totals[row['product__id']] = {
                'product_id': row['product__id'],
                'product_name': row['product__name'],
                'total_quantity': row['total_quantity'] or 0,
                'total_revenue': row['total_revenue'] or Decimal('0'),
            }
        for row in product_refunds:
            current = product_totals.setdefault(row['product__id'], {
                'product_id': row['product__id'],
                'product_name': row['product__name'],
                'total_quantity': 0,
                'total_revenue': Decimal('0'),
            })
            current['total_quantity'] -= row['total_quantity'] or 0
            current['total_revenue'] -= row['total_revenue'] or Decimal('0')
        top_products = sorted(
            product_totals.values(),
            key=lambda row: (
                -row['total_quantity'], row['product_name'] or '',
                row['product_id'] or 0,
            ),
        )[:10]

        return ServiceResponse.success(data={
            'stats': {
                'today': {
                    'total_revenue': str(today_revenue),
                    'order_count': today_agg['order_count'] or 0,
                    'refund_count': today_refunds.count(),
                    'refund_total': str(today_refund_agg['amount']),
                },
                'cashier_performance': [
                    {
                        'cashier_id': cp['cashier_id'],
                        'cashier_name': cp['cashier_name'],
                        'total_revenue': str(cp['total_revenue']),
                        'order_count': cp['order_count'],
                        'refund_count': cp['refund_count'],
                    }
                    for cp in cashier_perf
                ],
                'top_products': [
                    {
                        'product_id': tp['product_id'],
                        'product_name': tp['product_name'],
                        'total_quantity': tp['total_quantity'],
                        'total_revenue': str(tp['total_revenue']),
                    }
                    for tp in top_products
                ],
            }
        })

    @staticmethod
    def get_history(page=1, per_page=20, branch_id=None):
        qs = (
            Inkassa.objects.filter(is_deleted=False)
            .exclude(notes__startswith=Inkassa.refund_command_prefix())
            .select_related('cashier')
            .order_by('-created_at')
        )
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        total = qs.count()
        total_pages = (total + per_page - 1) // per_page
        items = qs[(page - 1) * per_page: page * per_page]

        return ServiceResponse.success(data={
            'inkassas': [_serialize_inkassa(i) for i in items],
            'pagination': {
                'current_page': page,
                'per_page': per_page,
                'total_inkassas': total,
                'total_pages': total_pages,
                'has_next': page * per_page < total,
                'has_previous': page > 1,
            },
        })

    @staticmethod
    def get_detail(inkassa_id):
        try:
            inkassa = Inkassa.objects.select_related('cashier').get(
                pk=inkassa_id,
                is_deleted=False,
            )
            if str(inkassa.notes or '').startswith(
                Inkassa.refund_command_prefix()
            ):
                raise Inkassa.DoesNotExist
        except Inkassa.DoesNotExist:
            return ServiceResponse.not_found('Inkassa not found')
        return ServiceResponse.success(data={'inkassa': _serialize_inkassa(inkassa)})

    @staticmethod
    @transaction.atomic
    def perform(user, amounts, branch_id=None):
        requested_branch = branch_id or amounts.get('branch_id')
        register, error = _resolve_register(requested_branch, for_update=True)
        if error:
            return error

        pending_before = Inkassa.pending_register_amount(register)
        balance_before = (
            register.current_balance or Decimal('0')
        ) - pending_before

        method_amounts = {}
        total_removed = Decimal('0')
        for method in ('CASH', 'UZCARD', 'HUMO', 'PAYME'):
            amount = amounts.get(method.lower(), 0)
            try:
                amount = Decimal(str(amount))
            except Exception:
                amount = Decimal('0')
            if amount < 0:
                return ServiceResponse.validation_error(
                    errors={method.lower(): 'Amount cannot be negative'},
                    message='Invalid amount',
                )
            if amount > 0:
                method_amounts[method] = amount
                total_removed += amount

        if total_removed <= 0:
            return ServiceResponse.validation_error(
                errors={'amount': 'At least one payment method amount must be greater than 0'},
                message='No amounts provided',
            )

        # The register drawer holds ONLY physical cash. Card sales (UZCARD /
        # HUMO / PAYME) settle to the bank and were never added to it, so the
        # register is bounded by — and only reduced by — the CASH portion.
        # (Bug fix: previously the whole cash+card total was checked against
        # and subtracted from the register, depleting cash that was never
        # there and rejecting valid collections.)
        cash_amount = method_amounts.get('CASH', Decimal('0'))
        card_amount = total_removed - cash_amount

        if cash_amount > balance_before:
            return ServiceResponse.validation_error(
                errors={'cash': f'Cash {cash_amount} exceeds register balance {balance_before}'},
                message='Insufficient register balance',
            )

        now = timezone.now()
        from base.services.business_day import day_window, business_date
        today_start, _ = day_window(business_date(now))

        last_inkassa = Inkassa.objects.filter(
            is_deleted=False, branch_id=register.branch_id,
        ).exclude(
            notes__startswith=Inkassa.refund_command_prefix(),
        ).order_by('-created_at').first()
        # Chain the period to the previous inkassa's end. Previously period_end
        # was never set on creation, so this always fell back to today_start and
        # every partial inkassa re-counted the WHOLE day's revenue/orders —
        # double-reporting across multiple same-day collections. We now stamp
        # period_end=now below so the next collection starts where this one ends.
        period_start = last_inkassa.period_end if (last_inkassa and last_inkassa.period_end) else today_start

        period_orders = Order.objects.filter(
            is_deleted=False, is_paid=True, branch_id=register.branch_id,
            paid_at__gte=period_start, paid_at__lte=now,
        )
        today_agg = period_orders.aggregate(
            total_revenue=Sum('total_amount'),
            order_count=Count('id'),
        )
        from base.services.order_refund import refund_totals
        period_refunds = refund_totals(OrderRefund.objects.filter(
            is_deleted=False,
            branch_id=register.branch_id,
            refunded_at__gte=period_start,
            refunded_at__lte=now,
        ))
        period_revenue = (
            today_agg['total_revenue'] or Decimal('0')
        ) - period_refunds['amount']

        created_inkassas = []
        running_balance = balance_before
        for method, amount in method_amounts.items():
            row_before = running_balance
            # Only cash leaves the drawer; card rows don't move the register.
            if method == 'CASH':
                running_balance = running_balance - amount
            # Exactly one row owns the period aggregate. Copying it to every
            # tender row made AI/report sums multiply mixed collections.
            first_in_batch = not created_inkassas
            is_cash_command = method == 'CASH'
            operator_notes = amounts.get('notes', '')
            inkassa = Inkassa.objects.create(
                branch_id=register.branch_id,
                cashier=user,
                amount=amount,
                inkass_type=method,
                balance_before=row_before,
                balance_after=running_balance,
                period_start=period_start,
                period_end=now,
                total_orders=(today_agg['order_count'] or 0) if first_in_batch else 0,
                total_revenue=period_revenue if first_in_batch else 0,
                register_command=is_cash_command,
                notes=(
                    Inkassa.command_notes(operator_notes)
                    if is_cash_command else operator_notes
                ),
            )
            created_inkassas.append(inkassa)

        # CashRegister is branch-owned; the desktop intentionally rejects a
        # pulled cloud balance. The CASH row is therefore a durable removal
        # command. Pending commands reduce cloud availability immediately, and
        # the branch applies/acknowledges them atomically on its next sync.
        from base.services.treasury_service import TreasuryService
        TreasuryService.deposit_inkassa(
            cash_amount=cash_amount, card_amount=card_amount,
            performed_by=user,
            reference_id=created_inkassas[0].id if created_inkassas else None,
        )

        return ServiceResponse.success(
            data={
                # amount_removed = what actually left the register (cash only).
                'amount_removed': str(cash_amount),
                'total_collected': str(total_removed),
                'cash_to_safe': str(cash_amount),
                'card_to_bank': str(card_amount),
                'balance_before': str(balance_before),
                'balance_after': str(running_balance),
                'reported_balance': str(register.current_balance),
                'pending_cash_commands': str(pending_before + cash_amount),
                'branch_id': register.branch_id,
                'inkassas': [_serialize_inkassa(i) for i in created_inkassas],
            },
            message='Inkassa performed successfully',
        )


def _serialize_inkassa(i):
    return {
        'id': i.id,
        'branch_id': i.branch_id,
        'amount': str(i.amount),
        'inkass_type': i.inkass_type,
        'balance_before': str(i.balance_before),
        'balance_after': str(i.balance_after),
        'period_start': i.period_start.isoformat() if i.period_start else None,
        'period_end': i.period_end.isoformat() if i.period_end else None,
        'total_orders': i.total_orders,
        'total_revenue': str(i.total_revenue),
        'notes': Inkassa.visible_notes(i.notes),
        'cashier': {
            'id': i.cashier.id,
            'name': f"{i.cashier.first_name} {i.cashier.last_name}",
        } if i.cashier else None,
        'created_at': i.created_at.isoformat(),
    }
