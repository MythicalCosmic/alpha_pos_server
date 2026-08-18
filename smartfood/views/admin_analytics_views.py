"""Manager-authenticated Telegram Mini App audience analytics."""
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from base.security.permissions import manager_required
from smartfood.services.analytics_service import BotAnalyticsService


@csrf_exempt
@require_GET
@manager_required
def overview(request):
    result, status = BotAnalyticsService.overview(request.GET.get('days'))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@manager_required
def visitors(request):
    result, status = BotAnalyticsService.visitors(
        q=request.GET.get('q', ''),
        converted=request.GET.get('converted', ''),
        page=request.GET.get('page', 1),
        per_page=request.GET.get('per_page', 20),
    )
    return JsonResponse(result, status=status)


urlpatterns = [
    path('analytics/overview', overview, name='smartfood-admin-analytics'),
    path('visitors', visitors, name='smartfood-admin-visitors'),
]
