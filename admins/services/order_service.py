import logging
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from datetime import timedelta, datetime
from uuid import uuid4
from base.repositories import OrderRepository, OrderItemRepository, ProductRepository, UserRepository
from base.services.inkassa_service import InkassaService
from base.helpers.response import ServiceResponse

logger = logging.getLogger(__name__)


ALLOWED_STATUSES = ['PREPARING', 'READY', 'CANCELED', 'COMPLETED']


def _schedule_loyalty_accrual(order_id):
    """Evaluate loyalty only after the sale/status transaction commits."""
    def accrue():
        try:
            from base.models import Order
            from notifications.services import loyalty_service
            order = Order.objects.filter(pk=order_id, is_deleted=False).first()
            if order:
                loyalty_service.maybe_accrue(order)
        except Exception:
            logger.exception('loyalty accrual failed for order %s', order_id)

    transaction.on_commit(accrue, robust=True)


def _schedule_fiscalization(order_id):
    """Fiscal provider calls must never escape an uncommitted sale."""
    def fiscalize():
        try:
            from fiscalization.services import FiscalizationService
            FiscalizationService.fiscalize_on_payment(order_id)
        except Exception:
            logger.exception(
                'non-critical fiscalization error in pay flow (order=%s)',
                order_id,
            )

    transaction.on_commit(fiscalize, robust=True)

ALLOWED_ORDER_FIELDS = {
    'created_at', '-created_at', 'updated_at', '-updated_at',
    'total_amount', '-total_amount', 'display_id', '-display_id',
    'status', '-status', 'id', '-id', 'paid_at', '-paid_at',
}


