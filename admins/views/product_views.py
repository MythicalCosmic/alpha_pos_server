from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.rate_limit import rate_limit
from base.security.permissions import manager_required, permission_required
from base.security.audit import audit
from base.models import AuditLog, Product
from admins.services.product_service import AdminProductService
from admins.requests.product_requests import create_product_request, bulk_ids_request


def _check_permission(request, perm):
    user_perms = request.user.permissions or []
    # ADMIN role has every permission (matches base.security.permissions
    # .permission_required) — without this an admin without '*' in their
    # permissions list got a 403 on create/edit.
    if '*' in user_perms or getattr(request.user, 'role', None) == 'ADMIN':
        return None
    if perm not in user_perms:
        return JsonResponse(
            {"success": False, "message": "You don't have permission to perform this action"},
            status=403,
        )
    return None


@csrf_exempt
@require_http_methods(["GET", "POST"])
@manager_required
def products(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        search = request.GET.get('search')
        category_ids = request.GET.get('category_ids')
        order_by = request.GET.get('order_by', '-created_at')
        include_deleted = request.GET.get('include_deleted', '').lower() == 'true'
        # Top-selling first is the default; pass popular=false to disable.
        popular = request.GET.get('popular', 'true').lower() not in ('false', '0', 'no')

        result, status_code = AdminProductService.get_all_products(
            page=page,
            per_page=per_page,
            search=search,
            category_ids=category_ids,
            order_by=order_by,
            include_deleted=include_deleted,
            popular=popular,
        )
        return JsonResponse(result, status=status_code)

    denied = _check_permission(request, 'product.create')
    if denied:
        return denied

    data, error = create_product_request(request)
    if error:
        return json_response(error)

    result, status_code = AdminProductService.create_product(
        name=data['name'],
        description=data.get('description'),
        price=data['price'],
        category_id=data['category_id'],
        colors=data.get('colors'),
        is_instant=data.get('is_instant', False),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
@manager_required
def product_detail(request, product_id):
    if request.method == "GET":
        include_deleted = request.GET.get('include_deleted', '').lower() == 'true'
        result, status_code = AdminProductService.get_product_by_id(product_id, include_deleted)
        return JsonResponse(result, status=status_code)

    if request.method == "DELETE":
        denied = _check_permission(request, 'product.delete')
        if denied:
            return denied
        hard_delete = request.GET.get('hard', '').lower() == 'true'
        result, status_code = AdminProductService.delete_product(product_id, hard_delete)
        return JsonResponse(result, status=status_code)

    denied = _check_permission(request, 'product.update')
    if denied:
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    # Capture the pre-update price so an audit row can record the delta
    # when price actually changes. Price is the highest-fraud field on
    # this surface — quietly cutting it ahead of a friend's order and
    # restoring it afterward should not be silent.
    old_price = None
    if 'price' in data:
        old_price = Product.objects.filter(pk=product_id).values_list('price', flat=True).first()

    result, status_code = AdminProductService.update_product(product_id, **data)

    if result.get('success') and 'price' in data:
        new_price = (result.get('data') or {}).get('product', {}).get('price')
        if old_price is None or str(old_price) != str(new_price):
            audit(
                request,
                AuditLog.Action.PRODUCT_PRICE_CHANGE,
                target_type='Product',
                target_id=product_id,
                metadata={
                    'old_price': str(old_price) if old_price is not None else None,
                    'new_price': str(new_price) if new_price is not None else None,
                },
            )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def products_by_category(request, category_id):
    result, status_code = AdminProductService.get_products_by_category(category_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def product_stats(request):
    result, status_code = AdminProductService.get_product_stats()
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def deleted_products(request):
    page = safe_page(request)
    per_page = safe_per_page(request, 20)
    result, status_code = AdminProductService.get_deleted_products(page, per_page)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@manager_required
@permission_required('product.update')
def restore_product(request, product_id):
    result, status_code = AdminProductService.restore_product(product_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@rate_limit('admin_bulk_delete_products', 10, 60)
@manager_required
@permission_required('product.delete')
def bulk_delete_products(request):
    data, error = bulk_ids_request(request)
    if error:
        return json_response(error)
    result, status_code = AdminProductService.bulk_delete(data['ids'])
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@rate_limit('admin_bulk_restore_products', 10, 60)
@manager_required
@permission_required('product.update')
def bulk_restore_products(request):
    data, error = bulk_ids_request(request)
    if error:
        return json_response(error)
    result, status_code = AdminProductService.bulk_restore(data['ids'])
    return JsonResponse(result, status=status_code)
