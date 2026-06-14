"""Customer loyalty summary — balance, earn rate, and per-order point history.

Points are credited on dispatch and reserved/refunded around redemption (see
order_service / dispatch_service); this service is read-only — it reports the
live balance plus the earn rate from BotConfig and a history row per BotOrder.
"""
from base.helpers.response import ServiceResponse
from smartfood.models import BotConfig, BotOrder
from smartfood.serializers import uzs


class LoyaltyService:
    @staticmethod
    def get(customer):
        cfg = BotConfig.load()
        orders = BotOrder.objects.filter(customer=customer).order_by('-id')
        history = [{
            'code': o.code,
            'points_earned': o.loyalty_points_earned,
            'points_used': o.loyalty_points_used,
            'created_at': o.created_at.isoformat() if o.created_at else None,
        } for o in orders]
        return ServiceResponse.success(data={
            'points': customer.loyalty_points,
            'earn_rate': {
                'points_per_uzs': uzs(cfg.loyalty_earn_per),
                'point_value_uzs': uzs(cfg.loyalty_point_value),
            },
            'history': history,
        })
