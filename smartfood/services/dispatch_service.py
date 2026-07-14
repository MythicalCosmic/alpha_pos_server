"""Dispatch a PENDING BotOrder to a SPECIFIC on-duty cashier — the core flow.

Creates a real base.Order under that cashier so it lands in THAT cashier's shift
(POS attributes orders by cashier_id + the shift's time window — there is no Shift
FK). OrderItem prices are the bot's FROZEN unit prices (base + size + toppings),
so the cashier's revenue and the customer's charge match the quote — note that
AdminOrderService.create_order would re-price at base product price only, which is
why we mint the order directly here. The existing Order post_save signal
broadcasts it to the cashier queue + KDS automatically.
"""
import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.repositories import OrderRepository
from smartfood.models import BotOrder, Customer

logger = logging.getLogger(__name__)

_PLACEHOLDER_EMAIL = 'smartfood-bot@local'


def _auto_assign_courier_safe(bot_order_id, pos_order_id):
    """Auto-assign an available courier to a freshly-dispatched DELIVERY order.
    Default OFF (COURIER_AUTO_ASSIGN); manual assignment via
    POST /api/admins/couriers/assign is always available. Best-effort — never
    breaks the dispatch; skips if a courier was already assigned by hand."""
    try:
        from couriers.models import DeliveryAssignment
        from couriers.services import assign, pick_available_courier
        from base.models import Order
        if DeliveryAssignment.objects.filter(order_id=pos_order_id).exists():
            return                                  # already assigned (e.g. manually)
        courier = pick_available_courier()
        if not courier:
            logger.info('auto courier-assign: no available courier (order=%s)', pos_order_id)
            return
        order = Order.objects.filter(id=pos_order_id).first()
        bot_order = BotOrder.objects.select_related('address').filter(id=bot_order_id).first()
        if not order or not bot_order:
            return
        addr = bot_order.address
        assign(order, courier, fee=bot_order.delivery_fee,
               addr_text=bot_order.address_text,
               addr_lat=(addr.lat if addr else None),
               addr_lng=(addr.lng if addr else None))
    except Exception:
        logger.exception('auto courier-assign failed (order=%s)', pos_order_id)


def _bot_customer_user():
    """Singleton placeholder base.User used as Order.user for dispatched bot
    orders (the real customer identity lives on the BotOrder). SUSPENDED so it can
    never log in. base.Order.user is required, so dispatched orders need a row."""
    from base.models import User
    user, _ = User.objects.get_or_create(
        email=_PLACEHOLDER_EMAIL,
        defaults={'first_name': 'Smart Food', 'last_name': 'Customer',
                  'role': 'USER', 'status': 'SUSPENDED', 'password': '!'},
    )
    return user


def _notify(bot_order, event):
    try:
        from smartfood.services.notification_service import notify_customer
        notify_customer(bot_order, event)
    except Exception:
        logger.debug('customer notify failed (%s)', event, exc_info=True)
    # Push the same transition to the customer's Mini App over WebSocket, AFTER
    # the surrounding transaction commits (so the client never sees a status that
    # was rolled back). Best-effort — realtime never breaks the order flow.
    try:
        from smartfood.realtime import publish_bot_order_event
        _oid = bot_order.id
        transaction.on_commit(lambda: publish_bot_order_event(_oid, event))
    except Exception:
        logger.debug('customer ws publish schedule failed (%s)', event, exc_info=True)


