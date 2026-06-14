"""Operator (manager-auth) Smart Food console — mounted at /api/admins/smartfood/.

Bot on/off + dispatch + reject + the pending queue + active cashiers
(admin_bot_views), and catalog publishing / stop-selling / sizes+toppings
management (admin_catalog_views).
"""
from smartfood.views import admin_bot_views, admin_catalog_views

urlpatterns = admin_bot_views.urlpatterns + admin_catalog_views.urlpatterns
