"""Smart Club loyalty — the real program.

A single append-only ledger (LoyaltyTransaction) is the source of truth for the
history; Customer.loyalty_points is the cached running balance. Every point
change MUST go through LoyaltyService.record() so the balance and the ledger stay
in lock-step. Customers redeem points for Rewards (gifts), which mint a Redemption
with a unique code; staff scan a member's QR (SF-<telegram_id>) or a redemption
code to award points for an in-store purchase, hand over a gift, or grant a bonus.
"""
import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from base.helpers.response import ServiceResponse
from smartfood.models import (
    BotConfig, Customer, Reward, Redemption, LoyaltyTransaction,
)
from smartfood.serializers import (
    decimal_number, reward_dict, redemption_dict, loyalty_txn_dict, member_dict,
)
from smartfood.services.loyalty_input import whole_number, reason_text, input_error, MAX_POINTS
from smartfood.services.order_input import OrderInputError
from smartfood.services.catalog_service import (
    customer_visible_product_rows,
    is_product_customer_visible,
)

# Unambiguous alphabet (no O/0/I/1) so a code read off a screen is easy to type.
_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def _gen_code():
    return 'GIFT-' + ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


class LoyaltyService:

    # --------------------------------------------------------------------- #
    #  Ledger primitive — the ONLY place Customer.loyalty_points is mutated.  #
    # --------------------------------------------------------------------- #
    @staticmethod
    @transaction.atomic
    def record(customer_id, kind, points, reason='', bot_order=None,
               reward=None, redemption=None, staff_id=None, pos_order=None):
        """Apply the exact signed delta. Reversed earned points can create debt."""
        points = whole_number(points, 'points')
        cust = Customer.objects.select_for_update().get(id=customer_id)
        new_balance = cust.loyalty_points + points
        if not -MAX_POINTS <= new_balance <= MAX_POINTS:
            raise OrderInputError('LOYALTY_BALANCE_OVERFLOW', 'The point balance exceeds the supported limit.')
        if kind in (LoyaltyTransaction.Kind.REDEEM, LoyaltyTransaction.Kind.SPEND_ORDER) and new_balance < 0:
            raise OrderInputError('INSUFFICIENT_POINTS', 'Not enough available points.')
        cust.loyalty_points = new_balance
        cust.save(update_fields=['loyalty_points'])
        txn = LoyaltyTransaction.objects.create(
            customer=cust, kind=kind, points=points, balance_after=new_balance,
            reason=(reason or '')[:200], bot_order=bot_order, reward=reward,
            redemption=redemption, staff_id=staff_id, pos_order=pos_order,
        )
        return txn, new_balance

    # --------------------------------------------------------------------- #
    #  Customer-facing                                                        #
    # --------------------------------------------------------------------- #
    @staticmethod
    def get(customer):
        cfg = BotConfig.load()
        customer.refresh_from_db(fields=['loyalty_points'])
        txns = list(customer.loyalty_txns.select_related('bot_order', 'redemption')[:50])
        active = customer.redemptions.filter(status=Redemption.Status.ISSUED)
        return ServiceResponse.success(data={
            'points': customer.loyalty_points,
            'available_points': max(0, customer.loyalty_points),
            'member_id': f'SF-{customer.telegram_id}',
            'earn_rate': {
                'points_per_uzs': decimal_number(cfg.loyalty_earn_per),  # compatibility alias
                'uzs_per_point': decimal_number(cfg.loyalty_earn_per),
                'point_value_uzs': decimal_number(cfg.loyalty_point_value),
                'earning_basis': cfg.loyalty_earning_basis,
            },
            'history': [loyalty_txn_dict(t) for t in txns],
            'redemptions': [redemption_dict(r) for r in active],
        })

    @staticmethod
    def rewards(customer=None, lang='uz'):
        pts = customer.loyalty_points if customer else 0
        # A zero-cost reward can mint unlimited redemption codes without a
        # ledger debit.  Keep malformed legacy rows off the public catalog even
        # if they were activated outside the operator service.
        visible_product_ids = customer_visible_product_rows().order_by().values('product_id')
        cfg = BotConfig.load()
        qs = Reward.objects.filter(
            is_active=True,
            points_cost__gt=0,
        ).filter(
            Q(kind=Reward.Kind.FREE_PRODUCT, product_id__in=visible_product_ids)
            | Q(kind=Reward.Kind.DISCOUNT, discount_amount__gt=0)
            | Q(kind=Reward.Kind.CUSTOM)
            | (Q(kind=Reward.Kind.FREE_DELIVERY) if cfg.delivery_fee > 0 else Q(pk__in=[]))
        ).order_by('sort_order', 'id')
        redemption_counts = {}
        if customer:
            redemption_counts = {
                row['reward_id']: row['total']
                for row in (
                    Redemption.objects.filter(customer=customer)
                    .exclude(status__in=[Redemption.Status.CANCELED, Redemption.Status.EXPIRED])
                    .values('reward_id')
                    .annotate(total=Count('id'))
                )
            }
        items = []
        for reward in qs:
            if reward.stock is not None and reward.stock <= 0:
                continue
            limit_reached = bool(
                reward.per_customer_limit
                and redemption_counts.get(reward.id, 0) >= reward.per_customer_limit
            )
            items.append(reward_dict(
                reward,
                lang,
                pts,
                limit_reached=limit_reached,
            ))
        return ServiceResponse.success(data={'points': pts, 'items': items})

    @staticmethod
    @transaction.atomic
    def redeem(customer, reward_id):
        # All reward writes use customer -> reward -> redemption lock order.
        cust = Customer.objects.select_for_update().get(id=customer.id)
        if cust.is_blocked:
            return ServiceResponse.forbidden('This member is blocked')
        reward = (Reward.objects.select_for_update()
                  .filter(id=reward_id, is_active=True).first())
        if not reward:
            return ServiceResponse.not_found('Gift not found')
        if reward.points_cost <= 0:
            return ServiceResponse.error('This gift is not configured correctly')
        if (
            reward.kind == Reward.Kind.FREE_PRODUCT
            and not is_product_customer_visible(reward.product_id)
        ):
            return ServiceResponse.error('This gift is not currently available')
        if reward.kind == Reward.Kind.DISCOUNT and reward.discount_amount <= 0:
            return ServiceResponse.error('This gift is not configured correctly')
        cfg = BotConfig.load()
        if reward.kind == Reward.Kind.FREE_DELIVERY and cfg.delivery_fee <= 0:
            return ServiceResponse.error('Delivery is already free; this gift is unavailable')
        if cust.loyalty_points < reward.points_cost:
            return ServiceResponse.error('Not enough points for this gift')
        if reward.stock is not None and reward.stock <= 0:
            return ServiceResponse.error('This gift is out of stock')
        if reward.per_customer_limit:
            used = (Redemption.objects
                    .filter(customer=cust, reward=reward)
                    .exclude(status__in=[Redemption.Status.CANCELED, Redemption.Status.EXPIRED]).count())
            if used >= reward.per_customer_limit:
                return ServiceResponse.error('You have reached the limit for this gift')

        if reward.stock is not None:
            reward.stock -= 1
            reward.save(update_fields=['stock'])

        code = _gen_code()
        while Redemption.objects.filter(code=code).exists():
            code = _gen_code()
        redemption = Redemption.objects.create(
            customer=cust, reward=reward, code=code,
            points_spent=reward.points_cost,
            reward_name=(reward.name_uz or reward.name_en or 'Gift'),
            kind=reward.kind,
            stock_reserved=reward.stock is not None,
            expires_at=timezone.now() + timedelta(days=cfg.reward_valid_days),
            reward_snapshot={
                'version': 1, 'kind': reward.kind, 'product_id': reward.product_id,
                'discount_amount_uzs': str(reward.discount_amount),
                'delivery_fee_uzs': str(cfg.delivery_fee),
                'names': {lang: getattr(reward, f'name_{lang}') for lang in ('uz', 'ru', 'en')},
                'descriptions': {lang: getattr(reward, f'desc_{lang}') for lang in ('uz', 'ru', 'en')},
            },
            action_history=[{'action': 'ISSUED', 'customer_id': cust.id,
                             'at': timezone.now().isoformat()}],
        )
        LoyaltyService.record(
            cust.id, LoyaltyTransaction.Kind.REDEEM, -reward.points_cost,
            reason=f'Redeemed {redemption.reward_name}',
            reward=reward, redemption=redemption,
        )
        return ServiceResponse.created(data={'redemption': redemption_dict(redemption)})

    @staticmethod
    def redemptions(customer):
        qs = customer.redemptions.all()[:50]
        return ServiceResponse.success(data={'items': [redemption_dict(r) for r in qs]})

    # --------------------------------------------------------------------- #
    #  Staff-facing (manager auth) — scan member QR / fulfil / grant          #
    # --------------------------------------------------------------------- #
    @staticmethod
    def _resolve_member(member_id):
        if not isinstance(member_id, str) or len(member_id) > 32:
            return None
        s = member_id.strip().upper()
        if s.startswith('SF-'):
            s = s[3:]
        try:
            tid = int(s)
        except (TypeError, ValueError):
            return None
        if not 0 < tid <= 9_223_372_036_854_775_807:
            return None
        return Customer.objects.filter(telegram_id=tid).first()

    @staticmethod
    def member(member_id):
        cust = LoyaltyService._resolve_member(member_id)
        if not cust:
            return ServiceResponse.not_found('Member not found')
        recent = list(cust.loyalty_txns.select_related('bot_order', 'redemption')[:10])
        active = cust.redemptions.filter(status=Redemption.Status.ISSUED)
        return ServiceResponse.success(data={
            'member': member_dict(cust),
            'history': [loyalty_txn_dict(t) for t in recent],
            'redemptions': [redemption_dict(r) for r in active],
        })

    @staticmethod
    @transaction.atomic
    def award_scan(member_id, amount=None, staff_id=None, *, order_id=None):
        """Award a verified POS receipt once; client amounts never prove a sale."""
        from base.models import Order, User
        from base.services.branch_scope import resolve_actor_branch
        from base.services.phone import normalize_uz_phone
        from base.services.tender import tender_integrity_issues
        from notifications.models import OrderLoyaltyCredit

        try:
            order_id = whole_number(order_id, 'order_id', minimum=1,
                                    maximum=9_223_372_036_854_775_807)
        except OrderInputError as error:
            return input_error(error)
        staff = User.objects.filter(pk=staff_id, status='ACTIVE', is_deleted=False).first()
        if staff is None:
            return ServiceResponse.forbidden('An active operator is required')
        order = Order.objects.select_for_update().filter(
            pk=order_id, is_deleted=False, branch_id=resolve_actor_branch(staff),
        ).first()
        if order is None:
            return ServiceResponse.not_found('Receipt not found in your branch')
        cust = LoyaltyService._resolve_member(member_id)
        if cust is None:
            return ServiceResponse.not_found('Member not found')
        cust = Customer.objects.select_for_update().get(pk=cust.pk)
        if cust.is_blocked:
            return ServiceResponse.forbidden('This member is blocked')
        phone = normalize_uz_phone(cust.phone_number)
        receipt_phone = normalize_uz_phone(order.phone_number or (
            order.customer.phone_number if order.customer_id else ''))
        if not phone or phone != receipt_phone:
            return ServiceResponse.forbidden('The receipt belongs to a different customer')
        if order.order_origin == Order.Origin.TELEGRAM or OrderLoyaltyCredit.objects.filter(order_id=order.pk).exists():
            return ServiceResponse.error('This receipt is handled by another loyalty flow')
        if (not order.is_paid or order.paid_at is None or order.status != Order.Status.COMPLETED
                or tender_integrity_issues(Order.objects.filter(pk=order.pk), require_concrete=True)
                or order.refunds.filter(is_deleted=False).exists()):
            return ServiceResponse.error('A completed, fully settled, unrefunded receipt is required')
        prior = LoyaltyTransaction.objects.filter(pos_order=order, kind=LoyaltyTransaction.Kind.EARN_SCAN).first()
        if prior:
            if prior.customer_id != cust.pk:
                return ServiceResponse.error('This receipt was already awarded to another member')
            return ServiceResponse.success(data={
                'awarded': prior.points, 'balance': prior.balance_after,
                'transaction_id': prior.pk, 'order_id': order.pk,
            })
        cfg = BotConfig.load()
        per = Decimal(cfg.loyalty_earn_per)
        if per <= 0:
            return ServiceResponse.error('Loyalty earn rate is not configured')
        # A POS scan has no Smart Food point-spend discount. Its actual
        # discounted merchandise total is the receipt-backed earning basis.
        basis = order.total_amount
        pts = int(basis / per)
        if pts <= 0:
            return ServiceResponse.error('Purchase too small to earn a point')
        txn, bal = LoyaltyService.record(
            cust.id, LoyaltyTransaction.Kind.EARN_SCAN, pts,
            reason=f'Verified receipt {order.pk}: {basis} {cfg.currency}',
            staff_id=staff_id, pos_order=order,
        )
        cust.loyalty_points = bal
        return ServiceResponse.success(data={
            'awarded': pts, 'balance': bal, 'member': member_dict(cust),
            'transaction_id': txn.pk, 'order_id': order.pk,
        })

    @staticmethod
    @transaction.atomic
    def grant(member_id, points, reason='', staff_id=None):
        """A reasoned, bounded ledger adjustment; negative debt remains explicit."""
        try:
            pts = whole_number(points, 'points')
            reason = reason_text(reason)
            if pts == 0:
                raise OrderInputError('INVALID_LOYALTY_INPUT', 'Points must be non-zero.')
        except OrderInputError as error:
            return input_error(error)
        cust = LoyaltyService._resolve_member(member_id)
        if not cust:
            return ServiceResponse.not_found('Member not found')
        kind = LoyaltyTransaction.Kind.GRANT if pts > 0 else LoyaltyTransaction.Kind.ADJUST
        try:
            txn, bal = LoyaltyService.record(cust.id, kind, pts, reason=reason, staff_id=staff_id)
        except OrderInputError as error:
            return input_error(error)
        cust.loyalty_points = bal
        return ServiceResponse.success(data={
            'balance': bal, 'member': member_dict(cust), 'transaction_id': txn.pk,
        })

    @staticmethod
    def _locked_redemption(code, customer_id=None):
        if not isinstance(code, str) or len(code) > 20:
            return None, None, None
        query = Redemption.objects.filter(code=code.strip().upper())
        if customer_id is not None:
            query = query.filter(customer_id=customer_id)
        identity = query.values('pk', 'customer_id', 'reward_id').first()
        if not identity:
            return None, None, None
        cust = Customer.objects.select_for_update().get(pk=identity['customer_id'])
        reward = Reward.objects.select_for_update().get(pk=identity['reward_id'])
        red = Redemption.objects.select_for_update().get(pk=identity['pk'])
        return cust, reward, red

    @staticmethod
    @transaction.atomic
    def fulfill(code, staff_id=None):
        """Repeated fulfillment cannot deliver or debit the gift a second time."""
        cust, _reward, red = LoyaltyService._locked_redemption(code)
        if not red:
            return ServiceResponse.not_found('Redemption code not found')
        if red.status == Redemption.Status.FULFILLED:
            return ServiceResponse.success(data={'redemption': redemption_dict(red)})
        if red.status != Redemption.Status.ISSUED:
            return ServiceResponse.error('This redemption is no longer active')
        if cust.is_blocked:
            return ServiceResponse.forbidden('This member is blocked')
        if red.expires_at and red.expires_at <= timezone.now():
            return ServiceResponse.error('This gift has expired; its points will be returned')
        red.status = Redemption.Status.FULFILLED
        red.fulfilled_at = timezone.now()
        red.fulfilled_by_id = staff_id
        red.action_history = [*red.action_history, {
            'action': red.status, 'staff_id': staff_id, 'at': red.fulfilled_at.isoformat(),
        }]
        red.save(update_fields=['status', 'fulfilled_at', 'fulfilled_by', 'action_history'])
        return ServiceResponse.success(data={'redemption': redemption_dict(red)})

    @staticmethod
    @transaction.atomic
    def cancel(code, reason, *, staff_id=None, customer_id=None, expired=False):
        try:
            reason = reason_text(reason)
        except OrderInputError as error:
            return input_error(error)
        cust, reward, red = LoyaltyService._locked_redemption(code, customer_id)
        if not red:
            return ServiceResponse.not_found('Redemption code not found')
        if red.status in (Redemption.Status.CANCELED, Redemption.Status.EXPIRED):
            return ServiceResponse.success(data={'redemption': redemption_dict(red)})
        if red.status != Redemption.Status.ISSUED:
            return ServiceResponse.error('A fulfilled gift cannot be canceled')
        now = timezone.now()
        if expired and (not red.expires_at or red.expires_at > now):
            return ServiceResponse.error('This gift has not expired')
        LoyaltyService.record(cust.pk, LoyaltyTransaction.Kind.REFUND, red.points_spent,
                              reason=reason, reward=reward, redemption=red, staff_id=staff_id)
        if red.stock_reserved and reward.stock is not None:
            reward.stock += 1
            reward.save(update_fields=['stock'])
        red.status = Redemption.Status.EXPIRED if expired else Redemption.Status.CANCELED
        red.canceled_at = now
        red.canceled_by_id = staff_id
        red.cancellation_reason = reason
        red.action_history = [*red.action_history, {
            'action': red.status, 'staff_id': staff_id, 'customer_id': customer_id,
            'reason': reason, 'at': now.isoformat(),
        }]
        red.save(update_fields=['status', 'canceled_at', 'canceled_by',
                                'cancellation_reason', 'action_history'])
        return ServiceResponse.success(data={'redemption': redemption_dict(red)})

    @staticmethod
    @transaction.atomic
    def reverse_refunded_scan(order_id):
        from base.models import Order
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if not order or not (order.is_deleted or order.status == 'CANCELED' or
                             order.refunds.filter(is_deleted=False).exists()):
            return False
        events = LoyaltyTransaction.objects.filter(pos_order_id=order_id)
        award = events.filter(kind=LoyaltyTransaction.Kind.EARN_SCAN).first()
        if not award or events.filter(kind=LoyaltyTransaction.Kind.REVERSE_SCAN).exists():
            return False
        LoyaltyService.record(
            award.customer_id, LoyaltyTransaction.Kind.REVERSE_SCAN, -award.points,
            reason=f'Reversed points for refunded receipt {order_id}', pos_order=order,
        )
        return True

    @staticmethod
    def reconcile_scans(limit=100):
        reversed_orders = LoyaltyTransaction.objects.filter(kind='REVERSE_SCAN').values('pos_order_id')
        order_ids = list(LoyaltyTransaction.objects.filter(kind='EARN_SCAN').filter(
            Q(pos_order__status='CANCELED') | Q(pos_order__is_deleted=True) |
            Q(pos_order__refund__is_deleted=False),
        ).exclude(pos_order_id__in=reversed_orders).order_by('pos_order_id').values_list(
            'pos_order_id', flat=True).distinct()[:limit])
        import logging
        completed = 0
        for order_id in order_ids:
            try:
                completed += LoyaltyService.reverse_refunded_scan(order_id)
            except Exception:
                logging.getLogger(__name__).exception('Receipt points reversal failed; retry required')
        return completed

    @staticmethod
    def expire_due(limit=100):
        codes = list(Redemption.objects.filter(
            status=Redemption.Status.ISSUED, expires_at__lte=timezone.now(),
        ).order_by('expires_at', 'pk').values_list('code', flat=True)[:limit])
        import logging
        completed = 0
        for code in codes:
            try:
                completed += LoyaltyService.cancel(
                    code, 'Reward expired; points returned', expired=True,
                )[1] < 400
            except Exception:
                logging.getLogger(__name__).exception('Reward expiry failed; retry required')
        return completed
