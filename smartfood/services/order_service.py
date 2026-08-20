"""Create + read customer BotOrders (created PENDING; dispatched later).

Money is recomputed server-side via cart_service; redeemed loyalty points are
reserved at create and refunded on reject (in dispatch_service).
"""
import hashlib
import json

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from base.helpers.response import ServiceResponse
from base.services.phone import is_canonical_uz_phone, normalize_uz_phone
from smartfood.models import Address, BotOrder, BotOrderItem, Customer
from smartfood.serializers import bot_order_dict, instore_order_dict
from smartfood.services.cart_service import price_cart, CartError
from smartfood.services.order_input import (
    OrderInputError,
    error_response,
    normalize_address_id,
    normalize_cart_items,
    normalize_client_order_id,
    normalize_note,
    normalize_order_type,
    normalize_payment_method,
    normalize_points,
    normalize_tip,
)

def _request_fingerprint(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


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
               note='', tip=0, points_used=0, payment_method='CASH', lang='uz',
               client_order_id=None):
        try:
            client_order_id = normalize_client_order_id(client_order_id)
            order_type = normalize_order_type(order_type)
            payment_method = normalize_payment_method(payment_method)
            address_id = normalize_address_id(
                address_id,
                required=(order_type == 'DELIVERY'),
            )
            items = normalize_cart_items(items)
            tip = normalize_tip(tip)
            points_used = normalize_points(points_used)
            note = normalize_note(note)
        except OrderInputError as exc:
            return error_response(exc)

        # Lock the customer row so loyalty redemption can't over-redeem under
        # concurrent order creation — the clamp in price_cart must read a fresh,
        # locked balance, and the reserve below happens within the same lock.
        customer = Customer.objects.select_for_update().get(id=customer.id)
        if phone not in (None, ''):
            if isinstance(phone, bool):
                return ServiceResponse.validation_error({
                    'phone': 'Enter the confirmed account phone number.',
                })
            submitted_phone = normalize_uz_phone(phone)
            if submitted_phone != normalize_uz_phone(customer.phone_number):
                return ({
                    'success': False,
                    'code': 'profile_phone_mismatch',
                    'message': 'Use the phone number confirmed on your account.',
                    'errors': {'phone': 'Phone does not match the confirmed account.'},
                }, 422)
        phone = normalize_uz_phone(customer.phone_number)

        fingerprint = _request_fingerprint({
            'items': items,
            'order_type': order_type,
            'address_id': address_id,
            'phone': phone,
            'note': note,
            'tip': str(tip),
            'points_used': points_used,
            'payment_method': payment_method,
        })
        existing = (BotOrder.objects.filter(
            customer=customer,
            client_order_id=client_order_id,
        ).prefetch_related('items').select_related('pos_order').first())
        if existing:
            if existing.request_fingerprint != fingerprint:
                return {
                    'success': False,
                    'code': 'idempotency_conflict',
                    'message': (
                        'client_order_id was already used with a different '
                        'order payload'
                    ),
                }, 409
            if (
                existing.status == BotOrder.Status.PENDING
                and getattr(settings, 'SMARTFOOD_AUTO_DISPATCH', True)
            ):
                from smartfood.models import BotOrderDispatchJob
                BotOrderDispatchJob.objects.get_or_create(
                    bot_order=existing,
                )
            return ServiceResponse.success(
                data=bot_order_dict(existing),
                message='Order already created',
            )

        if not customer.profile_complete or not is_canonical_uz_phone(phone):
            return ({
                'success': False,
                'code': 'profile_required',
                'message': 'Confirm your name and phone number before ordering.',
                'errors': {
                    field: 'required'
                    for field in customer.profile_missing
                },
            }, 422)

        # Only a genuinely new request needs current store availability.
        # Idempotency replay/conflict above must remain recoverable after a till
        # disconnect or an operator temporarily closes the bot; otherwise a
        # client retry cannot discover whether its first request was accepted.
        from smartfood.gating import bot_open
        is_open, reason = bot_open()
        if not is_open:
            return {
                'success': False,
                'closed': True,
                'reason': reason,
            }, 200
        from base.services.presence import resolve_active_cashier
        if resolve_active_cashier() is None:
            return {
                'success': False,
                'closed': True,
                'reason': 'no_cashier',
            }, 200

        try:
            priced = price_cart(items, order_type, tip, points_used, customer, lang)
        except CartError as e:
            return {'success': False, 'code': e.code, 'message': e.message}, e.http
        except OrderInputError as exc:
            return error_response(exc)

        address = None
        address_text = ''
        address_lat = None
        address_lng = None
        if order_type == 'DELIVERY':
            address = Address.objects.filter(id=address_id, customer=customer).first()
            if not address:
                return ServiceResponse.not_found('Address not found')
            if address.lat is None or address.lng is None:
                return ({
                    'success': False,
                    'code': 'location_required',
                    'message': 'Pin the delivery location on the map before ordering.',
                    'errors': {'address_id': 'A precise location is required.'},
                }, 422)
            address_text = address.line
            address_lat = address.lat
            address_lng = address.lng

        order = BotOrder.objects.create(
            customer=customer,
            client_order_id=client_order_id,
            request_fingerprint=fingerprint,
            status=BotOrder.Status.PENDING,
            order_type=order_type,
            address=address, address_text=address_text,
            address_lat=address_lat, address_lng=address_lng,
            phone_number=phone, note=note,
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
        if getattr(settings, 'SMARTFOOD_AUTO_DISPATCH', True):
            from smartfood.models import BotOrderDispatchJob
            BotOrderDispatchJob.objects.create(bot_order=order)
        from smartfood.services.notification_service import queue_order_status
        queue_order_status(order.id, 'placed')
        return ServiceResponse.created(data=bot_order_dict(order))

    @staticmethod
    def list_for(customer, status=None):
        qs = (BotOrder.objects.filter(customer=customer)
              .prefetch_related('items').select_related('pos_order').order_by('-id'))
        if status == 'active':
            qs = qs.filter(
                Q(status=BotOrder.Status.PENDING)
                | Q(status=BotOrder.Status.DISPATCHED)
                & ~Q(pos_order__status__in=['COMPLETED', 'CANCELED'])
            )
        elif status == 'history':
            qs = qs.filter(
                Q(status__in=[BotOrder.Status.REJECTED, BotOrder.Status.CANCELED])
                | Q(
                    status=BotOrder.Status.DISPATCHED,
                    pos_order__status__in=['COMPLETED', 'CANCELED'],
                )
            )
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
        order.status = BotOrder.Status.CANCELED
        order.save(update_fields=['status', 'updated_at'])
        from smartfood.services.loyalty_settlement_service import (
            reconcile_bot_order_loyalty,
        )
        reconcile_bot_order_loyalty(order.id)
        # Push the cancellation to the customer's Mini App over WS after commit.
        from smartfood.realtime import publish_bot_order_event
        from smartfood.services.notification_service import queue_order_status
        _oid = order.id
        transaction.on_commit(lambda: publish_bot_order_event(_oid, 'canceled'))
        queue_order_status(_oid, 'canceled')
        return ServiceResponse.success(data={'id': order.id, 'status': order.status})
