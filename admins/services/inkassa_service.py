import hashlib
import json
import logging
from datetime import timezone as datetime_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Sum, Count
from django.utils import timezone

from base.models import CashRegister, Inkassa, Order, OrderItem, OrderRefund
from base.helpers.response import ServiceResponse
from base.repositories import CashRegisterRepository
from base.services.revenue import net_line_revenue

logger = logging.getLogger(__name__)

_MONEY_QUANTUM = Decimal('0.01')
_MAX_MONEY = Decimal('9999999999.99')
_INKASSA_METHODS = ('CASH', 'UZCARD', 'HUMO', 'CARD', 'PAYME')


def _collection_money(raw):
    try:
        value = Decimal(str(raw)).quantize(_MONEY_QUANTUM)
        original = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        not value.is_finite()
        or not original.is_finite()
        or value != original
        or value < 0
        or value > _MAX_MONEY
    ):
        return None
    return value


def _payload_hash(branch_id, method_amounts, *, user, payload):
    """Commit a batch key to both money and its approval/audit metadata.

    The database batch key is the authoritative idempotency boundary.  Hashing
    only tender amounts allowed a retry to silently change the operator note,
    legacy approval, or approving actor while receiving the first response.
    """
    canonical = {
        'branch_id': branch_id,
        'tenders': {
            method: str(amount.quantize(_MONEY_QUANTUM))
            for method, amount in sorted(method_amounts.items())
        },
        'actor_id': str(getattr(user, 'pk', '') or ''),
        'notes': str(payload.get('notes') or '').strip(),
        'approve_legacy_opening': payload.get('approve_legacy_opening') is True,
        'legacy_opening_note': str(
            payload.get('legacy_opening_note') or ''
        ).strip(),
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _inkassa_batch_response(register, rows, *, replay=False):
    from base.models import TreasuryTransaction

    rows = list(rows)
    total = sum((row.amount for row in rows), Decimal('0.00'))
    cash = sum(
        (row.amount for row in rows if row.inkass_type == 'CASH'),
        Decimal('0.00'),
    )
    safe_delta = sum(
        (row.legacy_treasury_amount for row in rows), Decimal('0.00'),
    )
    cash_safe = sum(
        (row.legacy_treasury_amount for row in rows if row.inkass_type == 'CASH'),
        Decimal('0.00'),
    )
    entries = list(TreasuryTransaction.objects.filter(
        type=TreasuryTransaction.Type.INKASSA,
        reference_type='InkassaLegacy',
        reference_id__in=[row.id for row in rows],
    ).order_by('id'))
    allocations = [{
        'method': row.inkass_type,
        'collected': str(row.amount),
        'matched_recognized': str(row.settlement_offset_amount),
        'legacy_opening': str(row.legacy_treasury_amount),
        'safe_delta': str(row.legacy_treasury_amount),
        'entry_id': next(
            (entry.id for entry in entries if entry.reference_id == row.id), None,
        ),
    } for row in rows]
    reason = (
        'LEGACY_OPENING_POSTED'
        if safe_delta else 'SHIFT_SETTLEMENT_ALREADY_RECOGNIZED'
    )
    first = rows[0]
    last = rows[-1]
    return ServiceResponse.success(
        data={
            'batch_id': first.collection_batch_key,
            'replayed': replay,
            'amount_removed': str(cash),
            'total_collected': str(total),
            'cash_to_safe': str(cash_safe),
            'card_to_bank': '0.00',
            'treasury_posting': {
                'status': 'posted' if entries else 'not_posted',
                'account': 'SAFE',
                'total': str(safe_delta),
                'tenders': allocations,
                'entry_ids': [entry.id for entry in entries],
                'reason': reason,
            },
            'balance_before': str(first.balance_before),
            'balance_after': str(last.balance_after),
            'reported_balance': str(register.current_balance),
            'pending_cash_commands': str(Inkassa.pending_register_amount(register)),
            'branch_id': register.branch_id,
            'inkassas': [_serialize_inkassa(row) for row in rows],
        },
        message=(
            'Inkassa batch replayed safely'
            if replay else 'Inkassa performed successfully'
        ),
    )


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

    if for_update:
        from base.services.accounting_cursor import lock_branch_accounting
        register = lock_branch_accounting(requested)
    else:
        register = CashRegisterRepository.get_or_create_current(requested)
    return register, None


def _actor_can_manage_branch(actor, branch_id):
    role = str(getattr(actor, 'role', '') or '').upper()
    actor_branch = str(getattr(actor, 'branch_id', '') or '').strip()
    if _is_global_admin(actor):
        return True
    return role in ('ADMIN', 'MANAGER') and actor_branch == str(branch_id or '')


def _is_global_admin(actor):
    role = str(getattr(actor, 'role', '') or '').upper()
    actor_branch = str(getattr(actor, 'branch_id', '') or '').strip().lower()
    return role == 'ADMIN' and actor_branch in ('', 'cloud')


def _authorized_branch(actor, branch_id=None, *, manager_only=False):
    """Resolve the only branch an actor may read/manage.

    ``actor=None`` is retained for trusted internal callers and test helpers;
    HTTP views always supply the authenticated actor.
    """
    requested = str(branch_id or '').strip()
    if actor is None or _is_global_admin(actor):
        return requested or None, None

    role = str(getattr(actor, 'role', '') or '').upper()
    actor_branch = str(getattr(actor, 'branch_id', '') or '').strip()
    allowed = ('ADMIN', 'MANAGER') if manager_only else (
        'ADMIN', 'MANAGER', 'CASHIER', 'WAITER',
    )
    if role not in allowed or not actor_branch:
        return None, ServiceResponse.forbidden(
            'You do not have access to a branch register'
        )
    if requested and requested != actor_branch:
        return None, ServiceResponse.forbidden(
            'You cannot access another branch register'
        )
    return actor_branch, None


class AdminInkassaService:

    @staticmethod
    def get_balance(branch_id=None, actor=None):
        branch_id, auth_error = _authorized_branch(actor, branch_id)
        if auth_error:
            return auth_error
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
    def get_stats(branch_id=None, actor=None):
        branch_id, auth_error = _authorized_branch(actor, branch_id)
        if auth_error:
            return auth_error
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
    def get_history(page=1, per_page=20, branch_id=None, actor=None):
        branch_id, auth_error = _authorized_branch(
            actor, branch_id, manager_only=True,
        )
        if auth_error:
            return auth_error
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
    def get_detail(inkassa_id, actor=None):
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
        _branch, auth_error = _authorized_branch(
            actor, inkassa.branch_id, manager_only=True,
        )
        if auth_error:
            return auth_error
        return ServiceResponse.success(data={'inkassa': _serialize_inkassa(inkassa)})

    @staticmethod
    @transaction.atomic
    def perform(user, amounts, branch_id=None, batch_key=None):
        if not isinstance(amounts, dict):
            return ServiceResponse.validation_error(
                errors={'body': 'Must be a JSON object'},
                message='Invalid Inkassa request',
            )
        requested_branch, auth_error = _authorized_branch(
            user,
            branch_id or amounts.get('branch_id'),
            manager_only=True,
        )
        if auth_error:
            return auth_error
        register, error = _resolve_register(requested_branch, for_update=True)
        if error:
            return error
        if not _actor_can_manage_branch(user, register.branch_id):
            return ServiceResponse.forbidden('You cannot manage another branch register')

        batch_key = str(
            batch_key or amounts.get('batch_id') or amounts.get('idempotency_key') or ''
        ).strip()
        if not batch_key or len(batch_key) > 128:
            return ServiceResponse.validation_error(
                errors={
                    'batch_id': 'A stable batch id (1..128 characters) is required',
                },
                message='Inkassa idempotency key is required',
            )

        method_amounts = {}
        amount_errors = {}
        for method in _INKASSA_METHODS:
            raw = amounts.get(method.lower(), amounts.get(method, 0))
            parsed = _collection_money(raw)
            if parsed is None:
                amount_errors[method.lower()] = (
                    'Must be a finite non-negative amount with at most 2 decimals'
                )
            elif parsed > 0:
                method_amounts[method] = parsed
        if amount_errors:
            return ServiceResponse.validation_error(
                errors=amount_errors,
                message='Invalid Inkassa amount',
            )
        if not method_amounts:
            return ServiceResponse.validation_error(
                errors={
                    'amount': 'At least one payment method amount must be greater than 0',
                },
                message='No amounts provided',
            )

        payload_hash = _payload_hash(
            register.branch_id,
            method_amounts,
            user=user,
            payload=amounts,
        )
        existing_batch = list(Inkassa.objects.filter(
            branch_id=register.branch_id,
            collection_batch_key=batch_key,
        ).order_by('id'))
        if existing_batch:
            if any(row.is_deleted for row in existing_batch):
                return ServiceResponse.validation_error(
                    errors={'batch_id': 'This batch id belongs to tombstoned evidence'},
                    message='Inkassa batch id cannot be reused',
                )
            frozen = {row.inkass_type: row.amount for row in existing_batch}
            if (
                frozen != method_amounts
                or any(row.collection_payload_hash != payload_hash
                       for row in existing_batch)
            ):
                return ServiceResponse.validation_error(
                    errors={'batch_id': 'This batch id was already used for another payload'},
                    message='Conflicting Inkassa batch replay',
                )
            return _inkassa_batch_response(register, existing_batch, replay=True)

        pending_before = Inkassa.pending_register_amount(register)
        balance_before = (
            register.current_balance or Decimal('0')
        ) - pending_before
        # The register drawer holds ONLY physical cash. Electronic tenders do
        # not enter the drawer (manager reconciliation recognizes every tender
        # in SAFE), so the register is bounded by — and only reduced by — the
        # CASH portion.
        # (Bug fix: previously the whole cash+card total was checked against
        # and subtracted from the register, depleting cash that was never
        # there and rejecting valid collections.)
        cash_amount = method_amounts.get('CASH', Decimal('0'))

        if cash_amount > balance_before:
            return ServiceResponse.validation_error(
                errors={'cash': f'Cash {cash_amount} exceeds register balance {balance_before}'},
                message='Insufficient register balance',
            )

        # Enforce the lifecycle order. An eligible shift whose reconciliation
        # has not posted yet owns receipts absent from the recognized pool;
        # treating them as legacy now and reconciling later would double-credit.
        from base.models import Shift
        unposted = list(Shift.objects.filter(
            is_deleted=False,
            branch_id=register.branch_id,
            treasury_settlement_eligible=True,
            status__in=('ACTIVE', 'ENDED', 'COMPLETED'),
            reconciliation__treasury_posted_at__isnull=True,
        ).values_list('id', flat=True)[:20])
        if unposted:
            return ServiceResponse.validation_error(
                errors={
                    'shifts': (
                        'Close and reconcile eligible shifts before Inkassa: '
                        + ', '.join(str(value) for value in unposted)
                    ),
                },
                message='Shift reconciliation is required before Inkassa',
            )

        from base.services.treasury_service import TreasuryService
        allocation = TreasuryService.plan_inkassa_allocation(
            register.branch_id, method_amounts,
        )
        unallocated = {
            method: plan['unallocated']
            for method, plan in allocation.items()
            if plan['unallocated'] > 0
        }
        if unallocated:
            return ServiceResponse.validation_error(
                errors={
                    f'{method.lower()}': (
                        f'{amount} has no reconciled or legacy shift evidence'
                    )
                    for method, amount in unallocated.items()
                },
                message='Inkassa exceeds auditable tender evidence',
            )

        legacy_total = sum(
            (plan['legacy_opening'] for plan in allocation.values()),
            Decimal('0.00'),
        )
        if legacy_total > 0:
            if not _is_global_admin(user):
                return ServiceResponse.forbidden(
                    'Only a global administrator can approve a legacy cash opening'
                )
            if amounts.get('approve_legacy_opening') is not True:
                return ServiceResponse.validation_error(
                    errors={
                        'approve_legacy_opening': (
                            'Explicit manager approval is required for legacy evidence'
                        ),
                    },
                    message='Legacy opening approval required',
                )
            approval_note = str(amounts.get('legacy_opening_note') or '').strip()
            if not approval_note:
                return ServiceResponse.validation_error(
                    errors={'legacy_opening_note': 'Approval note is required'},
                    message='Legacy opening approval note required',
                )
            # A fresh sync presence is the cutover-readiness proof. During an
            # outage the register can arrive before its order/tender evidence;
            # never approve an opening against that incomplete snapshot.
            from base.services.presence import live_devices
            live = any(
                str(device.get('branch_id') or '') == register.branch_id
                for device in live_devices()
            )
            if not live:
                return ServiceResponse.validation_error(
                    errors={
                        'sync': 'A live branch sync heartbeat is required',
                    },
                    message='Branch sync is not fresh enough for legacy opening',
                )

        now = timezone.now()
        from base.services.business_day import day_window, business_date
        today_start, _ = day_window(business_date(now))

        last_inkassa = Inkassa.objects.filter(
            is_deleted=False, branch_id=register.branch_id,
        ).exclude(
            notes__startswith=Inkassa.refund_command_prefix(),
        ).order_by('-period_end', '-pk').first()
        # Chain the period to the previous inkassa's end. Previously period_end
        # was never set on creation, so this always fell back to today_start and
        # every partial inkassa re-counted the WHOLE day's revenue/orders —
        # double-reporting across multiple same-day collections. We now stamp
        # period_end=now below so the next collection starts where this one ends.
        # Accounting queries below are uniformly half-open [start, end). Their
        # cursor is local receipt time, so late offline events roll forward.
        period_start = last_inkassa.period_end if (last_inkassa and last_inkassa.period_end) else today_start

        period_orders = Order.objects.filter(
            is_deleted=False, is_paid=True, branch_id=register.branch_id,
            accounting_recorded_at__gte=period_start,
            accounting_recorded_at__lt=now,
        )
        today_agg = period_orders.aggregate(
            total_revenue=Sum('total_amount'),
            order_count=Count('id'),
        )
        from base.services.order_refund import refund_totals
        period_refunds = refund_totals(OrderRefund.objects.filter(
            is_deleted=False,
            branch_id=register.branch_id,
            accounting_recorded_at__gte=period_start,
            accounting_recorded_at__lt=now,
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
            if allocation[method]['legacy_opening'] > 0:
                approval_note = str(amounts.get('legacy_opening_note') or '').strip()
                operator_notes = (
                    f'{operator_notes}\nLegacy opening approved: {approval_note}'
                ).strip()
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
                settlement_offset_amount=allocation[method]['matched_recognized'],
                legacy_treasury_amount=allocation[method]['legacy_opening'],
                treasury_allocated_at=now,
                collection_batch_key=batch_key,
                collection_payload_hash=payload_hash,
                notes=(
                    Inkassa.command_notes(operator_notes)
                    if is_cash_command else operator_notes
                ),
            )
            created_inkassas.append(inkassa)

            TreasuryService.post_legacy_inkassa(
                inkassa.id,
                allocation[method]['legacy_opening'],
                method,
                branch_id=register.branch_id,
                performed_by=user,
            )

        # CashRegister is branch-owned; the desktop intentionally rejects a
        # pulled cloud balance. The CASH row is therefore a durable physical
        # removal command. Pending commands reduce cloud availability
        # immediately, and the branch applies/acknowledges them atomically on
        # its next sync.
        #
        # IMPORTANT: Inkassa is no longer a treasury recognition event. The
        # manager's shift reconciliation already posts every confirmed tender
        # to SAFE, one shift+tender at a time. Crediting SAFE/BANK here again
        # would double-book the same proceeds. Non-cash rows remain an audit
        # trail only; cash additionally drives the physical register command.

        return _inkassa_batch_response(register, created_inkassas)


def _serialize_inkassa(i):
    def money(value):
        return format(Decimal(value or 0).quantize(_MONEY_QUANTUM), 'f')

    def iso(value):
        if value is None:
            return None
        return value.astimezone(datetime_timezone.utc).isoformat()

    return {
        'id': i.id,
        'branch_id': i.branch_id,
        'amount': money(i.amount),
        'inkass_type': i.inkass_type,
        'balance_before': money(i.balance_before),
        'balance_after': money(i.balance_after),
        'period_start': iso(i.period_start),
        'period_end': iso(i.period_end),
        'total_orders': i.total_orders,
        'total_revenue': money(i.total_revenue),
        'batch_id': i.collection_batch_key or None,
        'treasury_allocation': {
            'matched_recognized': money(i.settlement_offset_amount),
            'legacy_opening': money(i.legacy_treasury_amount),
            'safe_delta': money(i.legacy_treasury_amount),
            'allocated_at': iso(i.treasury_allocated_at),
        },
        'notes': Inkassa.visible_notes(i.notes),
        'cashier': {
            'id': i.cashier.id,
            'name': f"{i.cashier.first_name} {i.cashier.last_name}",
        } if i.cashier else None,
        'created_at': iso(i.created_at),
    }
