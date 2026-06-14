"""Load + flip the BotConfig singleton (the runtime ON/OFF + delivery params)."""
from smartfood.models import BotConfig
from smartfood.serializers import config_dict
from base.helpers.response import ServiceResponse

# Fields an operator may set via POST /api/admins/smartfood/config.
_EDITABLE = (
    'enabled', 'currency', 'delivery_fee', 'free_delivery_threshold',
    'min_order_amount', 'default_tip_options', 'service_area', 'default_lang',
    'loyalty_earn_per', 'loyalty_point_value',
    'support_phone', 'support_telegram', 'support_email', 'support_chat_id',
)


class BotConfigService:
    @staticmethod
    def get():
        return ServiceResponse.success(data=config_dict(BotConfig.load()))

    @staticmethod
    def update(values):
        cfg = BotConfig.load()
        for key in _EDITABLE:
            if key in values and values[key] is not None:
                setattr(cfg, key, values[key])
        cfg.save()
        return ServiceResponse.success(data=config_dict(BotConfig.load()), message='Config updated')

    @staticmethod
    def set_enabled(flag):
        cfg = BotConfig.load()
        cfg.enabled = bool(flag)
        cfg.save()
        return ServiceResponse.success(data={'enabled': cfg.enabled})
