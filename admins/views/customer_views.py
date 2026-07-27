from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from admins.services.customer_service import AdminCustomerService
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.security.permissions import admin_required


@require_GET
@admin_required
def customers(request):
    result, status_code = AdminCustomerService.list_customers(
        actor=request.user,
        page=safe_page(request),
        per_page=safe_per_page(request, 20),
        search=request.GET.get("search", ""),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["PATCH"])
@admin_required
def customer_detail(request, customer_id):
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])
    result, status_code = AdminCustomerService.update_customer(
        customer_id,
        actor=request.user,
        changes=data,
    )
    return JsonResponse(result, status=status_code)
