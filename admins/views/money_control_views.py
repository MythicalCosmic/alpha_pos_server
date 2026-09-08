from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from base.http_validation import (
    QueryValidationError,
    iso_date,
    optional_int,
    validate_period,
)
from base.security.permissions import backoffice_permission_required
from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.services.business_day import business_date
from base.services.money_control_service import MoneyControlService
from admins.services.cash_position_service import CashPositionService


@require_GET
@backoffice_permission_required('money.control.view')
def overview(request):
    try:
        default_date = business_date()
        date_from = iso_date(request.GET, 'date_from', default_date)
        date_to = iso_date(request.GET, 'date_to', date_from)
        validate_period(date_from, date_to)
        location_id = optional_int(request.GET, 'location_id')
    except QueryValidationError as exc:
        return JsonResponse({
            'success': False,
            'code': 'FILTER_VALIDATION_ERROR',
            'message': 'One or more filters are invalid.',
            'errors': exc.errors,
        }, status=422)
    result, status = MoneyControlService.overview(
        actor=request.user,
        date_from=date_from,
        date_to=date_to,
        location_id=location_id,
    )
    return JsonResponse(result, status=status)


@require_GET
@backoffice_permission_required('money.control.view')
def cash_position(request):
    return json_response(CashPositionService.get(actor=request.user))


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@backoffice_permission_required('money.control.view')
def recurring_costs(request):
    if request.method == 'GET':
        return json_response(CashPositionService.recurring_costs(actor=request.user))
    payload, error = parse_json_body(request)
    if error:
        return json_response(error)
    return json_response(CashPositionService.save_recurring(
        actor=request.user,
        payload=payload,
    ))


@csrf_exempt
@require_http_methods(['PATCH'])
@backoffice_permission_required('money.control.view')
def recurring_cost_detail(request, cost_id):
    payload, error = parse_json_body(request)
    if error:
        return json_response(error)
    return json_response(CashPositionService.save_recurring(
        actor=request.user,
        payload=payload,
        cost_id=cost_id,
    ))
