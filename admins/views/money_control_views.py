from django.http import JsonResponse
from django.views.decorators.http import require_GET

from base.http_validation import (
    QueryValidationError,
    iso_date,
    optional_int,
    validate_period,
)
from base.security.permissions import backoffice_permission_required
from base.services.business_day import business_date
from base.services.money_control_service import MoneyControlService


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
