"""Operator (manager-auth) Smart Food console — mounted at /api/admins/smartfood/.

Bot on/off + dispatch + reject + the pending queue + active cashiers
(admin_bot_views), and catalog publishing / stop-selling / sizes+toppings
management (admin_catalog_views).
"""
from smartfood.views import (
    admin_analytics_views,
    admin_bot_views,
    admin_broadcast_views,
    admin_catalog_views,
    admin_customer_views,
    admin_loyalty_views,
    admin_marketing_views,
)

urlpatterns = (admin_analytics_views.urlpatterns
               + admin_bot_views.urlpatterns
               + admin_broadcast_views.urlpatterns
               + admin_catalog_views.urlpatterns
               + admin_customer_views.urlpatterns
               + admin_loyalty_views.urlpatterns
               + admin_marketing_views.urlpatterns)
