"""Demand forecasting endpoint."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from admins.services.forecast_service import forecast_tomorrow
from base.security.permissions import admin_required
from base.security.rate_limit import rate_limit


# Gemini calls are billable — cap to 5/min per admin IP. Generating
# tomorrow's forecast once an hour is the expected usage pattern;
# anything beyond that is almost always a UI accidental double-click.
@require_GET
@rate_limit('forecast_tomorrow', max_attempts=5, window=60)
@admin_required
def tomorrow_view(request):
    data, err = forecast_tomorrow()
    if err == 'no_history':
        return JsonResponse(
            {'success': True, 'data': {'predictions': [], 'reason': 'no_history'}},
        )
    if err:
        status = 503 if err in ('llm_sdk_missing', 'llm_key_missing') else 502
        return JsonResponse({'success': False, 'message': err}, status=status)
    return JsonResponse({'success': True, 'data': data})
