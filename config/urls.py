"""Server edition URLconf — the back-office surface.

No POS order-taking, no Telegram/QR self-order webhooks, no customers.urls.
Order-WRITE endpoints under admins are not mounted (read/analytics only).
"""
import os

from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, include

from base.services.sync.views import get_sync_urls


def healthz(_request):
    sha = os.environ.get('APP_GIT_SHA', 'unknown')
    return HttpResponse(f'ok {sha}', content_type='text/plain')


urlpatterns = [
    path('admin/', admin.site.urls),
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
]
