"""Customer loyalty endpoint (mounted under api/smartfood/)."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.urls import path

from smartfood.security import customer_required
from smartfood.services.loyalty_service import LoyaltyService


@require_GET
@customer_required
def loyalty(request):
    result, status = LoyaltyService.get(request.customer)
    return JsonResponse(result, status=status)


urlpatterns = [
    path('loyalty', loyalty),
]
