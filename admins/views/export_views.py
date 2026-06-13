"""1C export endpoints."""
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from admins.services.export_service import build_export, parse_date_range
from base.security.permissions import admin_required


@require_GET
@admin_required
def one_c_export(request):
    df_str = request.GET.get('from')
    dt_str = request.GET.get('to')
    include_unpaid = request.GET.get('include_unpaid', '').lower() in ('1', 'true', 'yes')

    df, dt, err = parse_date_range(df_str, dt_str)
    if err:
        return JsonResponse({'success': False, 'message': err}, status=422)

    xml, count = build_export(df, dt, include_unpaid=include_unpaid)
    filename = f'orders-{df.isoformat()}-to-{dt.isoformat()}.xml'
    resp = HttpResponse(xml, content_type='application/xml; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    resp['X-Export-Count'] = str(count)
    return resp
