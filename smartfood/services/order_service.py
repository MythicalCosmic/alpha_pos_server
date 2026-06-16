"""Create + read customer BotOrders (created PENDING; dispatched later).

Money is recomputed server-side via cart_service; redeemed loyalty points are
reserved at create and refunded on reject (in dispatch_service).
"""
from django.db import transaction
from django.db.models import F

from base.helpers.response import ServiceResponse
from smartfood.models import Address, BotOrder, BotOrderItem, Customer
from smartfood.serializers import bot_order_dict
from smartfood.services.cart_service import price_cart, CartError


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
        return ServiceResponse.created(data=bot_order_dict(order))

    @staticmethod
    def list_for(customer, status=None):
        qs = (BotOrder.objects.filter(customer=customer)
              .prefetch_related('items').select_related('pos_order').order_by('-id'))
        if status == 'active':
            qs = qs.filter(status__in=[BotOrder.Status.PENDING, BotOrder.Status.DISPATCHED])
        elif status == 'history':
            qs = qs.filter(status__in=[BotOrder.Status.REJECTED, BotOrder.Status.CANCELED])
        return ServiceResponse.success(data={'items': [bot_order_dict(o) for o in qs]})

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
        return ServiceResponse.success(data={'id': order.id, 'status': order.status})
