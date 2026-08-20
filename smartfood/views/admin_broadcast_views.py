"""Manager-authenticated draft and send controls for Telegram broadcasts."""

from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import manager_required
from smartfood.services.broadcast_service import BroadcastService
from smartfood.services.media_service import BroadcastMediaService


def _json(request):
    data, error = parse_json_body(request)
    if error:
        return None, json_response(error)
    if not isinstance(data, dict):
        return None, JsonResponse(
            {'success': False, 'message': 'Expected a JSON object.'},
            status=422,
        )
    return data, None


@csrf_exempt
@manager_required
@require_http_methods(['GET', 'POST'])
def broadcasts(request):
    if request.method == 'GET':
        result, status = BroadcastService.list(
            status=request.GET.get('status', ''),
            q=request.GET.get('q', ''),
            page=request.GET.get('page', 1),
            per_page=request.GET.get('per_page', 20),
        )
    else:
        data, error = _json(request)
        if error:
            return error
        result, status = BroadcastService.create(data, actor=request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@manager_required
@require_http_methods(['GET', 'PATCH', 'DELETE'])
def broadcast_detail(request, broadcast_id):
    if request.method == 'GET':
        result, status = BroadcastService.get(broadcast_id)
    elif request.method == 'DELETE':
        result, status = BroadcastService.delete(broadcast_id)
    else:
        data, error = _json(request)
        if error:
            return error
        result, status = BroadcastService.update(
            broadcast_id,
            data,
            actor=request.user,
        )
    return JsonResponse(result, status=status)


@csrf_exempt
@manager_required
@require_http_methods(['POST'])
def broadcast_send(request, broadcast_id):
    data, error = _json(request)
    if error:
        return error
    result, status = BroadcastService.send(
        broadcast_id,
        actor=request.user,
        expected_updated_at=data.get('expected_updated_at'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@manager_required
@require_http_methods(['POST', 'DELETE'])
def broadcast_image(request, broadcast_id):
    if request.method == 'DELETE':
        result, status = BroadcastMediaService.remove(broadcast_id)
    else:
        result, status = BroadcastMediaService.upload(
            broadcast_id,
            request.FILES.get('image'),
        )
    return JsonResponse(result, status=status)


urlpatterns = [
    path('broadcasts', broadcasts),
    path('broadcasts/<int:broadcast_id>', broadcast_detail),
    path('broadcasts/<int:broadcast_id>/send', broadcast_send),
    path('broadcasts/<int:broadcast_id>/image', broadcast_image),
]
