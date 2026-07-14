from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.security.permissions import admin_required
from admins.services.place_service import PlaceService, TableService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def places(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        place_type = request.GET.get('place_type')
        is_active = request.GET.get('is_active')

        if is_active is not None:
            is_active = is_active.lower() == 'true'

        result, status_code = PlaceService.list(
            page=page, per_page=per_page,
            place_type=place_type, is_active=is_active,
            branch_id=request.GET.get('branch_id'),
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        result, status_code = error
        return JsonResponse(result, status=status_code)

    result, status_code = PlaceService.create(
        name=data.get('name'),
        place_type=data.get('place_type', 'HALL'),
        capacity=data.get('capacity', 0),
        branch_id=data.get('branch_id'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def place_detail(request, place_id):
    if request.method == "GET":
        result, status_code = PlaceService.get(place_id, request.GET.get('branch_id'))
        return JsonResponse(result, status=status_code)

    if request.method == "PUT":
        data, error = parse_json_body(request)
        if error:
            result, status_code = error
            return JsonResponse(result, status=status_code)

        result, status_code = PlaceService.update(
            place_id, branch_id=data.pop('branch_id', None), **data,
        )
        return JsonResponse(result, status=status_code)

    result, status_code = PlaceService.delete(place_id, request.GET.get('branch_id'))
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def tables(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        place_id = request.GET.get('place_id')
        status = request.GET.get('status')

        if place_id:
            place_id = int(place_id)

        result, status_code = TableService.list(
            page=page, per_page=per_page,
            place_id=place_id, status=status,
            branch_id=request.GET.get('branch_id'),
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        result, status_code = error
        return JsonResponse(result, status=status_code)

    result, status_code = TableService.create(
        place_id=data.get('place_id'),
        number=data.get('number'),
        capacity=data.get('capacity', 4),
        branch_id=data.get('branch_id'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def table_detail(request, table_id):
    if request.method == "GET":
        result, status_code = TableService.get(table_id, request.GET.get('branch_id'))
        return JsonResponse(result, status=status_code)

    if request.method == "PUT":
        data, error = parse_json_body(request)
        if error:
            result, status_code = error
            return JsonResponse(result, status=status_code)

        result, status_code = TableService.update(
            table_id, branch_id=data.pop('branch_id', None), **data,
        )
        return JsonResponse(result, status=status_code)

    result, status_code = TableService.delete(table_id, request.GET.get('branch_id'))
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["PATCH"])
@admin_required
def table_status(request, table_id):
    data, error = parse_json_body(request)
    if error:
        result, status_code = error
        return JsonResponse(result, status=status_code)

    status = data.get('status')
    if not status:
        return JsonResponse(
            {'success': False, 'message': 'Status is required'},
            status=400,
        )

    result, status_code = TableService.update_status(
        table_id, status, data.get('branch_id'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET"])
@admin_required
def tables_by_place(request, place_id):
    result, status_code = TableService.get_for_place(
        place_id, request.GET.get('branch_id'),
    )
    return JsonResponse(result, status=status_code)