def _format_duration(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _live_items(order):
    """Return live lines while reusing OrderRepository's prefetch cache."""
    return [item for item in order.items.all() if not item.is_deleted]


def _payments_payload(order):
    """Canonical tender split for ONE order: {cash, card, payme} (+ `unknown` only
    when the tender genuinely cannot be determined), plus per-acquirer `card_detail`.

    This is what replaces the opaque `MIXED` marker: a half-cash sale reports
    {"cash": "35000.00", "card": "18000.00"} instead of just "MIXED". `cash` is the
    BILL's cash portion — OrderPayment stores the tendered cash, which may include
    the customer's change. Uses the prefetched `payments` relation (no extra query).
    """
    from base.services.tender import split_from_rows
    lines = [p for p in order.payments.all() if not p.is_deleted]
    split, detail = split_from_rows(
        order.total_amount, order.payment_method,
        [(p.method, p.amount) for p in lines], order_id=order.id,
    )
    data = {
        'cash': str(split['cash']),
        'card': str(split['card']),
        'payme': str(split['payme']),
    }
    if split['unknown']:
        data['unknown'] = str(split['unknown'])
    if any(detail.values()):
        # Storage keeps the acquirer so a bank statement still reconciles.
        data['card_detail'] = {k: str(v) for k, v in detail.items() if v}
    return data


def _serialize_order_list(order, include_items=True):
    live_items = _live_items(order)
    data = {
        'id': order.id,
        'display_id': order.display_id,
        'order_number': order.order_number,
        'order_type': order.order_type,
        'order_origin': order.order_origin,
        'phone_number': order.phone_number,
        'delivery_address': getattr(order, 'delivery_address', '') or '',
        'description': order.description,
        'user': {
            'id': order.user.id,
            'name': f"{order.user.first_name} {order.user.last_name}",
        } if order.user else None,
        'cashier': {
            'id': order.cashier.id,
            'name': f"{order.cashier.first_name} {order.cashier.last_name}",
        } if order.cashier else None,
        'delivery_person': {
            'id': order.delivery_person.id,
            'name': f"{order.delivery_person.first_name} {order.delivery_person.last_name or ''}".strip(),
        } if order.delivery_person else None,
        'customer': {
            'id': order.customer.id,
            'name': order.customer.name,
            'phone': order.customer.phone_number,
            'is_staff': order.customer.is_staff,
        } if order.customer_id else None,
        'status': order.status,
        'is_paid': order.is_paid,
        'total_amount': str(order.total_amount or 0),
        # Canonical tender split ({cash, card, payme}) instead of an opaque MIXED.
        'payments': _payments_payload(order),
        # The list queryset is prefetched with `items__product__category`
        # (OrderRepository.get_with_relations) — iterate the cached items
        # instead of `.count()` (extra query) and `.values()` (fresh query
        # that bypasses the prefetch).
        'items_count': len(live_items),
        'paid_at': order.paid_at.isoformat() if order.paid_at else None,
        'ready_at': order.ready_at.isoformat() if order.ready_at else None,
        'created_at': order.created_at.isoformat(),
        'updated_at': order.updated_at.isoformat(),
    }
    # Inline line items (item 5). Skippable with ?include_items=false (item 14) to
    # lighten the list payload for views that only need headers (items_count stays).
    if include_items:
        data['items'] = [
            {
                'id': i.id,
                'product__id': i.product_id,
                'product__name': i.product.name if i.product else None,
                'product__category__id': i.product.category_id if i.product else None,
                'product__category__name': (
                    i.product.category.name if i.product and i.product.category else None
                ),
                'quantity': i.quantity,
                'detail': i.detail,
                'price': i.price,
                'ready_at': i.ready_at,
            }
            for i in live_items
        ]
    return data


def _serialize_order_detail(order):
    items = []
    for item in _live_items(order):
        prep_time = (item.ready_at - order.created_at).total_seconds() if item.ready_at else None
        items.append({
            'id': item.id,
            'product': {
                'id': item.product.id,
                'name': item.product.name,
                'category': item.product.category.name if item.product.category else None,
            },
            'quantity': item.quantity,
            'price': str(item.price),
            'subtotal': str(item.price * item.quantity),
            'detail': item.detail,
            'ready_at': item.ready_at.isoformat() if item.ready_at else None,
            'is_ready': item.ready_at is not None,
            'preparation_time_seconds': prep_time,
            'preparation_time_formatted': _format_duration(prep_time) if prep_time else None,
        })

    order_prep_time = (order.ready_at - order.created_at).total_seconds() if order.ready_at else None

    return {
        'id': order.id,
        'display_id': order.display_id,
        'order_number': order.order_number,
        'order_type': order.order_type,
        'order_origin': order.order_origin,
        'phone_number': order.phone_number,
        'delivery_address': getattr(order, 'delivery_address', '') or '',
        'description': order.description,
        'user': {
            'id': order.user.id,
            'name': f"{order.user.first_name} {order.user.last_name}",
            'email': order.user.email,
        } if order.user else None,
        'cashier': {
            'id': order.cashier.id,
            'name': f"{order.cashier.first_name} {order.cashier.last_name}",
        } if order.cashier else None,
        'delivery_person': {
            'id': order.delivery_person.id,
            'name': f"{order.delivery_person.first_name} {order.delivery_person.last_name or ''}".strip(),
            'phone': order.delivery_person.phone_number,
        } if order.delivery_person else None,
        'customer': {
            'id': order.customer.id,
            'name': order.customer.name,
            'phone': order.customer.phone_number,
            'is_staff': order.customer.is_staff,
        } if order.customer_id else None,
        'status': order.status,
        'is_paid': order.is_paid,
        'paid_at': order.paid_at.isoformat() if order.paid_at else None,
        'total_amount': str(order.total_amount),
        # Canonical tender split ({cash, card, payme}) instead of an opaque MIXED.
        'payments': _payments_payload(order),
        'items': items,
        'items_ready_count': sum(1 for i in items if i['is_ready']),
        'items_total_count': len(items),
        'created_at': order.created_at.isoformat(),
        'updated_at': order.updated_at.isoformat(),
        'ready_at': order.ready_at.isoformat() if order.ready_at else None,
        'preparation_time_seconds': order_prep_time,
        'preparation_time_formatted': _format_duration(order_prep_time) if order_prep_time else None,
    }


def _parse_statuses(statuses_param):
    if not statuses_param:
        return None
    param = statuses_param.strip().strip('[]')
    if not param:
        return None
    return [s.strip().strip('"\'') for s in param.split(',') if s.strip()]


def _parse_int_list(param):
    if not param:
        return None
    param = param.strip().strip('[]')
    if not param:
        return None
    result = []
    for item in param.split(','):
        item = item.strip().strip('"\'')
        if item.isdigit():
            result.append(int(item))
    return result or None


def _parse_string_list(param):
    if not param:
        return None
    value = str(param).strip().strip('[]')
    if not value:
        return None
    result = [
        item.strip().strip('"\'').upper()
        for item in value.split(',')
        if item.strip().strip('"\'')
    ]
    return result or None


def _build_filtered_order_queryset(
        *, statuses=None, payment_status=None, category_ids=None,
        product_ids=None, user_id=None, cashier_id=None, order_type=None,
        date_from=None, date_to=None, payment_method=None, search=None,
        order_by='-created_at', include_deleted=False, tod_from=None,
        tod_to=None, datetime_from=None, datetime_to=None,
        from_at=None, to_at=None):
    """Build the canonical Orders-page population.

    Both the paginated list and its global KPI endpoint call this function.
    Pagination and ordering are deliberately applied *after* this membership
    query, so `/orders/stats` can never become page-scoped or silently omit a
    list filter.
    """
    from django.db.models import Q
    from base.models import Order
    from base.services.business_day import resolve_reporting_window

    statuses_list = _parse_statuses(statuses)
    category_ids_list = _parse_int_list(category_ids)
    product_ids_list = _parse_int_list(product_ids)
    has_window = any(value not in (None, '') for value in (
        date_from, date_to, tod_from, tod_to,
        datetime_from, datetime_to, from_at, to_at,
    ))
    window = resolve_reporting_window(
        date_from, date_to,
        tod_from=tod_from, tod_to=tod_to,
        datetime_from=datetime_from, datetime_to=datetime_to,
        from_at=from_at, to_at=to_at,
    ) if has_window else None

    qs = OrderRepository.build_filtered_queryset(
        statuses=statuses_list,
        payment_status=payment_status,
        category_ids=category_ids_list,
        product_ids=product_ids_list,
        user_id=user_id,
        cashier_id=cashier_id,
        order_type=order_type,
        date_from=None,
        date_to=None,
        order_by=(order_by if order_by in ALLOWED_ORDER_FIELDS else '-created_at'),
        include_deleted=include_deleted,
        tod_from=None,
        tod_to=None,
    )
    if window is not None:
        qs = window.filter(qs, 'created_at')

    requested_methods = _parse_string_list(payment_method)
    if requested_methods:
        valid = set(Order.PaymentMethod.values)
        methods = set()
        for method in requested_methods:
            if method == 'CARD':
                methods.update({'CARD', 'UZCARD', 'HUMO'})
            elif method in valid:
                methods.add(method)
        if methods:
            tender_q = Q(payment_method__in=methods) | Q(
                payments__is_deleted=False,
                payments__method__in=methods,
            )
            # Legacy paid rows with no stored method are canonically cash.
            if 'CASH' in methods:
                tender_q |= Q(is_paid=True, payment_method__isnull=True)
            qs = qs.filter(tender_q).distinct()

    search_term = (search or '').strip()[:200]
    if search_term:
        search_q = (
            Q(phone_number__icontains=search_term)
            | Q(description__icontains=search_term)
            | Q(user__first_name__icontains=search_term)
            | Q(user__last_name__icontains=search_term)
            | Q(user__email__icontains=search_term)
            | Q(cashier__first_name__icontains=search_term)
            | Q(cashier__last_name__icontains=search_term)
            | Q(cashier__email__icontains=search_term)
            | Q(customer__name__icontains=search_term)
            | Q(customer__phone_number__icontains=search_term)
            | Q(items__is_deleted=False, items__product__name__icontains=search_term)
        )
        if search_term.isdigit():
            number = int(search_term)
            search_q |= (
                Q(pk=number)
                | Q(display_id=number)
                | Q(order_number=number)
            )
        qs = qs.filter(search_q).distinct()

    return qs, {
        'statuses': statuses_list,
        'category_ids': category_ids_list,
        'product_ids': product_ids_list,
        'payment_status': payment_status,
        'payment_method': requested_methods,
        'user_id': user_id,
        'cashier_id': cashier_id,
        'order_type': order_type,
        'date_from': date_from,
        'date_to': date_to,
        'tod_from': tod_from,
        'tod_to': tod_to,
        'datetime_from': datetime_from or from_at,
        'datetime_to': datetime_to or to_at,
        'start_at': window.start_at.isoformat() if window else None,
        'end_at': window.end_at.isoformat() if window else None,
        'search': search_term or None,
        'include_deleted': bool(include_deleted),
    }


def _business_start():
    from base.services.business_day import business_day_start
    return business_day_start()


def _parse_date(date_str):
    """Parse a start-of-range bound. A bare YYYY-MM-DD anchors to the BUSINESS-day
    start (AppSettings.business_day_start, default 03:00) so reports bound on the
    operating day, not the calendar day. An explicit timestamp is honored as-is."""
    if not date_str:
        return None
    s = date_str.strip()
    try:
        d = datetime.strptime(s, '%Y-%m-%d').date()
        return timezone.make_aware(datetime.combine(d, _business_start()))
    except (ValueError, TypeError):
        try:
            return timezone.make_aware(datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))
        except (ValueError, TypeError):
            return None


def _parse_date_to(date_str):
    """Parse an inclusive end-of-range bound.

    A bare date rolls to the last microsecond before the NEXT business-day cutover,
    so the whole operating day is included — an order at 01:00 still counts toward
    the previous business day (the stats filters use created_at__lte=date_to). An
    explicit timestamp is honored as-is.
    """
    if not date_str:
        return None
    s = date_str.strip()
    try:
        d = datetime.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        # Not a bare date — an explicit timestamp (or junk); honor via _parse_date.
        return _parse_date(date_str)
    from base.services.business_day import business_day_end
    nxt = timezone.make_aware(
        datetime.combine(d + timedelta(days=1), business_day_end()),
    )
    return nxt - timedelta(microseconds=1)


