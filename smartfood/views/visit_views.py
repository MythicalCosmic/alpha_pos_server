"""Customer-side analytics events for the Telegram Mini App."""
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from base.helpers.request import get_client_ip, get_user_agent, parse_json_body
from base.helpers.response import json_response
from smartfood.security import customer_required
from smartfood.services.analytics_service import BotVisitService


@csrf_exempt
@require_POST
@customer_required
def record_visit(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = BotVisitService.record(
        request.customer,
        data.get('client_visit_id'),
        user_agent=get_user_agent(request),
        ip_address=get_client_ip(request),
    )
    return JsonResponse(result, status=status)


urlpatterns = [
    path('analytics/visit', record_visit, name='smartfood-record-visit'),
]
