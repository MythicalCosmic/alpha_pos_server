"""Customer support endpoints (mounted under api/smartfood/).

GET  support                              -> contacts + FAQ
GET  support/tickets                      -> this customer's ticket threads
POST support/tickets {subject,text}       -> open a ticket (first CUSTOMER message)
POST support/tickets/<id>/messages {text} -> append a CUSTOMER message
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.urls import path

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from smartfood.security import customer_required
from smartfood.services.support_service import SupportService


@require_GET
@customer_required
def support(request):
    result, status = SupportService.contacts()
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@customer_required
def tickets(request):
    if request.method == 'GET':
        result, status = SupportService.list_tickets(request.customer)
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = SupportService.create_ticket(
        request.customer, data.get('subject'), data.get('text'))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@customer_required
def ticket_messages(request, ticket_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = SupportService.add_message(
        request.customer, ticket_id, data.get('text'))
    return JsonResponse(result, status=status)


urlpatterns = [
    path('support', support),
    path('support/tickets', tickets),
    path('support/tickets/<int:ticket_id>/messages', ticket_messages),
]