def _recalculate_total(order):
    from discounts.repositories import OrderDiscountRepository
    from discounts.services.discount_service import DiscountService

    order.subtotal = OrderItemRepository.calculate_order_total(order)
    # Recompute each applied discount against the *current* items rather than
    # trusting the frozen OrderDiscount.discount_amount. A percentage / BUY_X /
    # FREE_ITEM rule frozen at apply-time goes stale the moment items change:
    # if the order grew the customer is over-charged, if it shrank the drawer is
    # under-credited (mark_as_paid would settle the wrong cash, or drive
    # total_amount negative and *remove* real cash via add_to_register). The
    # OrderDiscount rows are the source of truth — refresh them, then sum.
    order_items = list(order.items.filter(is_deleted=False).select_related(
        'product__category',
    ))
    applied = Decimal('0')
    for od in OrderDiscountRepository.get_for_order(order.id).select_related(
        'discount__discount_type'
    ).order_by('created_at', 'pk'):
        new_amount = DiscountService.calculate_discount(
            od.discount, order_items, already_applied_discount=applied,
        )
        if new_amount != od.discount_amount:
            od.discount_amount = new_amount
            od.save(update_fields=['discount_amount'])
        applied += new_amount
    order.discount_amount = min(applied, order.subtotal)
    order.total_amount = max(Decimal('0'), order.subtotal - order.discount_amount)
    order.save(update_fields=['subtotal', 'discount_amount', 'total_amount'])


def _adjust_order_stock(order, product_id, quantity_delta):
    # Keep ingredient stock in sync when an already-deducted order's lines
    # change. adjust_for_item_change self-gates to a no-op unless the order had
    # prior deductions, so this is safe regardless of stock config.
    if quantity_delta == 0:
        return None
    try:
        from stock.services import OrderStockService, StockSettingsService
        location_id = StockSettingsService.get_default_location_id()
        result, status = OrderStockService.adjust_for_item_change(
            order.id, product_id, quantity_delta, location_id,
            order.cashier_id or order.user_id,
        )
        if status >= 400:
            logger.error(
                'stock adjustment rejected for order=%s product=%s: %s',
                order.id, product_id, result,
            )
            return result, status
        return None
    except Exception:
        logger.exception('stock adjustment failed in admin order edit flow')
        return ServiceResponse.error(
            'Stock adjustment failed; the order change was not applied. Please retry.'
        )


def _apply_order_stock_transition(order_id, old_status, new_status,
                                  stock_items, performed_by_id):
    try:
        from stock.services import OrderStatusHandler, StockSettingsService
        stock_settings = StockSettingsService.load()
        if (not stock_settings.stock_enabled
                or not getattr(stock_settings, 'auto_deduct_on_sale', True)):
            return None
        location_id = StockSettingsService.get_default_location_id()
        needs_location = (
            (stock_settings.reserve_on_order_create and old_status is None)
            or new_status == stock_settings.deduct_on_order_status
        )
        if needs_location and not location_id:
            return ServiceResponse.error(
                'Stock is enabled but no default stock location is configured.'
            )
        result, status = OrderStatusHandler.on_status_change(
            order_id, old_status, new_status, stock_items,
            location_id, performed_by_id,
        )
        if status >= 400:
            logger.error(
                'stock transition rejected for order=%s %s->%s: %s',
                order_id, old_status, new_status, result,
            )
            return result, status
        return None
    except Exception:
        logger.exception(
            'stock transition failed for order=%s %s->%s',
            order_id, old_status, new_status,
        )
        return ServiceResponse.error(
            'Stock processing failed; the order change was not applied. Please retry.'
        )


def _check_and_update_ready(order):
    total = order.items.filter(is_deleted=False).count()
    ready = order.items.filter(is_deleted=False, ready_at__isnull=False).count()
    all_ready = total > 0 and total == ready

    if all_ready and order.status != 'READY':
        order.status = 'READY'
        order.ready_at = timezone.now()
        order.save(update_fields=['status', 'ready_at'])
        return True, True

    return all_ready, False


