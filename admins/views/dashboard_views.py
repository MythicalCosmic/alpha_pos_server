"""Owner dashboard endpoints."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from admins.services.dashboard_service import get_today
from base.security.permissions import admin_required


@require_GET
@admin_required
def today_view(request):
    return JsonResponse({'success': True, 'data': get_today()})
