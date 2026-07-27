"""1C export endpoints."""
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from admins.services.export_service import build_export, parse_date_range
from base.security.permissions import admin_required


@require_GET
@admin_required
def one_c_export(request):
    df_str = request.GET.get('date_from') or request.GET.get('from')
    dt_str = request.GET.get('date_to') or request.GET.get('to')
    include_unpaid = request.GET.get('include_unpaid', '').lower() in ('1', 'true', 'yes')

    from base.services.business_day import (
        request_window_params, resolve_reporting_window,
    )

    params = request_window_params(request.GET)
    has_exact = any(params[name] not in (None, '') for name in (
        'datetime_from', 'datetime_to', 'from_at', 'to_at',
    ))
    if not has_exact:
        df, dt, err = parse_date_range(df_str, dt_str)
        if err:
            return JsonResponse({'success': False, 'message': err}, status=422)
        params['date_from'], params['date_to'] = df, dt
    try:
        window = resolve_reporting_window(**params)
    except ValueError as exc:
        return JsonResponse(
            {
                'success': False,
                'message': str(exc),
                'errors': {'range': str(exc)},
            },
            status=422,
        )

    xml, count = build_export(
        include_unpaid=include_unpaid,
        window=window,
    )
    filename = (
        f'orders-{window.date_from.isoformat()}'
        f'-to-{window.date_to.isoformat()}.xml'
    )
    resp = HttpResponse(xml, content_type='application/xml; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    resp['X-Export-Count'] = str(count)
    # Binary/XML exports cannot carry the dashboard's JSON ``range`` object,
    # so expose the same authoritative half-open bounds as response metadata.
    resp['X-Range-Start'] = window.start_at.isoformat()
    resp['X-Range-End'] = window.end_at.isoformat()
    resp['X-Range-Mode'] = window.mode
    return resp
