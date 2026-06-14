"""Cart quote — server-side repricing of a client cart (never trusts prices)."""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.urls import path

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from smartfood.security import customer_required
from smartfood.gating import require_open
from smartfood.services.cart_service import CartService


@csrf_exempt
@require_POST
@require_open
@customer_required
def cart_quote(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = CartService.quote(
        data.get('items'),
        data.get('order_type', 'DELIVERY'),
        data.get('tip', 0),
        data.get('points_used', 0),
        request.customer,
        request.customer.language,
    )
    return JsonResponse(result, status=status)


urlpatterns = [
    path('cart/quote', cart_quote, name='cart-quote'),
]
