"""Manager-authenticated Telegram customer registry endpoints."""

from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from base.security.permissions import manager_required
from smartfood.services.admin_customer_service import AdminCustomerService


@csrf_exempt
@require_GET
@manager_required
def users_summary(request):
    result, status = AdminCustomerService.summary()
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@manager_required
def users(request):
    result, status = AdminCustomerService.list(
        q=request.GET.get('q', ''),
        language=request.GET.get('language', ''),
        profile=request.GET.get('profile', ''),
        ordered=request.GET.get('ordered', ''),
        page=request.GET.get('page', 1),
        per_page=request.GET.get('per_page', 20),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@manager_required
def user_detail(request, customer_id):
    result, status = AdminCustomerService.detail(customer_id)
    return JsonResponse(result, status=status)


urlpatterns = [
    path('users/summary', users_summary),
    path('users', users),
    path('users/<int:customer_id>', user_detail),
]
