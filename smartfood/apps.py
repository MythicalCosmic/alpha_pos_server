from django.apps import AppConfig


class SmartfoodConfig(AppConfig):
    """Server-only customer Telegram Mini App delivery backend.

    Integrates with the existing POS: the menu is driven by base.Product /
    base.Category (published + stop-selling via a thin shadow layer), and a bot
    order is manually DISPATCHED to a specific on-duty cashier — which mints a
    real base.Order under that cashier (so it lands in THAT cashier's shift),
    reusing the existing order-create + realtime broadcast.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'smartfood'
    verbose_name = 'Smart Food (customer delivery)'
