"""Customer-facing bot config (runtime ON/OFF + delivery params)."""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.urls import path

from smartfood.security import customer_required
from smartfood.services.config_service import BotConfigService


@csrf_exempt
@require_GET
@customer_required
def config(request):
    result, status = BotConfigService.get()
    return JsonResponse(result, status=status)


urlpatterns = [
    path('config', config, name='config'),
]
