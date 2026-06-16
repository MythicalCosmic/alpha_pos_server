"""Customer loyalty + rewards endpoints (mounted under api/smartfood/).

GET  /loyalty                 balance, earn rate, member id, history, redemptions
GET  /rewards                 the gift catalog (with affordable flags)
POST /rewards/<id>/redeem     spend points -> mint a redemption code
GET  /redemptions             this customer's redemptions
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.urls import path

from smartfood.security import customer_required
from smartfood.services.loyalty_service import LoyaltyService


def _lang(request):
    return request.GET.get('lang') or getattr(request.customer, 'language', 'uz') or 'uz'


@require_GET
@customer_required
def loyalty(request):
    result, status = LoyaltyService.get(request.customer)
    return JsonResponse(result, status=status)


@require_GET
@customer_required
def rewards(request):
    result, status = LoyaltyService.rewards(request.customer, lang=_lang(request))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@customer_required
def redeem(request, reward_id):
    result, status = LoyaltyService.redeem(request.customer, reward_id)
    return JsonResponse(result, status=status)


@require_GET
@customer_required
def redemptions(request):
    result, status = LoyaltyService.redemptions(request.customer)
    return JsonResponse(result, status=status)


urlpatterns = [
    path('loyalty', loyalty),
    path('rewards', rewards),
    path('rewards/<int:reward_id>/redeem', redeem),
    path('redemptions', redemptions),
]
