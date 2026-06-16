"""Smart Club loyalty — the real program.

A single append-only ledger (LoyaltyTransaction) is the source of truth for the
history; Customer.loyalty_points is the cached running balance. Every point
change MUST go through LoyaltyService.record() so the balance and the ledger stay
in lock-step. Customers redeem points for Rewards (gifts), which mint a Redemption
with a unique code; staff scan a member's QR (SF-<telegram_id>) or a redemption
code to award points for an in-store purchase, hand over a gift, or grant a bonus.
"""
import secrets
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from base.helpers.response import ServiceResponse
from smartfood.models import (
    BotConfig, Customer, Reward, Redemption, LoyaltyTransaction,
)
from smartfood.serializers import (
    uzs, reward_dict, redemption_dict, loyalty_txn_dict, member_dict,
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
               reward=None, redemption=None, staff_id=None):
        """Apply a signed point delta and append a ledger row. Returns (txn, balance).
        Clamps the balance at 0 so a refund/earn never resurrects negative points."""
        cust = Customer.objects.select_for_update().get(id=customer_id)
        new_balance = max(0, cust.loyalty_points + int(points))
        cust.loyalty_points = new_balance
        cust.save(update_fields=['loyalty_points'])
        txn = LoyaltyTransaction.objects.create(
            customer=cust, kind=kind, points=int(points), balance_after=new_balance,
            reason=(reason or '')[:200], bot_order=bot_order, reward=reward,
            redemption=redemption, staff_id=staff_id,
        )
        return txn, new_balance

    # --------------------------------------------------------------------- #
    #  Customer-facing                                                        #
    # --------------------------------------------------------------------- #
    @staticmethod
    def get(customer):
        cfg = BotConfig.load()
        txns = list(customer.loyalty_txns.all()[:50])
        active = customer.redemptions.filter(status=Redemption.Status.ISSUED)
        return ServiceResponse.success(data={
            'points': customer.loyalty_points,
            'member_id': f'SF-{customer.telegram_id}',
            'earn_rate': {
                'points_per_uzs': uzs(cfg.loyalty_earn_per),
                'point_value_uzs': uzs(cfg.loyalty_point_value),
            },
            'history': [loyalty_txn_dict(t) for t in txns],
            'redemptions': [redemption_dict(r) for r in active],
        })

    @staticmethod
    def rewards(customer=None, lang='uz'):
        pts = customer.loyalty_points if customer else 0
        qs = Reward.objects.filter(is_active=True).order_by('sort_order', 'id')
        items = [reward_dict(r, lang, pts) for r in qs
                 if (r.stock is None or r.stock > 0)]
        return ServiceResponse.success(data={'points': pts, 'items': items})

    @staticmethod
    @transaction.atomic
    def redeem(customer, reward_id):
        reward = (Reward.objects.select_for_update()
                  .filter(id=reward_id, is_active=True).first())
        if not reward:
            return ServiceResponse.not_found('Gift not found')
        cust = Customer.objects.select_for_update().get(id=customer.id)
        if cust.loyalty_points < reward.points_cost:
            return ServiceResponse.error('Not enough points for this gift')
        if reward.stock is not None and reward.stock <= 0:
            return ServiceResponse.error('This gift is out of stock')
        if reward.per_customer_limit:
            used = (Redemption.objects
                    .filter(customer=cust, reward=reward)
                    .exclude(status=Redemption.Status.CANCELED).count())
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
        s = (member_id or '').strip().upper()
        if s.startswith('SF-'):
            s = s[3:]
        try:
            tid = int(s)
        except (TypeError, ValueError):
            return None
        return Customer.objects.filter(telegram_id=tid).first()

    @staticmethod
    def member(member_id):
        cust = LoyaltyService._resolve_member(member_id)
        if not cust:
            return ServiceResponse.not_found('Member not found')
        recent = list(cust.loyalty_txns.all()[:10])
        active = cust.redemptions.filter(status=Redemption.Status.ISSUED)
        return ServiceResponse.success(data={
            'member': member_dict(cust),
            'history': [loyalty_txn_dict(t) for t in recent],
            'redemptions': [redemption_dict(r) for r in active],
        })

    @staticmethod
    @transaction.atomic
    def award_scan(member_id, amount, staff_id=None):
        """Credit points for an in-store purchase (member scans their QR at the till)."""
        cust = LoyaltyService._resolve_member(member_id)
        if not cust:
            return ServiceResponse.not_found('Member not found')
        if cust.is_blocked:
            return ServiceResponse.error('This member is blocked')
        cfg = BotConfig.load()
        per = Decimal(str(cfg.loyalty_earn_per or 0))
        if per <= 0:
            return ServiceResponse.error('Loyalty earn rate is not configured')
        try:
            amt = Decimal(str(amount))
        except Exception:
            return ServiceResponse.error('Invalid amount')
        if amt <= 0:
            return ServiceResponse.error('Amount must be positive')
        pts = int(amt / per)
        if pts <= 0:
            return ServiceResponse.error('Purchase too small to earn a point')
        _, bal = LoyaltyService.record(
            cust.id, LoyaltyTransaction.Kind.EARN_SCAN, pts,
            reason=f'In-store purchase {int(amt)} {cfg.currency}', staff_id=staff_id,
        )
        return ServiceResponse.success(data={
            'awarded': pts, 'balance': bal, 'member': member_dict(cust),
        })

    @staticmethod
    @transaction.atomic
    def grant(member_id, points, reason='', staff_id=None):
        """Manually grant (or deduct) points — a bonus, a comped gift, a fix."""
        cust = LoyaltyService._resolve_member(member_id)
        if not cust:
            return ServiceResponse.not_found('Member not found')
        try:
            pts = int(points)
        except (TypeError, ValueError):
            return ServiceResponse.error('Points must be a whole number')
        if pts == 0:
            return ServiceResponse.error('Points must be non-zero')
        kind = (LoyaltyTransaction.Kind.GRANT if pts > 0
                else LoyaltyTransaction.Kind.ADJUST)
        _, bal = LoyaltyService.record(
            cust.id, kind, pts, reason=(reason or 'Manual grant'), staff_id=staff_id,
        )
        return ServiceResponse.success(data={'balance': bal, 'member': member_dict(cust)})

    @staticmethod
    @transaction.atomic
    def fulfill(code, staff_id=None):
        """Mark a redemption FULFILLED (staff scanned/typed the gift code)."""
        red = (Redemption.objects.select_for_update()
               .filter(code=(code or '').strip().upper()).first())
        if not red:
            return ServiceResponse.not_found('Redemption code not found')
        if red.status != Redemption.Status.ISSUED:
            return ServiceResponse.error(f'Already {red.get_status_display().lower()}')
        red.status = Redemption.Status.FULFILLED
        red.fulfilled_at = timezone.now()
        red.fulfilled_by_id = staff_id
        red.save(update_fields=['status', 'fulfilled_at', 'fulfilled_by'])
        return ServiceResponse.success(data={'redemption': redemption_dict(red)})
