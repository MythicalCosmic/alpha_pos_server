"""Operator catalog management endpoints (mounted at api/admins/smartfood/).

Manager-authenticated thin views over AdminCatalogService: accept POS
products/categories to the bot, edit their bot-only fields, stop/resume selling,
and manage the bot-only sizes / topping groups / toppings. All business logic
lives in the service; these views only parse the body and pass IDs through.
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.urls import path

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import manager_required
from smartfood.services.admin_catalog_service import AdminCatalogService


# ---- products ------------------------------------------------------------- #
@require_GET
@manager_required
def unpublished_products(request):
    result, status = AdminCatalogService.list_unpublished_products()
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
def accept_product(request, product_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.accept_product(product_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['PATCH'])
@manager_required
def update_product(request, product_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.update_product(product_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
def stop_product(request, product_id):
    result, status = AdminCatalogService.set_product_selling(product_id, False)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
def resume_product(request, product_id):
    result, status = AdminCatalogService.set_product_selling(product_id, True)
    return JsonResponse(result, status=status)


# ---- categories ----------------------------------------------------------- #
@csrf_exempt
@require_POST
@manager_required
def accept_category(request, category_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.accept_category(category_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['PATCH'])
@manager_required
def update_category(request, category_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.update_category(category_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
def stop_category(request, category_id):
    result, status = AdminCatalogService.set_category_selling(category_id, False)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
def resume_category(request, category_id):
    result, status = AdminCatalogService.set_category_selling(category_id, True)
    return JsonResponse(result, status=status)


# ---- sizes ---------------------------------------------------------------- #
@csrf_exempt
@require_POST
@manager_required
def create_size(request, product_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.create_size(product_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['PATCH', 'DELETE'])
@manager_required
def size_detail(request, size_id):
    if request.method == 'DELETE':
        result, status = AdminCatalogService.delete_size(size_id)
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.update_size(size_id, **data)
    return JsonResponse(result, status=status)


# ---- topping groups ------------------------------------------------------- #
@csrf_exempt
@require_POST
@manager_required
def create_topping_group(request, product_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.create_topping_group(product_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['PATCH', 'DELETE'])
@manager_required
def topping_group_detail(request, group_id):
    if request.method == 'DELETE':
        result, status = AdminCatalogService.delete_topping_group(group_id)
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.update_topping_group(group_id, **data)
    return JsonResponse(result, status=status)


# ---- toppings ------------------------------------------------------------- #
@csrf_exempt
@require_POST
@manager_required
def create_topping(request, group_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.create_topping(group_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['PATCH', 'DELETE'])
@manager_required
def topping_detail(request, topping_id):
    if request.method == 'DELETE':
        result, status = AdminCatalogService.delete_topping(topping_id)
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AdminCatalogService.update_topping(topping_id, **data)
    return JsonResponse(result, status=status)


# Mounted under api/admins/smartfood/ (paths are relative to that mount).
urlpatterns = [
    path('catalog/unpublished', unpublished_products),

    path('products/<int:product_id>/accept', accept_product),
    path('products/<int:product_id>', update_product),
    path('products/<int:product_id>/stop', stop_product),
    path('products/<int:product_id>/resume', resume_product),

    path('categories/<int:category_id>/accept', accept_category),
    path('categories/<int:category_id>', update_category),
    path('categories/<int:category_id>/stop', stop_category),
    path('categories/<int:category_id>/resume', resume_category),

    path('products/<int:product_id>/sizes', create_size),
    path('sizes/<int:size_id>', size_detail),

    path('products/<int:product_id>/topping-groups', create_topping_group),
    path('topping-groups/<int:group_id>', topping_group_detail),

    path('topping-groups/<int:group_id>/toppings', create_topping),
    path('toppings/<int:topping_id>', topping_detail),
]