class DispatchService:
    @staticmethod
    @transaction.atomic
    def dispatch(bot_order_id, cashier_id, operator=None):
        bot_order = (BotOrder.objects.select_for_update()
                     .filter(id=bot_order_id).first())
        if not bot_order:
            return ServiceResponse.not_found('Order not found')
        if bot_order.status != BotOrder.Status.PENDING:
            return {'success': False, 'code': 'already_handled',
                    'message': f'Order already {bot_order.status.lower()}'}, 409

        # Lock both cashier + ACTIVE shift and take branch ownership from that
        # pair.  The cloud process has no single BRANCH_ID; using its environment
        # here created global/blank orders that never belonged to the target
        # till and could disappear from branch analytics after sync.
        from base.services.order_refund import (
            SettlementInvariantError, lock_active_cashier_shift,
        )
        try:
            active_shift = lock_active_cashier_shift(cashier_id)
        except SettlementInvariantError as exc:
            return ServiceResponse.error(str(exc))
        target_branch = str(active_shift.branch_id or '').strip()

        items = list(bot_order.items.select_related('product').all())
        if not items:
            return ServiceResponse.error('Order has no items')

        from base.models import OrderItem
        now = timezone.now()
        placeholder = _bot_customer_user()
        # Reconcile the Telegram customer onto the unified base.Customer so the POS
        # order carries the client id — the same Order.customer link the desktop POS
        # sets. Customer.resolve matches by phone FIRST then telegram_id, so a
        # dispatched bot order converges onto the walk-in's existing in-store client
        # row when the phone matches (instead of forking a telegram-only row).
        pos_customer = None
        sf_customer = bot_order.customer
        if sf_customer is not None:
            from base.models import Customer as PosCustomer
            pos_customer, _ = PosCustomer.resolve(
                phone=sf_customer.phone_number or bot_order.phone_number or None,
                telegram_id=sf_customer.telegram_id,
                name=sf_customer.name,
                branch_id=target_branch,
                adopt_node_owned=True,
            )
        food_total = sum((it.line_total for it in items), Decimal('0.00'))

        # The POS order's total_amount must equal what the customer actually pays
        # (food net of loyalty discount, plus delivery + tip) so the cashier
        # collects/records the right amount. The full breakdown goes in the
        # description so it's visible on the till + courier slip.
        econ = [f'Food {int(food_total)}']
        if bot_order.delivery_fee:
            econ.append(f'Delivery +{int(bot_order.delivery_fee)}')
        if bot_order.tip:
            econ.append(f'Tip +{int(bot_order.tip)}')
        if bot_order.discount:
            econ.append(f'Loyalty -{int(bot_order.discount)}')
        econ.append(f'TOTAL {int(bot_order.total)} ({bot_order.payment_method})')
        parts = [p for p in (bot_order.address_text, bot_order.note, ' | '.join(econ)) if p]
        description = ' || '.join(parts)

        order = OrderRepository.create(
            user_id=placeholder.id,
            cashier_id=cashier_id,
            customer_id=pos_customer.id if pos_customer else None,
            display_id=OrderRepository.next_display_id(scope=target_branch),
            chef_queue_number=OrderRepository.next_chef_queue_number(
                scope=target_branch,
            ),
            order_number=OrderRepository.next_order_number(
                scope=target_branch,
            ),
            order_type=bot_order.order_type,          # DELIVERY / PICKUP (both valid)
            phone_number=bot_order.phone_number,
            description=description,
            status='PREPARING',
            is_paid=False,
            subtotal=food_total,
            # Loyalty is a product discount. Persist it on the canonical order
            # so product/category analytics can allocate it across food lines;
            # delivery and tips remain non-product revenue in total_amount.
            discount_amount=bot_order.discount,
            total_amount=bot_order.total,             # what the customer pays (nets discount + delivery + tip)
            branch_id=target_branch,
        )

        any_kitchen = False
        new_items = []
        for it in items:
            instant = it.product.is_instant
            any_kitchen = any_kitchen or (not instant)
            new_items.append(OrderItem(
                order=order, product=it.product, detail=(it.detail or None),
                quantity=it.quantity, price=it.unit_price,   # frozen: base + size + toppings
                ready_at=now if instant else None,
                branch_id=target_branch,
            ))
        OrderItem.objects.bulk_create(new_items)
        # Publish only after the dispatch transaction commits. A pre-commit
        # timestamp can be captured by the change-feed cursor while these rows
        # are still invisible, permanently dispatching an empty order shell.
        for item in new_items:
            item._publish_synced_at_after_commit(using=item._state.db)

        if not any_kitchen:
            order.status = 'READY'
            order.ready_at = now
            order.save(update_fields=['status', 'ready_at'])

        # Toppings are price-only; base products drive stock.  Dispatch and its
        # configured deduction are one transaction: returning success after an
        # inventory failure creates a sale that analytics can never reconcile.
        try:
            from stock.services import OrderStatusHandler, StockSettingsService
            stock_settings = StockSettingsService.load()
            stock_active = (
                stock_settings.stock_enabled
                and getattr(stock_settings, 'auto_deduct_on_sale', True)
            )
            location_id = (
                StockSettingsService.get_default_location_id()
                if stock_active else None
            )
            if (stock_active
                    and not location_id
                    and (stock_settings.reserve_on_order_create
                         or stock_settings.deduct_on_order_status == 'PREPARING')):
                transaction.set_rollback(True)
                return ServiceResponse.error(
                    'Stock is enabled but no default stock location is configured.'
                )
            if stock_active:
                result, status = OrderStatusHandler.on_status_change(
                    order.id, None, 'PREPARING',
                    [{'product_id': it.product_id, 'quantity': it.quantity} for it in items],
                    location_id, placeholder.id,
                )
                if status >= 400:
                    transaction.set_rollback(True)
                    return result, status
        except Exception:
            logger.exception('stock handler failed during bot dispatch (order=%s)', order.id)
            transaction.set_rollback(True)
            return ServiceResponse.error(
                'Stock processing failed; the order was not dispatched. Please retry.'
            )

        # Credit earned loyalty points now that it's a real order (via the ledger).
        if bot_order.loyalty_points_earned:
            from smartfood.models import LoyaltyTransaction
            from smartfood.services.loyalty_service import LoyaltyService
            LoyaltyService.record(
                bot_order.customer_id, LoyaltyTransaction.Kind.EARN_ORDER,
                bot_order.loyalty_points_earned,
                reason=f'Earned on order {bot_order.code}', bot_order=bot_order)

        bot_order.pos_order = order
        bot_order.dispatched_cashier_id = cashier_id
        bot_order.dispatched_by = operator
        bot_order.dispatched_at = now
        bot_order.status = BotOrder.Status.DISPATCHED
        bot_order.save(update_fields=['pos_order', 'dispatched_cashier', 'dispatched_by',
                                      'dispatched_at', 'status', 'updated_at'])
        _notify(bot_order, 'dispatched')
        # Auto courier-assign (default OFF): hand a DELIVERY order to an available
        # courier right after dispatch. Runs after commit; manual assignment via
        # POST /api/admins/couriers/assign stays the default and an override.
        if order.order_type == 'DELIVERY' and getattr(settings, 'COURIER_AUTO_ASSIGN', False):
            _bo_id, _po_id = bot_order.id, order.id
            transaction.on_commit(lambda: _auto_assign_courier_safe(_bo_id, _po_id))
        return ServiceResponse.success(data={
            'bot_order_id': bot_order.id,
            'pos_order_id': order.id,
            'pos_order_uuid': str(order.uuid),
            'display_id': order.display_id,
        })

    @staticmethod
    @transaction.atomic
    def reject(bot_order_id, reason='', operator=None):
        bot_order = BotOrder.objects.select_for_update().filter(id=bot_order_id).first()
        if not bot_order:
            return ServiceResponse.not_found('Order not found')
        if bot_order.status != BotOrder.Status.PENDING:
            return {'success': False, 'code': 'already_handled',
                    'message': f'Order already {bot_order.status.lower()}'}, 409
        if bot_order.loyalty_points_used:   # refund reserved points (via the ledger)
            from smartfood.models import LoyaltyTransaction
            from smartfood.services.loyalty_service import LoyaltyService
            LoyaltyService.record(
                bot_order.customer_id, LoyaltyTransaction.Kind.REFUND,
                bot_order.loyalty_points_used,
                reason=f'Refund rejected order {bot_order.code}', bot_order=bot_order)
        bot_order.status = BotOrder.Status.REJECTED
        bot_order.reject_reason = (reason or '')[:200]
        bot_order.dispatched_by = operator
        bot_order.save(update_fields=['status', 'reject_reason', 'dispatched_by', 'updated_at'])
        _notify(bot_order, 'rejected')
        return ServiceResponse.success(data={'bot_order_id': bot_order.id, 'status': bot_order.status})

    @staticmethod
    def auto_dispatch(bot_order_id):
        """Phase 3: resolve the active cashier on a CONNECTED till (presence
        registry) and dispatch the order to them automatically. If no POS is
        online / no on-shift cashier is present, REJECT the order (product
        decision) so the customer is told immediately rather than the order
        hanging PENDING forever. Returns the dispatch/reject (body, status)."""
        from base.services.presence import resolve_active_cashier
        resolved = resolve_active_cashier()
        if not resolved:
            logger.info('auto-dispatch: no connected POS for bot order %s -> reject',
                        bot_order_id)
            return DispatchService.reject(
                bot_order_id,
                reason='No POS terminal is online to accept the order right now',
            )
        return DispatchService.dispatch(bot_order_id, resolved['cashier_id'])

    @staticmethod
    def connected_pos():
        """Live tills + their active cashier — operator visibility (Phase 2)."""
        from base.services.presence import live_devices
        from base.models import User
        names = {}
        rows = []
        for d in live_devices():
            cid = d.get('cashier_id')
            if cid and cid not in names:
                u = User.objects.filter(id=cid).first()
                names[cid] = (f'{u.first_name} {u.last_name}'.strip() if u else '')
            rows.append({
                'device_id': d.get('device_id'),
                'branch_id': d.get('branch_id'),
                'cashier_id': cid,
                'cashier_name': names.get(cid, ''),
            })
        return ServiceResponse.success(data={'items': rows})

    @staticmethod
    def pending_queue():
        from smartfood.serializers import bot_order_dict
        orders = (BotOrder.objects.filter(status=BotOrder.Status.PENDING)
                  .prefetch_related('items').select_related('customer', 'pos_order').order_by('id'))
        return ServiceResponse.success(data={'items': [bot_order_dict(o) for o in orders]})

    @staticmethod
    def active_cashiers_list():
        from smartfood.gating import active_cashiers
        rows = [{
            'cashier_id': s.user_id,
            'name': f"{s.user.first_name} {s.user.last_name}".strip(),
            'shift_id': s.id,
            'start_time': s.start_time.isoformat() if s.start_time else None,
        } for s in active_cashiers().order_by('start_time')]
        return ServiceResponse.success(data={'items': rows})