class AdminOrderService:

    @staticmethod
    def get_all_orders(page=1, per_page=20, statuses=None, payment_status=None,
                       category_ids=None, user_id=None, cashier_id=None,
                       order_type=None, date_from=None, date_to=None,
                       order_by='-created_at', include_deleted=False,
                       include_items=True, product_ids=None,
                       tod_from=None, tod_to=None, payment_method=None,
                       search=None, datetime_from=None, datetime_to=None,
                       from_at=None, to_at=None):
        qs, applied_filters = _build_filtered_order_queryset(
            statuses=statuses,
            payment_status=payment_status,
            category_ids=category_ids,
            product_ids=product_ids,
            user_id=user_id,
            cashier_id=cashier_id,
            order_type=order_type,
            date_from=date_from,
            date_to=date_to,
            payment_method=payment_method,
            search=search,
            order_by=order_by,
            include_deleted=include_deleted,
            tod_from=tod_from,
            tod_to=tod_to,
            datetime_from=datetime_from,
            datetime_to=datetime_to,
            from_at=from_at,
            to_at=to_at,
        )

        page_obj, paginator = OrderRepository.paginate(qs, page, per_page)
        orders = [_serialize_order_list(o, include_items=include_items)
                  for o in page_obj.object_list]

        return ServiceResponse.success(data={
            'orders': orders,
            'filters': applied_filters,
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_orders': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })

    @staticmethod
    def get_order_by_id(order_id, include_deleted=False):
        if include_deleted:
            from base.models import Order
            try:
                order = Order.objects.select_related(
                    'user', 'cashier', 'delivery_person'
                ).prefetch_related('items__product__category').get(pk=order_id)
            except Order.DoesNotExist:
                return ServiceResponse.not_found('Order not found')
        else:
            order = OrderRepository.get_by_id_with_relations(order_id)
            if not order:
                return ServiceResponse.not_found('Order not found')

        return ServiceResponse.success(data={'order': _serialize_order_detail(order)})

    @staticmethod
    @transaction.atomic
    def create_order(user_id, items, order_type='HALL', phone_number=None,
                     description=None, cashier_id=None, delivery_person_id=None):
        if not UserRepository.exists(id=user_id):
            return ServiceResponse.not_found('User not found')

        if cashier_id and not UserRepository.exists(id=cashier_id, role='CASHIER'):
            return ServiceResponse.error('Invalid cashier')

        from base.services.order_refund import (
            SettlementInvariantError, lock_active_cashier_shift,
        )
        try:
            owner_shift = lock_active_cashier_shift(cashier_id)
        except SettlementInvariantError as exc:
            return ServiceResponse.error(str(exc))
        target_branch = str(owner_shift.branch_id or '').strip()

        if not items:
            return ServiceResponse.validation_error(
                errors={'items': 'At least one item is required'},
                message='Order must have at least one item',
            )

        if order_type not in ['HALL', 'DELIVERY', 'PICKUP']:
            return ServiceResponse.validation_error(
                errors={'order_type': 'Must be HALL, DELIVERY, or PICKUP'},
                message='Invalid order type',
            )

        delivery_person = None
        if delivery_person_id:
            # DeliveryPerson is branch-scoped sync data. A branch-owned Order
            # must never reference a cloud/foreign courier UUID because that FK
            # parent is absent from the terminal's change feed and the Order is
            # then deferred forever. Lock it with the shift ownership check so
            # it cannot be re-owned while this order is being created.
            from base.models import DeliveryPerson
            delivery_person = (
                DeliveryPerson.objects.select_for_update()
                .filter(
                    pk=delivery_person_id,
                    branch_id=target_branch,
                    is_deleted=False,
                    is_active=True,
                )
                .first()
            )
            if not delivery_person:
                return ServiceResponse.error(
                    'Delivery person is inactive or belongs to a different branch'
                )

        display_id = OrderRepository.next_display_id(scope=target_branch)
        chef_queue_number = OrderRepository.next_chef_queue_number(
            scope=target_branch,
        )
        order_number = OrderRepository.next_order_number(scope=target_branch)

        product_ids = [item.get('product_id') for item in items]
        products = {p.id: p for p in ProductRepository.filter(id__in=product_ids)}

        total_amount = Decimal('0.00')
        order_items_data = []

        for item_data in items:
            product_id = item_data.get('product_id')
            quantity = item_data.get('quantity', 1)

            if quantity <= 0:
                return ServiceResponse.validation_error(
                    errors={'quantity': 'Must be greater than 0'},
                    message='Quantity must be greater than 0',
                )

            product = products.get(product_id)
            if not product:
                return ServiceResponse.not_found(f'Product with id {product_id} not found')

            order_items_data.append({
                'product': product,
                'detail': item_data.get('detail'),
                'quantity': quantity,
                'price': product.price,
            })
            total_amount += product.price * quantity

        order = OrderRepository.create(
            user_id=user_id,
            cashier_id=cashier_id,
            display_id=display_id,
            chef_queue_number=chef_queue_number,
            order_number=order_number,
            order_type=order_type,
            phone_number=phone_number,
            description=description,
            status='PREPARING',
            is_paid=False,
            subtotal=total_amount,
            total_amount=total_amount,
            delivery_person=delivery_person,
            branch_id=target_branch,
        )

        from base.models import OrderItem
        now = timezone.now()
        # Instant items (drinks, packaged goods) need no kitchen prep, so they
        # are born ready and never hit the chef display. Mirrors the customer
        # order path so an instant product behaves the same on every surface.
        any_kitchen_item = False
        new_items = []
        for d in order_items_data:
            instant = d['product'].is_instant
            if not instant:
                any_kitchen_item = True
            new_items.append(OrderItem(
                order=order,
                product=d['product'],
                detail=d['detail'],
                quantity=d['quantity'],
                price=d['price'],
                ready_at=now if instant else None,
                branch_id=target_branch,
            ))
        OrderItem.objects.bulk_create(new_items)
        # bulk_create bypasses SyncMixin.save(). Keep the rows in the NULL
        # safety lane until this transaction commits, then publish each exact
        # version. Stamping synced_at *inside* the transaction lets /changes
        # advance its cutoff while the uncommitted rows are invisible, losing
        # the order lines forever.
        for item in new_items:
            item._publish_synced_at_after_commit(using=item._state.db)

        # An order made up entirely of instant items has nothing to cook —
        # it's ready the moment it's placed.
        if not any_kitchen_item:
            order.status = 'READY'
            order.ready_at = now
            order.save(update_fields=['status', 'ready_at'])

        stock_items = [
            {'product_id': d['product'].id, 'quantity': d['quantity']}
            for d in order_items_data
        ]
        stock_error = _apply_order_stock_transition(
            order.id, None, 'PREPARING', stock_items, user_id,
        )
        if stock_error:
            transaction.set_rollback(True)
            return stock_error

        return ServiceResponse.created(
            data={'order_id': order.id, 'display_id': order.display_id},
            message='Order created successfully',
        )

    @staticmethod
    @transaction.atomic
    def update_order(order_id, **kwargs):
        order = OrderRepository.get_by_id(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        allowed = {'phone_number', 'description', 'order_type'}
        for key, value in kwargs.items():
            if key in allowed and hasattr(order, key):
                setattr(order, key, value)

        order.save()
        return ServiceResponse.success(message='Order updated successfully')

    @staticmethod
    @transaction.atomic
    def add_item_to_order(order_id, product_id, quantity):
        # Lock the order for the duration of the recalculate so two concurrent
        # add-item calls don't both read order.subtotal, both compute their own
        # new total, and one clobber the other. Without the lock the quantity
        # update below also races: existing.quantity += q + save() loses one
        # of the increments under concurrency.
        order = OrderRepository.get_for_update(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        if order.is_paid:
            # A paid order's total was already credited to the cash register on
            # payment. Editing items afterwards rewrites total_amount with no
            # matching register adjustment, desyncing the drawer. Block it.
            return ServiceResponse.error('Cannot modify an order that has already been paid')

        if order.status not in ['PREPARING', 'OPEN']:
            return ServiceResponse.error('Cannot modify order that is not in PREPARING status')

        product = ProductRepository.get_by_id(product_id)
        if not product:
            return ServiceResponse.not_found('Product not found')

        # A zero/negative quantity flows straight into F('quantity') + quantity
        # and the subtotal recalculate, producing a negative line and a negative
        # order total that then removes cash from the register on payment.
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            return ServiceResponse.validation_error(
                errors={'quantity': 'Must be a positive integer'},
                message='Quantity must be greater than 0',
            )

        is_instant = product.is_instant
        existing = OrderItemRepository.get_existing_unready(order_id, product_id)
        if existing and not is_instant:
            # The parent Order lock serializes every add for this ticket. Save
            # through SyncMixin so this cloud-originated change enters /changes;
            # QuerySet.update() bypassed the sync bookkeeping entirely.
            existing.quantity += quantity
            existing.save(update_fields=['quantity'])
        else:
            # Instant items are born ready and never need the kitchen.
            OrderItemRepository.create(
                order=order, product=product, quantity=quantity, price=product.price,
                ready_at=timezone.now() if is_instant else None,
                branch_id=order.branch_id,
            )

        # Only adding a real (non-instant) item reopens a ready order for the
        # kitchen; tacking on a drink must not send the order back to PREPARING.
        if not is_instant and order.ready_at:
            order.ready_at = None
            order.status = 'PREPARING'
            order.save(update_fields=['ready_at', 'status'])

        _recalculate_total(order)
        stock_error = _adjust_order_stock(order, product_id, quantity)
        if stock_error:
            transaction.set_rollback(True)
            return stock_error
        return ServiceResponse.success(message='Item added to order successfully')

    @staticmethod
    @transaction.atomic
    def update_order_item(order_id, item_id, quantity):
        order = OrderRepository.get_by_id(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        if order.is_paid:
            # A paid order's total was already credited to the cash register on
            # payment. Editing items afterwards rewrites total_amount with no
            # matching register adjustment, desyncing the drawer. Block it.
            return ServiceResponse.error('Cannot modify an order that has already been paid')

        if order.status not in ['PREPARING', 'OPEN']:
            return ServiceResponse.error('Cannot modify order that is not in PREPARING status')

        if quantity <= 0:
            return ServiceResponse.validation_error(
                errors={'quantity': 'Must be greater than 0'},
                message='Quantity must be greater than 0',
            )

        item = OrderItemRepository.first(id=item_id, order_id=order_id)
        if not item:
            return ServiceResponse.not_found('Order item not found')

        old_quantity = item.quantity
        product_id = item.product_id
        item.quantity = quantity
        item.save(update_fields=['quantity'])
        _recalculate_total(order)
        stock_error = _adjust_order_stock(order, product_id, quantity - old_quantity)
        if stock_error:
            transaction.set_rollback(True)
            return stock_error

        return ServiceResponse.success(message='Order item updated successfully')

    @staticmethod
    @transaction.atomic
    def remove_item_from_order(order_id, item_id):
        order = OrderRepository.get_by_id(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        if order.is_paid:
            # A paid order's total was already credited to the cash register on
            # payment. Editing items afterwards rewrites total_amount with no
            # matching register adjustment, desyncing the drawer. Block it.
            return ServiceResponse.error('Cannot modify an order that has already been paid')

        if order.status not in ['PREPARING', 'OPEN']:
            return ServiceResponse.error('Cannot modify order that is not in PREPARING status')

        item = OrderItemRepository.first(id=item_id, order_id=order_id)
        if not item:
            return ServiceResponse.not_found('Order item not found')

        product_id = item.product_id
        removed_quantity = item.quantity
        # Cloud hard deletes have no durable change-feed record. Preserve the
        # line as a synchronized soft-delete tombstone so branch totals drop it
        # as well.
        item.delete()

        # Return ingredient stock for the removed line *before* any order
        # deletion so the deduction can still be located and reversed.
        stock_error = _adjust_order_stock(order, product_id, -removed_quantity)
        if stock_error:
            transaction.set_rollback(True)
            return stock_error

        if not order.items.filter(is_deleted=False).exists():
            order.delete()
            return ServiceResponse.success(message='Order deleted (no items remaining)')

        _check_and_update_ready(order)
        _recalculate_total(order)
        return ServiceResponse.success(message='Item removed from order successfully')

    @staticmethod
    @transaction.atomic
    def update_order_status(order_id, status, cashier_id=None, reason=''):
        order = OrderRepository.get_for_update(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        if status not in ALLOWED_STATUSES:
            return ServiceResponse.error(f'Invalid status. Allowed: {", ".join(ALLOWED_STATUSES)}')

        if order.status == 'CANCELED':
            return ServiceResponse.error('Cannot update cancelled order')

        old_status = order.status
        update_fields = ['status']
        order.status = status

        refund = None
        if status == 'CANCELED' and order.is_paid:
            from base.services.order_refund import (
                SettlementInvariantError, record_paid_order_refund,
            )
            try:
                refund, _ = record_paid_order_refund(
                    order.id, cashier_id, reason=reason,
                )
            except SettlementInvariantError as exc:
                return ServiceResponse.error(str(exc))

        if status == 'READY':
            now = timezone.now()
            order.ready_at = now
            for item in order.items.select_for_update().filter(
                is_deleted=False, ready_at__isnull=True,
            ):
                item.ready_at = now
                item.save(update_fields=['ready_at'])
            update_fields.append('ready_at')

        order.save(update_fields=update_fields)

        stock_items = [
            {'product_id': i.product_id, 'quantity': i.quantity}
            for i in order.items.filter(is_deleted=False)
        ]
        stock_error = _apply_order_stock_transition(
            order.id, old_status, status, stock_items, order.user_id,
        )
        if stock_error:
            transaction.set_rollback(True)
            return stock_error

        # Loyalty accrual — silent no-op for non-eligible transitions
        # (not COMPLETED, unpaid, no phone, already credited). Idempotent.
        _schedule_loyalty_accrual(order.id)

        return ServiceResponse.success(
            data={
                'status': status,
                'refund_id': refund.id if refund else None,
            },
            message=f'Order status updated to {status}',
        )

    @staticmethod
    @transaction.atomic
    def mark_as_paid(order_id, payment_method='CASH', payments=None,
                     cashier_id=None):
        """Mark an order paid, WRITING the tender line(s). Mirrors the till path.

        Two input shapes:
          - single tender: payment_method='CASH'          -> one full-amount line
          - split:         payments=[{method,amount}, ...] -> one line per component

        Previously this set only is_paid/payment_method/paid_at and wrote no
        OrderPayment row, so a cloud-paid sale carried no tender lines. MIXED stays an
        OUTPUT-only roll-up: it cannot be a bare input method because a single method
        carries no split to decompose -- send `payments` instead.
        """
        from base.models import Order, OrderPayment
        order = OrderRepository.get_for_update(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        if order.status == 'CANCELED':
            return ServiceResponse.error('Cancelled order cannot be paid')

        if order.is_paid:
            return ServiceResponse.error('Order already paid')

        from base.services.order_refund import (
            SettlementInvariantError, lock_active_cashier_shift,
        )
        # Service callers historically omit cashier_id for an order that is
        # already assigned to a cashier.  The assigned cashier is the monetary
        # owner; keep explicit cashier_id as the override used by the endpoint.
        cashier_id = cashier_id or order.cashier_id
        try:
            settlement_shift = lock_active_cashier_shift(
                cashier_id, branch_id=order.branch_id,
            )
        except SettlementInvariantError as exc:
            return ServiceResponse.error(str(exc))

        # Concrete tenders only: MIXED is the roll-up the system SETS, never an input.
        valid_methods = [c[0] for c in Order.PaymentMethod.choices if c[0] != 'MIXED']
        total = Decimal(order.total_amount or 0)

        if payments:
            lines = []
            for p in payments:
                method = str((p or {}).get('method', '')).upper()
                if method not in valid_methods:
                    return ServiceResponse.validation_error(
                        errors={'payments': f'method must be one of {valid_methods}'})
                try:
                    amount = Decimal(str((p or {}).get('amount')))
                except Exception:  # noqa: BLE001
                    return ServiceResponse.validation_error(
                        errors={'payments': 'amount must be a number'})
                if amount <= 0:
                    return ServiceResponse.validation_error(
                        errors={'payments': 'amount must be > 0'})
                lines.append((method, amount))
        else:
            payment_method = str(payment_method or '').upper()
            if payment_method not in valid_methods:
                return ServiceResponse.validation_error(
                    errors={'payment_method': f'Must be one of {valid_methods}; '
                                              f'MIXED requires a `payments` split'},
                )
            lines = [(payment_method, total)]

        from django.conf import settings as django_settings
        if (getattr(django_settings, 'DEPLOYMENT_MODE', 'local') == 'cloud'
                and any(method == Order.PaymentMethod.CASH for method, _ in lines)):
            return ServiceResponse.error(
                'Physical cash payment must be settled on the owning branch desktop.'
            )

        paid_sum = sum((a for _, a in lines), Decimal('0'))
        noncash = sum((a for m, a in lines if m != 'CASH'), Decimal('0'))
        if paid_sum < total:
            return ServiceResponse.validation_error(
                errors={'payments': 'Payments do not cover the total'},
                message=f'Short by {total - paid_sum}')
        # Only cash may over-tender (the customer's change); card/Payme never can.
        if noncash > total:
            return ServiceResponse.validation_error(
                errors={'payments': 'Non-cash overpayment is not allowed'})

        branch_assigned = False
        if not order.branch_id:
            order.branch_id = settlement_shift.branch_id
            branch_assigned = True
        from base.services.accounting_cursor import lock_branch_accounting
        lock_branch_accounting(order.branch_id)
        paid_event_at = timezone.now()

        distinct = {m for m, _ in lines}
        payment_action_id = uuid4()
        order.is_paid = True
        order.payment_action_id = payment_action_id
        order.payment_method = (next(iter(distinct)) if len(distinct) == 1
                                else Order.PaymentMethod.MIXED)
        order.paid_at = paid_event_at
        order.accounting_recorded_at = paid_event_at
        order.cashier_id = settlement_shift.user_id
        payment_update_fields = [
            'is_paid', 'payment_action_id', 'payment_method', 'paid_at',
            'accounting_recorded_at', 'cashier',
        ]
        if branch_assigned:
            payment_update_fields.append('branch_id')
        order.save(update_fields=payment_update_fields)

        # The tender lines: what makes this sale visible to per-tender shift settlement
        # (cashbox.drawer) and to base.services.tender.
        for line_index, (method, amount) in enumerate(lines):
            OrderPayment.objects.create(
                order=order,
                method=method,
                amount=amount,
                payment_action_id=payment_action_id,
                line_index=line_index,
                branch_id=order.branch_id,
            )

        # The drawer only holds physical cash, net of change. Card/Payme settle
        # externally and reconcile against the acquirer report, not the register.
        cash_to_drawer = total - noncash
        if cash_to_drawer > 0:
            InkassaService.add_to_register(cash_to_drawer, order.branch_id)

        stock_items = [
            {'product_id': i.product_id, 'quantity': i.quantity}
            for i in order.items.filter(is_deleted=False)
        ]
        stock_error = _apply_order_stock_transition(
            order.id, order.status, 'PAID', stock_items, order.user_id,
        )
        if stock_error:
            transaction.set_rollback(True)
            return stock_error

        # Pay completes the second half of the COMPLETED + paid eligibility
        # check for loyalty. No-op if order isn't COMPLETED yet — the next
        # update_order_status call will pick it up.
        _schedule_loyalty_accrual(order.id)

        # Fiscalize the sale (Soliq). No-op unless enabled; serve-now policy
        # means a provider failure never blocks the sale (queued for retry).
        _schedule_fiscalization(order.id)

        return ServiceResponse.success(
            data={'is_paid': True},
            message='Order marked as paid',
        )

    @staticmethod
    @transaction.atomic
    def mark_as_unpaid(order_id, cashier_id=None):
        order = OrderRepository.get_for_update(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        # Cancelling a paid order already reversed its cash through the drawer
        # (update_order_status CANCELED path) while deliberately leaving
        # is_paid=True. Reversing again here would double-credit the register.
        if order.status == 'CANCELED':
            return ServiceResponse.error('Cancelled order cannot be marked unpaid')

        if not order.is_paid:
            return ServiceResponse.error('Order is not paid')

        from base.services.order_refund import (
            SettlementInvariantError, record_paid_order_refund,
        )
        cashier_id = cashier_id or order.cashier_id
        try:
            refund, _created = record_paid_order_refund(
                order, cashier_id, reason='Admin void/unpay correction',
            )
        except SettlementInvariantError as exc:
            return ServiceResponse.error(str(exc))

        old_status = order.status
        order.status = 'CANCELED'
        order.save(update_fields=['status'])
        stock_items = [
            {'product_id': item.product_id, 'quantity': item.quantity}
            for item in order.items.filter(is_deleted=False)
        ]
        stock_error = _apply_order_stock_transition(
            order.id, old_status, 'CANCELED', stock_items, order.user_id,
        )
        if stock_error:
            transaction.set_rollback(True)
            return stock_error

        # This endpoint is now a terminal correction, not an invitation to
        # rewrite and repay the same sale. paid_at/payment_method/tender rows are
        # deliberately preserved and the dated refund is the negative event.
        return ServiceResponse.success(
            data={
                'is_paid': True,
                'status': 'CANCELED',
                'refund_id': refund.id if refund else None,
            },
            message='Paid order voided with an immutable refund',
        )

    @staticmethod
    @transaction.atomic
    def mark_order_ready(order_id):
        # Row-lock the order so two concurrent ready-flips can't both pass
        # the status guard and run the side-effects twice.
        order = OrderRepository.get_for_update(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        if order.status == 'CANCELED':
            return ServiceResponse.error('Cannot mark cancelled order as ready')

        if order.status == 'READY':
            return ServiceResponse.error('Order is already ready')

        now = timezone.now()
        order.status = 'READY'
        order.ready_at = now
        order.save(update_fields=['status', 'ready_at'])
        for item in order.items.select_for_update().filter(
            is_deleted=False, ready_at__isnull=True,
        ):
            item.ready_at = now
            item.save(update_fields=['ready_at'])

        order_prep_time = (order.ready_at - order.created_at).total_seconds()

        return ServiceResponse.success(
            data={
                'status': order.status,
                'ready_at': order.ready_at.isoformat(),
                'preparation_time_seconds': order_prep_time,
                'preparation_time_formatted': _format_duration(order_prep_time),
            },
            message='Order marked as ready',
        )

    @staticmethod
    @transaction.atomic
    def mark_item_ready(order_id, item_id):
        order = OrderRepository.get_by_id_with_relations(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        if order.status == 'CANCELED':
            return ServiceResponse.error('Cannot modify cancelled order')

        if order.status == 'READY':
            return ServiceResponse.error('Order is already marked as ready')

        item = order.items.filter(id=item_id, is_deleted=False).first()
        if not item:
            return ServiceResponse.not_found('Order item not found')

        if item.ready_at is not None:
            return ServiceResponse.error('Item is already marked as ready')

        now = timezone.now()
        item.ready_at = now
        item.save(update_fields=['ready_at'])

        item_prep_time = (item.ready_at - order.created_at).total_seconds()
        all_ready, order_became_ready = _check_and_update_ready(order)

        order_prep_time = None
        if order_became_ready and order.ready_at:
            order_prep_time = (order.ready_at - order.created_at).total_seconds()

        return ServiceResponse.success(
            data={
                'item': {
                    'id': item.id,
                    'product_name': item.product.name,
                    'ready_at': item.ready_at.isoformat(),
                    'preparation_time_seconds': item_prep_time,
                    'preparation_time_formatted': _format_duration(item_prep_time),
                },
                'order': {
                    'id': order.id,
                    'display_id': order.display_id,
                    'status': order.status,
                    'all_items_ready': all_ready,
                    'ready_at': order.ready_at.isoformat() if order.ready_at else None,
                    'preparation_time_seconds': order_prep_time,
                    'preparation_time_formatted': _format_duration(order_prep_time) if order_prep_time else None,
                },
            },
            message='Item marked as ready',
        )

    @staticmethod
    @transaction.atomic
    def unmark_item_ready(order_id, item_id):
        order = OrderRepository.get_by_id(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        if order.status == 'CANCELED':
            return ServiceResponse.error('Cannot modify cancelled order')

        from base.models import OrderItem as OI
        item = OI.objects.select_for_update().filter(
            id=item_id, order=order, ready_at__isnull=False
        ).first()

        if not item:
            return ServiceResponse.error('Item is not marked as ready')

        item.ready_at = None
        item.save(update_fields=['ready_at'])

        if order.status == 'READY':
            order.status = 'PREPARING'
            order.ready_at = None
            order.save(update_fields=['status', 'ready_at'])

        return ServiceResponse.success(
            data={'item_id': item_id, 'order_status': order.status},
            message='Item unmarked as ready',
        )

    @staticmethod
    def delete_order(order_id, hard_delete=False):
        if hard_delete:
            from base.models import Order
            try:
                order = Order.objects.get(pk=order_id)
            except Order.DoesNotExist:
                return ServiceResponse.not_found('Order not found')
            # Keep a cloud tombstone row. Physical deletion is invisible to
            # branch timestamp cursors and caused supposedly deleted orders to
            # survive indefinitely on tills and in their analytics.
            order.delete()
            return ServiceResponse.success(message='Order archived successfully')

        order = OrderRepository.get_by_id(order_id)
        if not order:
            return ServiceResponse.not_found('Order not found')

        order.is_deleted = True
        order.save(update_fields=['is_deleted', 'synced_at', 'sync_version'])
        return ServiceResponse.success(message='Order deleted successfully')

    @staticmethod
    def restore_order(order_id):
        from base.models import Order
        try:
            order = Order.objects.get(pk=order_id)
        except Order.DoesNotExist:
            return ServiceResponse.not_found('Order not found')

        if not order.is_deleted:
            return ServiceResponse.error('Order is not deleted')

        order.is_deleted = False
        order.save()
        return ServiceResponse.success(
            data={'order': {'id': order.id, 'display_id': order.display_id}},
            message='Order restored successfully',
        )

    @staticmethod
    def get_order_stats(date_from=None, date_to=None, cashier_id=None,
                        product_ids=None, tod_from=None, tod_to=None,
                        statuses=None, payment_status=None, category_ids=None,
                        user_id=None, order_type=None, payment_method=None,
                        search=None, include_deleted=False,
                        datetime_from=None, datetime_to=None,
                        from_at=None, to_at=None):
        from django.db.models import (
            Avg, Count, DecimalField, DurationField, ExpressionWrapper, F, Q,
            Sum,
        )
        from django.db.models.functions import Coalesce
        from base.models import Order, OrderRefund

        filtered, applied_filters = _build_filtered_order_queryset(
            statuses=statuses,
            payment_status=payment_status,
            category_ids=category_ids,
            product_ids=product_ids,
            user_id=user_id,
            cashier_id=cashier_id,
            order_type=order_type,
            date_from=date_from,
            date_to=date_to,
            payment_method=payment_method,
            search=search,
            include_deleted=include_deleted,
            tod_from=tod_from,
            tod_to=tod_to,
            datetime_from=datetime_from,
            datetime_to=datetime_to,
            from_at=from_at,
            to_at=to_at,
        )
        # Collapse relation joins (product/category/search/tender) to one stable
        # Order membership subquery before aggregating. This prevents fan-out and
        # makes the figures global across every matching row, never the list page.
        order_ids = filtered.order_by().values('pk')
        population = Order.objects.filter(pk__in=order_ids)
        # Payment status is a row-membership property on the Orders page. Do
        # not require ``paid_at`` here: a legacy/synced paid row with a missing
        # event timestamp must still land in PAID (and its amount), otherwise
        # PAID + UNPAID can be smaller than the filtered list itself.
        paid = population.filter(is_paid=True)
        refunds = OrderRefund.objects.filter(
            is_deleted=False,
            order_id__in=population.order_by().values('pk'),
        )

        stats = population.aggregate(
            total=Count('id'),
            open=Count('id', filter=Q(status='OPEN')),
            preparing=Count('id', filter=Q(status='PREPARING')),
            ready=Count('id', filter=Q(status='READY')),
            completed=Count('id', filter=Q(status='COMPLETED')),
            cancelled=Count('id', filter=Q(status='CANCELED')),
            paid_membership=Count('id', filter=Q(is_paid=True)),
            # Historical API contract: UNPAID is the actionable settlement
            # queue. OPEN tickets and cancelled rows remain visible in
            # status_counts, but are not work waiting at the cashier.
            unpaid=Count(
                'id',
                filter=Q(
                    is_paid=False,
                    status__in=['PREPARING', 'READY', 'COMPLETED'],
                ),
            ),
        )
        money = paid.aggregate(
            gross_revenue=Coalesce(
                Sum('total_amount'), Decimal('0.00'),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
            avg_order_value=Coalesce(
                Avg('total_amount'), Decimal('0.00'),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
            paid=Count('id'),
        )
        refund_totals = refunds.aggregate(
            amount=Coalesce(
                Sum('amount'), Decimal('0.00'),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
            count=Count('id'),
        )
        total_revenue = money['gross_revenue'] - refund_totals['amount']
        prep = population.filter(
            ready_at__isnull=False,
            status__in=['READY', 'COMPLETED'],
        ).aggregate(avg=Avg(ExpressionWrapper(
            F('ready_at') - F('created_at'),
            output_field=DurationField(),
        )))['avg']
        avg_prep = int(prep.total_seconds()) if prep else None

        # Canonical tender over that exact membership. A MIXED order contributes
        # to its real cash/card/Payme lines, with refunds netted once.
        try:
            from base.services.tender import net_breakdown
            _split, _detail = net_breakdown(paid, refunds)
            payment_breakdown = {k: str(_split[k]) for k in ('cash', 'card', 'payme')}
            if _split['unknown']:
                payment_breakdown['unknown'] = str(_split['unknown'])
            payment_breakdown['card_detail'] = {k: str(v) for k, v in _detail.items()}
        except Exception:
            logger.exception('order stats tender breakdown failed')
            payment_breakdown = {'cash': '0', 'card': '0', 'payme': '0'}

        # Per-status + payment-status breakdowns over the SAME windowed/filtered
        # aggregate (all keys always present, zero-filled). PAID/UNPAID reuse the
        # existing paid/unpaid semantics so they match paid_orders/unpaid_orders.
        status_counts = {
            'OPEN': stats.get('open', 0),
            'PREPARING': stats['preparing'],
            'READY': stats['ready'],
            'COMPLETED': stats['completed'],
            'CANCELED': stats['cancelled'],
        }
        payment_counts = {
            'PAID': stats['paid_membership'],
            'UNPAID': stats['unpaid'],
        }

        return ServiceResponse.success(data={
            'total_orders': stats['total'],
            'preparing_orders': stats['preparing'],
            'ready_orders': stats['ready'],
            'completed_orders': stats['completed'],
            'cancelled_orders': stats['cancelled'],
            'paid_orders': stats['paid_membership'],
            'unpaid_orders': stats['unpaid'],
            'total_revenue': str(total_revenue),
            'gross_revenue': str(money['gross_revenue']),
            'refund_amount': str(refund_totals['amount']),
            'refunded_orders': refund_totals['count'],
            'avg_order_value': str(money['avg_order_value']),
            'payment_breakdown': payment_breakdown,
            'status_counts': status_counts,
            'payment_counts': payment_counts,
            'average_preparation_time_seconds': avg_prep,
            'average_preparation_time_formatted': _format_duration(avg_prep) if avg_prep else None,
            'filters': applied_filters,
        })

    @staticmethod
    def get_daily_stats(date_from=None, date_to=None, cashier_id=None,
                        tod_from=None, tod_to=None):
        from base.services.business_day import parse_hhmm
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)
        tod_from_t, tod_to_t = parse_hhmm(tod_from), parse_hhmm(tod_to)

        if not date_from_dt:
            date_from_dt = timezone.now() - timedelta(days=30)
        if not date_to_dt:
            date_to_dt = timezone.now()

        daily = OrderRepository.get_daily_stats(
            date_from_dt, date_to_dt, cashier_id, tod_from=tod_from_t, tod_to=tod_to_t)

        return ServiceResponse.success(data={
            'daily_stats': [{
                'date': d['date'].isoformat() if d['date'] else None,
                'orders': d['orders'],
                'revenue': str(d['revenue']),
                'paid': d['paid'],
                'cancelled': d['cancelled'],
            } for d in daily],
            'period': {
                'from': date_from_dt.isoformat(),
                'to': date_to_dt.isoformat(),
            },
        })

    @staticmethod
    def get_monthly_stats(date_from=None, date_to=None):
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)

        if not date_from_dt:
            date_from_dt = timezone.now() - timedelta(days=365)

        monthly = OrderRepository.get_monthly_stats(date_from_dt, date_to_dt)

        return ServiceResponse.success(data={
            'monthly_stats': [{
                'month': m['month'].isoformat() if m['month'] else None,
                'orders': m['orders'],
                'revenue': str(m['revenue']),
                'paid': m['paid'],
                'cancelled': m['cancelled'],
                'avg_order_value': str(m['avg_order_value']),
            } for m in monthly],
        })

    @staticmethod
    def get_yearly_stats():
        yearly = OrderRepository.get_yearly_stats()

        return ServiceResponse.success(data={
            'yearly_stats': [{
                'year': y['year'].year if y['year'] else None,
                'orders': y['orders'],
                'revenue': str(y['revenue']),
                'paid': y['paid'],
                'cancelled': y['cancelled'],
            } for y in yearly],
        })

    @staticmethod
    def get_cashier_stats(date_from=None, date_to=None):
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)

        by_cashier = OrderRepository.get_by_cashier_stats(date_from_dt, date_to_dt)

        return ServiceResponse.success(data={
            'cashier_stats': [{
                'cashier_id': c['cashier_id'],
                'cashier_name': f"{c['cashier__first_name']} {c['cashier__last_name']}",
                'orders': c['orders'],
                'revenue': str(c['revenue']),
                'paid': c['paid'],
                'cancelled': c['cancelled'],
            } for c in by_cashier],
        })

    @staticmethod
    def get_status_stats(date_from=None, date_to=None):
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)

        by_status = OrderRepository.get_by_status_stats(date_from_dt, date_to_dt)

        return ServiceResponse.success(data={
            'status_stats': [{
                'status': s['status'],
                'count': s['count'],
                'revenue': str(s['revenue']),
            } for s in by_status],
        })

    @staticmethod
    def get_order_type_stats(date_from=None, date_to=None):
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)

        by_type = OrderRepository.get_by_order_type_stats(date_from_dt, date_to_dt)

        return ServiceResponse.success(data={
            'order_type_stats': [{
                'order_type': t['order_type'],
                'count': t['count'],
                'revenue': str(t['revenue']),
            } for t in by_type],
        })

    @staticmethod
    def get_top_products(date_from=None, date_to=None, limit=20):
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)

        top = OrderItemRepository.get_top_products(date_from_dt, date_to_dt, limit)

        return ServiceResponse.success(data={
            'top_products': [{
                'product_id': p['product_id'],
                'product_name': p['product__name'],
                'category_name': p['product__category__name'],
                'total_quantity': p['total_qty'],
                'total_revenue': str(p['total_revenue']),
                'order_count': p['order_count'],
            } for p in top],
        })

    @staticmethod
    def get_least_sold_products(date_from=None, date_to=None, limit=20):
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)

        least = OrderItemRepository.get_least_sold_products(date_from_dt, date_to_dt, limit)

        return ServiceResponse.success(data={
            'least_sold_products': [{
                'product_id': p['product_id'],
                'product_name': p['product__name'],
                'category_name': p['product__category__name'],
                'total_quantity': p['total_qty'],
                'total_revenue': str(p['total_revenue']),
                'order_count': p['order_count'],
            } for p in least],
        })

    @staticmethod
    def get_category_stats(date_from=None, date_to=None):
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)

        by_cat = OrderItemRepository.get_product_category_stats(date_from_dt, date_to_dt)

        return ServiceResponse.success(data={
            'category_stats': [{
                'category_id': c['product__category_id'],
                'category_name': c['product__category__name'],
                'total_quantity': c['total_qty'],
                'total_revenue': str(c['total_revenue']),
                'order_count': c['order_count'],
            } for c in by_cat],
        })

    @staticmethod
    def get_hourly_stats(date_from=None, date_to=None, tod_from=None, tod_to=None):
        from base.services.business_day import parse_hhmm
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)
        tod_from_t, tod_to_t = parse_hhmm(tod_from), parse_hhmm(tod_to)

        hourly = OrderRepository.get_hourly_distribution(
            date_from_dt, date_to_dt, tod_from=tod_from_t, tod_to=tod_to_t)

        return ServiceResponse.success(data={
            'hourly_stats': [{
                'hour': h['hour'],
                'count': h['count'],
                'revenue': str(h['revenue']),
            } for h in hourly],
        })

    @staticmethod
    def get_dashboard_stats(date_from=None, date_to=None, tod_from=None, tod_to=None):
        from base.services.business_day import parse_hhmm
        date_from_dt = _parse_date(date_from)
        date_to_dt = _parse_date_to(date_to)
        tod_from_t, tod_to_t = parse_hhmm(tod_from), parse_hhmm(tod_to)
        _tk = {'tod_from': tod_from_t, 'tod_to': tod_to_t}

        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today.replace(day=1)

        today_stats = OrderRepository.get_stats_aggregate(today, None, **_tk)
        month_stats = OrderRepository.get_stats_aggregate(month_start, None, **_tk)
        overall_stats = OrderRepository.get_stats_aggregate(date_from_dt, date_to_dt, **_tk)
        avg_prep = OrderRepository.get_avg_prep_time(today, None)

        top = OrderItemRepository.get_top_products(date_from_dt, date_to_dt, 5)
        least = OrderItemRepository.get_least_sold_products(date_from_dt, date_to_dt, 5)

        return ServiceResponse.success(data={
            'today': {
                'orders': today_stats['total'],
                'revenue': str(today_stats['total_revenue']),
                'paid': today_stats['paid'],
                'cancelled': today_stats['cancelled'],
                'avg_prep_time': _format_duration(avg_prep) if avg_prep else None,
            },
            'this_month': {
                'orders': month_stats['total'],
                'revenue': str(month_stats['total_revenue']),
                'paid': month_stats['paid'],
                'cancelled': month_stats['cancelled'],
            },
            'overall': {
                'total_orders': overall_stats['total'],
                'total_revenue': str(overall_stats['total_revenue']),
                'avg_order_value': str(overall_stats['avg_order_value']),
            },
            'top_products': [{
                'product_name': p['product__name'],
                'total_quantity': p['total_qty'],
            } for p in top],
            'least_sold_products': [{
                'product_name': p['product__name'],
                'total_quantity': p['total_qty'],
            } for p in least],
        })
