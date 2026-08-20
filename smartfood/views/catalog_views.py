"""Customer catalog endpoints (read-only and available while ordering is closed).

Mounted under api/smartfood/. BotConfig.enabled gates quotes and order writes,
not browsing, so customers can inspect the menu before service resumes.
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.urls import path

from smartfood.security import customer_required
from smartfood.services.catalog_service import CatalogService


def _lang(request):
    return request.GET.get('lang') or request.customer.language or 'uz'


@csrf_exempt
@require_GET
@customer_required
def categories(request):
    result, status = CatalogService.categories(lang=_lang(request))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@customer_required
def products(request):
    result, status = CatalogService.products(
        lang=_lang(request),
        category_id=request.GET.get('category_id'),
        tag=request.GET.get('tag'),
        q=request.GET.get('q'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@customer_required
def product_detail(request, product_id):
    result, status = CatalogService.product_detail(product_id, lang=_lang(request))
    return JsonResponse(result, status=status)


urlpatterns = [
    path('catalog/categories', categories),
    path('catalog/products', products),
    path('catalog/products/<int:product_id>', product_detail),
]
