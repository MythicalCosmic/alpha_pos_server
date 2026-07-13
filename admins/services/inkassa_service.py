import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum, Count
from django.utils import timezone

from base.models import CashRegister, Inkassa, Order, OrderItem
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
        return ServiceResponse.success(data={
            'branch_id': register.branch_id,
            'balance': str(register.current_balance),
            'last_updated': register.last_updated.isoformat(),
        })

    @staticmethod
    def get_stats(branch_id=None):
        # Match every other money surface: "today" is the configured business
        # day (03:00 by default), not calendar midnight.
        from base.services.business_day import today_window
        today_start, today_end = today_window()

        # Exclude cancelled orders. Cancelling a paid order reverses its cash
        # from the register (add_to_register(-total)) but leaves is_paid /
        # paid_at set, so without this exclusion the reported revenue counts
        # money the drawer no longer holds — overstating expected cash.
        today_orders = Order.objects.filter(
            is_deleted=False, is_paid=True,
            created_at__gte=today_start, created_at__lt=today_end,
        ).exclude(status='CANCELED')
        if branch_id:
            today_orders = today_orders.filter(branch_id=branch_id)
        today_agg = today_orders.aggregate(
            total_revenue=Sum('total_amount'),
            order_count=Count('id'),
        )

        cashier_perf = (
            today_orders
            .exclude(status='CANCELED')
            .values('cashier__id', 'cashier__first_name', 'cashier__last_name')
            .annotate(
                total_revenue=Sum('total_amount'),
                order_count=Count('id'),
            )
            .order_by('-total_revenue')
        )

        product_items = OrderItem.objects.filter(
            order__is_deleted=False,
            order__is_paid=True,
            order__created_at__gte=today_start,
            order__created_at__lt=today_end,
            is_deleted=False,
        )
        if branch_id:
            product_items = product_items.filter(order__branch_id=branch_id)
        top_products = (
            product_items
            .exclude(order__status='CANCELED')
            .values('product__id', 'product__name')
            .annotate(
                total_quantity=Sum('quantity'),
                total_revenue=Sum(net_line_revenue()),
            )
            .order_by('-total_quantity')[:10]
        )

        return ServiceResponse.success(data={
            'stats': {
                'today': {
                    'total_revenue': str(today_agg['total_revenue'] or Decimal('0')),
                    'order_count': today_agg['order_count'] or 0,
                },
                'cashier_performance': [
                    {
                        'cashier_id': cp['cashier__id'],
                        'cashier_name': f"{cp['cashier__first_name'] or ''} {cp['cashier__last_name'] or ''}".strip(),
                        'total_revenue': str(cp['total_revenue'] or Decimal('0')),
                        'order_count': cp['order_count'],
                    }
                    for cp in cashier_perf
                ],
                'top_products': [
                    {
                        'product_id': tp['product__id'],
                        'product_name': tp['product__name'],
                        'total_quantity': tp['total_quantity'],
                        'total_revenue': str(tp['total_revenue'] or Decimal('0')),
                    }
                    for tp in top_products
                ],
            }
        })

    @staticmethod
    def get_history(page=1, per_page=20, branch_id=None):
        qs = Inkassa.objects.filter(is_deleted=False).select_related('cashier').order_by('-created_at')
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
                pk=inkassa_id, is_deleted=False
            )
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

        balance_before = register.current_balance

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
        ).exclude(status='CANCELED')
        today_agg = period_orders.aggregate(
            total_revenue=Sum('total_amount'),
            order_count=Count('id'),
        )

        created_inkassas = []
        running_balance = balance_before
        for method, amount in method_amounts.items():
            row_before = running_balance
            # Only cash leaves the drawer; card rows don't move the register.
            if method == 'CASH':
                running_balance = running_balance - amount
            inkassa = Inkassa.objects.create(
                branch_id=register.branch_id,
                cashier=user,
                amount=amount,
                inkass_type=method,
                balance_before=row_before,
                balance_after=running_balance,
                period_start=period_start,
                period_end=now,
                total_orders=today_agg['order_count'] or 0,
                total_revenue=today_agg['total_revenue'] or 0,
                notes=amounts.get('notes', ''),
            )
            created_inkassas.append(inkassa)

        # Remove only the cash from the register, then route the whole
        # collection into the treasury: cash → SAFE, cards → BANK. synced_at /
        # sync_version are reset so the new balance propagates to the cloud.
        register.current_balance -= cash_amount
        register.save(update_fields=['current_balance', 'last_updated',
                                     'synced_at', 'sync_version'])

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
                'balance_after': str(register.current_balance),
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
        'notes': i.notes or '',
        'cashier': {
            'id': i.cashier.id,
            'name': f"{i.cashier.first_name} {i.cashier.last_name}",
        } if i.cashier else None,
        'created_at': i.created_at.isoformat(),
    }
