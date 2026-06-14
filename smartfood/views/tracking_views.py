"""Order tracking — the order detail already carries pos_order status/uuid."""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.urls import path

from smartfood.security import customer_required
from smartfood.services.order_service import BotOrderService


@csrf_exempt
@require_GET
@customer_required
def track_order(request, order_id):
    result, status = BotOrderService.get_for(request.customer, order_id)
    return JsonResponse(result, status=status)


urlpatterns = [
    path('orders/<int:order_id>/track', track_order, name='order-track'),
]
