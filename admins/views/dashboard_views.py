"""Owner dashboard endpoints."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from admins.services.dashboard_service import get_today, get_range, get_sidebar_counts
from admins.services.workbook_export_service import build_dashboard_workbook
from admins.views.export_response import xlsx_attachment
from base.security.permissions import admin_required
from base.security.rate_limit import rate_limit


def _reporting_kwargs(request):
    """Canonical ISO datetime pair plus backwards-compatible date/clock inputs."""
    from base.services.business_day import request_window_params
    return request_window_params(request.GET)


def _range_error(exc):
    return JsonResponse(
        {'success': False, 'message': str(exc), 'errors': {'range': str(exc)}},
        status=422,
    )


@require_GET
@admin_required
def today_view(request):
    return JsonResponse({'success': True, 'data': get_today()})


@require_GET
@admin_required
def range_view(request):
    """GET /dashboard?from=YYYY-MM-DD&to=YYYY-MM-DD — date-range headline figures.
    Optional tod_from/tod_to ("HH:MM") = working-hours filter within each day."""
    try:
        data = get_range(**_reporting_kwargs(request))
    except ValueError as exc:
        return _range_error(exc)
    return JsonResponse({'success': True, 'data': data})


@require_GET
@rate_limit('admin_dashboard_export', max_attempts=10, window=60)
@admin_required
def export_view(request):
    """Download the same filtered dashboard figures as a native XLSX file."""
    from base.services.business_day import parse_hhmm

    raw_tod_from = request.GET.get('tod_from')
    raw_tod_to = request.GET.get('tod_to')
    effective_tod_from = parse_hhmm(raw_tod_from)
    effective_tod_to = parse_hhmm(raw_tod_to)
    try:
        data = get_range(**_reporting_kwargs(request))
    except ValueError as exc:
        return _range_error(exc)
    payload = build_dashboard_workbook(
        data,
        filters={
            'tod_from': (
                effective_tod_from.strftime('%H:%M')
                if effective_tod_from else None
            ),
            'tod_to': (
                effective_tod_to.strftime('%H:%M')
                if effective_tod_to else None
            ),
        },
    )
    date_range = data['range']
    filename = (
        f'alpha-pos-dashboard-{date_range["from"]}'
        f'-to-{date_range["to"]}.xlsx'
    )
    return xlsx_attachment(payload, filename, count=data.get('orders', 0))


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
    try:
        data = operations_dashboard(**_reporting_kwargs(request))
    except ValueError as exc:
        return _range_error(exc)
    return JsonResponse({'success': True, 'data': data})


@require_GET
@admin_required
def sales_view(request):
    """GET /dashboard/sales?range=30d (or ?from=&to=) — the Sales dashboard page:
    revenue/expense series, last-period comparison, hour-of-week heatmap, channel
    mix. Business-day windowed (item 8)."""
    from admins.services.sales_dashboard_service import sales_dashboard
    try:
        data = sales_dashboard(
            range_token=request.GET.get('range'),
            granularity=request.GET.get('granularity'),
            **_reporting_kwargs(request),
        )
    except ValueError as exc:
        return _range_error(exc)
    return JsonResponse({'success': True, 'data': data})


@require_GET
@admin_required
def sales_expenses_view(request):
    """GET /dashboard/sales/expenses -- paginated drawer expenses for a range."""
    from base.helpers.request import safe_page
    from admins.services.sales_dashboard_service import sales_expenses

    try:
        per_page = int(request.GET.get('limit') or request.GET.get('per_page') or 50)
    except (TypeError, ValueError):
        per_page = 50
    per_page = max(1, min(per_page, 200))
    try:
        data = sales_expenses(
            page=safe_page(request),
            per_page=per_page,
            **_reporting_kwargs(request),
        )
    except ValueError as exc:
        return _range_error(exc)
    return JsonResponse({'success': True, 'data': data})
