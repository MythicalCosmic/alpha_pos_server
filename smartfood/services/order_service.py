"""Create + read customer BotOrders (created PENDING; dispatched later).

Money is recomputed server-side via cart_service; redeemed loyalty points are
reserved at create and refunded on reject (in dispatch_service).
"""
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import F

from base.helpers.response import ServiceResponse
from smartfood.models import Address, BotOrder, BotOrderItem, Customer
from smartfood.serializers import bot_order_dict, instore_order_dict
from smartfood.services.cart_service import price_cart, CartError

logger = logging.getLogger(__name__)


def _auto_dispatch_safe(bot_order_id):
    """Run auto-dispatch outside the create transaction; never surface its errors
    to the customer (the order is already created PENDING — worst case it falls
    back to the manual operator queue)."""
    try:
        from smartfood.services.dispatch_service import DispatchService
        DispatchService.auto_dispatch(bot_order_id)
    except Exception:
        logger.exception('auto-dispatch failed (bot_order=%s)', bot_order_id)


def _instore_orders_for(sf_customer, limit=30):
    """In-store base.Orders for the unified client linked to this Telegram account
    (by telegram_id). Excludes dispatched bot orders (bot_order link) — those are
    already in the bot list — and cancelled orders. Returns [] if no link/orders."""
    tid = getattr(sf_customer, 'telegram_id', None)
    if not tid:
        return []
    from base.models import Customer as BaseCustomer, Order as BaseOrder
    base_client = (BaseCustomer.objects.filter(is_deleted=False, telegram_id=tid)
                   .order_by('id').first())
    if not base_client:
        return []
    orders = (BaseOrder.objects
              .filter(customer=base_client, is_deleted=False, bot_order__isnull=True)
              .exclude(status='CANCELED')
              .prefetch_related('items__product')
              .order_by('-created_at')[:limit])
    return [instore_order_dict(o) for o in orders]


class BotOrderService:
    @staticmethod
    @transaction.atomic
    def create(customer, items, order_type='DELIVERY', address_id=None, phone='',
               note='', tip=0, points_used=0, payment_method='CASH', lang='uz'):
        order_type = 'PICKUP' if str(order_type).upper() == 'PICKUP' else 'DELIVERY'
        # Lock the customer row so loyalty redemption can't over-redeem under
        # concurrent order creation — the clamp in price_cart must read a fresh,
        # locked balance, and the reserve below happens within the same lock.
        customer = Customer.objects.select_for_update().get(id=customer.id)
        try:
            priced = price_cart(items, order_type, tip, points_used, customer, lang)
        except CartError as e:
            return {'success': False, 'code': e.code, 'message': e.message}, e.http

        address = None
        address_text = ''
        if order_type == 'DELIVERY':
            if not address_id:
                return ServiceResponse.validation_error({'address_id': 'required'},
                                                        'A delivery address is required')
            address = Address.objects.filter(id=address_id, customer=customer).first()
            if not address:
                return ServiceResponse.not_found('Address not found')
            address_text = address.line

        payment_method = 'CARD' if str(payment_method).upper() == 'CARD' else 'CASH'

        order = BotOrder.objects.create(
            customer=customer, status=BotOrder.Status.PENDING, order_type=order_type,
            address=address, address_text=address_text,
            phone_number=(phone or customer.phone_number or ''), note=note or '',
            subtotal=priced['subtotal'], delivery_fee=priced['delivery_fee'],
            discount=priced['discount'], tip=priced['tip'], total=priced['total'],
            loyalty_points_used=priced['points_used'],
            loyalty_points_earned=priced['points_earned'],
            payment_method=payment_method,
        )
        BotOrderItem.objects.bulk_create([
            BotOrderItem(
                bot_order=order, product=ln['product'], size=ln['size'],
                quantity=ln['quantity'], unit_price=ln['unit_price'],
                line_total=ln['line_total'], toppings_snapshot=ln['toppings_snapshot'],
                detail=ln['detail'],
            ) for ln in priced['lines']
        ])

        # Reserve redeemed loyalty points now (refunded if the order is rejected).
        # Routed through the ledger so the balance + LoyaltyTransaction history stay
        # in lock-step (see LoyaltyService.record).
        if priced['points_used']:
            from smartfood.models import LoyaltyTransaction
            from smartfood.services.loyalty_service import LoyaltyService
            LoyaltyService.record(
                customer.id, LoyaltyTransaction.Kind.SPEND_ORDER, -priced['points_used'],
                reason=f'Redeemed on order {order.code}', bot_order=order)

        order = BotOrder.objects.prefetch_related('items').select_related('pos_order').get(id=order.id)
        # Phase 3: auto-dispatch to the active cashier on a connected POS the
        # moment the order lands (or reject if none online). Gated by a setting so
        # the manual operator queue can be restored. Runs after commit so the
        # order row is durable before dispatch reads/locks it.
        if getattr(settings, 'SMARTFOOD_AUTO_DISPATCH', True):
            _oid = order.id
            transaction.on_commit(lambda: _auto_dispatch_safe(_oid))
        return ServiceResponse.created(data=bot_order_dict(order))

    @staticmethod
    def list_for(customer, status=None):
        qs = (BotOrder.objects.filter(customer=customer)
              .prefetch_related('items').select_related('pos_order').order_by('-id'))
        if status == 'active':
            qs = qs.filter(status__in=[BotOrder.Status.PENDING, BotOrder.Status.DISPATCHED])
        elif status == 'history':
            qs = qs.filter(status__in=[BotOrder.Status.REJECTED, BotOrder.Status.CANCELED])
        data = {'items': [bot_order_dict(o) for o in qs]}
        # In-store orders made OUTSIDE the bot, surfaced for the phone-matched
        # unified client. Skipped on the 'active' tab (that's in-flight bot orders).
        if status != 'active':
            data['in_store'] = _instore_orders_for(customer)
        return ServiceResponse.success(data=data)

    @staticmethod
    def get_for(customer, order_id):
        order = (BotOrder.objects.filter(id=order_id, customer=customer)
                 .prefetch_related('items').select_related('pos_order').first())
        if not order:
            return ServiceResponse.not_found('Order not found')
        return ServiceResponse.success(data=bot_order_dict(order))

    @staticmethod
    @transaction.atomic
    def cancel(customer, order_id):
        order = BotOrder.objects.select_for_update().filter(id=order_id, customer=customer).first()
        if not order:
            return ServiceResponse.not_found('Order not found')
        if order.status != BotOrder.Status.PENDING:
            return {'success': False, 'code': 'cannot_cancel',
                    'message': 'Only pending orders can be canceled'}, 409
        if order.loyalty_points_used:
            from smartfood.models import LoyaltyTransaction
            from smartfood.services.loyalty_service import LoyaltyService
            LoyaltyService.record(
                customer.id, LoyaltyTransaction.Kind.REFUND, order.loyalty_points_used,
                reason=f'Refund canceled order {order.code}', bot_order=order)
        order.status = BotOrder.Status.CANCELED
        order.save(update_fields=['status', 'updated_at'])
        # Push the cancellation to the customer's Mini App over WS after commit.
        from smartfood.realtime import publish_bot_order_event
        _oid = order.id
        transaction.on_commit(lambda: publish_bot_order_event(_oid, 'canceled'))
        return ServiceResponse.success(data={'id': order.id, 'status': order.status})
