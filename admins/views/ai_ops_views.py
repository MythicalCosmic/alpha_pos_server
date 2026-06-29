"""AI operations endpoints (admin panel): Morning Briefing, context prompt chips,
and Anomaly Watch. Mounted under /api/admins/ai/ (admins.urls). Models + logic live
in the stock app (the AI domain); these are the thin admin-facing views."""
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from base.helpers.request import parse_json_body
from base.security.permissions import admin_required

# Per-route suggestion chips (FE falls back to a static set if this is absent).
_CONTEXT_PROMPTS = {
    '/dashboard': ["Compare to last 30 days", "Which day was weakest?", "What drove the peak?"],
    '/dashboard/sales': ["Explain the dip", "Compare to last month", "Best day this period?"],
    '/dashboard/products': ["Top movers this week", "Which items are Dogs?", "What sells together?"],
    '/dashboard/staff': ["Who had the best shift?", "Any unusual void rates?"],
    '/stock': ["What's about to run out?", "Show dead stock", "Reorder suggestions"],
    '/orders': ["Today's cancellations", "Average ticket trend", "Busiest hour today"],
}


@csrf_exempt
@require_GET
@admin_required
def briefing(request):
    """GET /api/admins/ai/briefing — the cached once-per-business-day digest."""
    from stock.services.ai_briefing_service import AIBriefingService
    data = AIBriefingService.get_or_generate(
        request.user.id, location_id=request.GET.get('location_id'))
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_POST
@admin_required
def briefing_dismiss(request):
    """POST /api/admins/ai/briefing/dismiss — collapse the card for the business day."""
    from stock.services.ai_briefing_service import AIBriefingService
    AIBriefingService.dismiss(request.user.id)
    return JsonResponse({'success': True})


@csrf_exempt
@require_GET
@admin_required
def context_prompts(request):
    """GET /api/admins/ai/context-prompts — per-route click-to-prompt chips."""
    return JsonResponse({'success': True, 'data': _CONTEXT_PROMPTS})


@csrf_exempt
@require_GET
@admin_required
def anomalies(request):
    """GET /api/admins/ai/anomalies?since=ISO&unacked=1 — fired alerts."""
    from stock.services.anomaly_service import AnomalyService
    since = parse_datetime(request.GET.get('since') or '') if request.GET.get('since') else None
    unacked = (request.GET.get('unacked') or '').lower() in ('1', 'true', 'yes')
    data = AnomalyService.list_anomalies(since=since, unacked=unacked)
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_POST
@admin_required
def anomaly_ack(request, anomaly_id):
    """POST /api/admins/ai/anomalies/<id>/ack — mark an alert seen."""
    from stock.services.anomaly_service import AnomalyService
    if not AnomalyService.ack(anomaly_id, request.user.id):
        return JsonResponse({'success': False, 'message': 'Anomaly not found'}, status=404)
    return JsonResponse({'success': True})


@csrf_exempt
@require_http_methods(["GET", "PATCH"])
@admin_required
def anomaly_settings(request):
    """GET/PATCH /api/admins/ai/anomalies/settings — per-operator mute + quiet hours."""
    from stock.services.anomaly_service import AnomalyService
    if request.method == 'GET':
        return JsonResponse({'success': True, 'data': AnomalyService.get_settings(request.user.id)})
    data, err = parse_json_body(request)
    if err:
        data = {}
    result = AnomalyService.update_settings(request.user.id, **(data or {}))
    return JsonResponse({'success': True, 'data': result})
