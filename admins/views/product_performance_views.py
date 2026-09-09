"""Admin HTTP endpoints for product performance reporting and downloads."""

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from admins.services.product_performance_export_service import (
    EXPORT_FORMATS,
    render_product_performance_export,
)
from admins.services.product_performance_service import (
    ProductPerformanceError,
    product_performance_report,
)
from admins.views.export_response import file_attachment
from base.security.permissions import manager_required
from base.security.rate_limit import rate_limit
from base.services.branch_scope import resolve_actor_branch
from base.services.business_day import request_window_params


def _report_kwargs(request):
    query = request.GET
    requested_branch = query.get('branch_id')
    branch_id = (
        requested_branch
        if getattr(request.user, 'role', None) == 'ADMIN'
        else resolve_actor_branch(request.user)
    )
    return {
        **request_window_params(query),
        'branch_id': branch_id,
        'preset': query.get('preset'),
        'category_id': query.get('category_id'),
        'product_id': query.get('product_id'),
        'search': query.get('search'),
        'cashier_id': query.get('cashier_id'),
        'order_type': query.get('order_type'),
        'order_origin': query.get('order_origin'),
        'payment_method': query.get('payment_method'),
        'sort': query.get('sort'),
    }


def _error(exc):
    return JsonResponse({
        'success': False,
        'code': exc.code,
        'message': str(exc),
        'errors': exc.errors,
    }, status=exc.status)


@require_GET
@manager_required
def product_performance(request):
    """Paginated on-screen report; totals cover the complete filtered set."""
    try:
        report = product_performance_report(
            **_report_kwargs(request),
            page=request.GET.get('page'),
            per_page=request.GET.get('per_page'),
        )
    except ProductPerformanceError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': report})


@require_GET
@rate_limit('admin_product_performance_export', max_attempts=10, window=60)
@manager_required
def product_performance_export(request):
    """Download the complete filtered dataset as XLSX, PDF, or UTF-8 CSV."""
    file_format = str(request.GET.get('format') or 'xlsx').strip().lower()
    if file_format == 'excel':
        file_format = 'xlsx'
    if file_format not in EXPORT_FORMATS:
        return _error(ProductPerformanceError(
            'Unsupported export format',
            code='INVALID_EXPORT_FORMAT',
            errors={'format': 'Use xlsx, excel, pdf, or csv'},
        ))
    try:
        report = product_performance_report(
            **_report_kwargs(request), paginate=False,
        )
    except ProductPerformanceError as exc:
        return _error(exc)
    payload = render_product_performance_export(report, file_format)
    content_type, extension = EXPORT_FORMATS[file_format]
    date_range = report['range']
    filename = (
        f'alpha-pos-product-performance-{date_range["from"]}'
        f'-to-{date_range["to"]}.{extension}'
    )
    summary = report['summary']
    return file_attachment(
        payload,
        filename,
        content_type,
        count=summary['product_count'],
        headers={
            'X-Report-From': date_range['from'],
            'X-Report-To': date_range['to'],
            'X-Report-Cost-Complete': str(summary['cost_complete']).lower(),
        },
    )
