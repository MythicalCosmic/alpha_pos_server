"""Server edition URLconf — the cloud/back-office surface.

The customer Telegram/Smartfood APIs and authenticated Admin order-management
routes are intentionally mounted here. Local POS-only ``customers.urls`` and
waiter routes remain exclusive to the desktop edition.
"""
import os

from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, include

from admins.views.legacy_dashboard_views import legacy_dashboard
from base.services.sync.views import get_sync_urls
from notifications.views import customer_bot_views


def healthz(_request):
    sha = os.environ.get('APP_GIT_SHA', 'unknown')
    return HttpResponse(f'ok {sha}', content_type='text/plain')


urlpatterns = [
    path('admin/', admin.site.urls),
    path('legacy-dashboard/', legacy_dashboard, name='legacy_dashboard'),
    path('healthz', healthz),
    path('api/admins/', include('admins.urls')),
    path('api/admins/stock/', include('stock.urls')),
    path('api/admins/hr/', include('hr.urls')),
    path('api/admins/discounts/', include('discounts.urls')),
    path('api/admins/notifications/', include('notifications.urls')),
    path('api/admins/cashbox/', include('cashbox.urls')),
    path('api/sync/', include(get_sync_urls())),
    path('api/licensing/', include('licensing.urls')),
    path('api/fiscalization/', include('fiscalization.urls')),
    # Customer-facing Telegram bot (separate token) — greet + open the web app.
    path('api/customer-bot/webhook/', customer_bot_views.customer_webhook,
         name='customer-bot-webhook'),
    # Smart Food customer delivery: Mini App API (customer-auth) + operator console.
    path('api/smartfood/', include('smartfood.urls')),
    path('api/admins/smartfood/', include('smartfood.admin_urls')),
    # Courier delivery: back-office assignment endpoints…
    path('api/admins/couriers/', include('couriers.admin_urls')),
    # …and the rider app's own paths at root (/auth/courier/login/, /courier/…,
    # /orders/<id>/accept|decline|status/) exactly as the mobile app calls them.
    path('', include('couriers.urls')),
]
