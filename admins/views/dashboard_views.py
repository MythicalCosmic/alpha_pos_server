"""Owner dashboard endpoints."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from admins.services.dashboard_service import get_today, get_range, get_sidebar_counts
from base.security.permissions import admin_required


@require_GET
@admin_required
def today_view(request):
    return JsonResponse({'success': True, 'data': get_today()})


@require_GET
@admin_required
def range_view(request):
    """GET /dashboard?from=YYYY-MM-DD&to=YYYY-MM-DD — date-range headline figures."""
    data = get_range(request.GET.get('from'), request.GET.get('to'))
    return JsonResponse({'success': True, 'data': data})


@require_GET
@admin_required
def sidebar_counts_view(request):
    """GET /sidebar-counts — {active_shifts, today_orders, today_revenue}."""
    return JsonResponse({'success': True, 'data': get_sidebar_counts()})
