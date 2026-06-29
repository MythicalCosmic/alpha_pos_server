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


@require_GET
@admin_required
def operations_view(request):
    """GET /dashboard/operations — Operations tab: live table grid, order funnel,
    prep-by-category, orders-by-hour. Defaults to today's business day."""
    from admins.services.operations_dashboard_service import operations_dashboard
    data = operations_dashboard(request.GET.get('from'), request.GET.get('to'))
    return JsonResponse({'success': True, 'data': data})


@require_GET
@admin_required
def sales_view(request):
    """GET /dashboard/sales?range=30d (or ?from=&to=) — the Sales dashboard page:
    revenue/expense series, last-period comparison, hour-of-week heatmap, channel
    mix. Business-day windowed (item 8)."""
    from admins.services.sales_dashboard_service import sales_dashboard
    data = sales_dashboard(
        range_token=request.GET.get('range'),
        date_from=request.GET.get('from'),
        date_to=request.GET.get('to'),
    )
    return JsonResponse({'success': True, 'data': data})
