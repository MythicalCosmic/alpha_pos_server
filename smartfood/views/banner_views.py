"""Customer-facing scheduled home banners."""
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.http import require_GET

from smartfood.security import customer_required
from smartfood.services.marketing_service import BannerService


@require_GET
@customer_required
def banners(request):
    language = (
        request.GET.get('lang')
        or getattr(request.customer, 'language', 'uz')
        or 'uz'
    )
    result, status = BannerService.public(language)
    return JsonResponse(result, status=status)


urlpatterns = [path('banners', banners)]
