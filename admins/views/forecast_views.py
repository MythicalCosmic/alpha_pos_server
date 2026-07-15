"""Demand forecasting endpoint."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from admins.services.forecast_service import forecast_tomorrow
from base.security.permissions import admin_required
from base.security.rate_limit import rate_limit


# The aggregate is cheap but scans recent order items. Five refreshes per
# minute is ample for the admin UI and protects against accidental polling.
@require_GET
@rate_limit('forecast_tomorrow', max_attempts=5, window=60)
@admin_required
def tomorrow_view(request):
    data, _ = forecast_tomorrow()
    return JsonResponse({'success': True, 'data': data})
