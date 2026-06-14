"""Customer Telegram Mini App routes — mounted at /api/smartfood/.

Each view module owns its own urlpatterns; this just aggregates them.
"""
from smartfood.views import (
    auth_views,
    config_views,
    catalog_views,
    cart_views,
    order_views,
    tracking_views,
    address_views,
    loyalty_views,
    support_views,
)

urlpatterns = (
    auth_views.urlpatterns
    + config_views.urlpatterns
    + catalog_views.urlpatterns
    + cart_views.urlpatterns
    + order_views.urlpatterns
    + tracking_views.urlpatterns
    + address_views.urlpatterns
    + loyalty_views.urlpatterns
    + support_views.urlpatterns
)
