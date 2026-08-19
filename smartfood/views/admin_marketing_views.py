"""Manager-authenticated home-banner and reward-catalog endpoints."""
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import manager_required
from smartfood.services.marketing_service import BannerService, RewardCatalogService
from smartfood.services.media_service import BannerMediaService, RewardMediaService


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
def banners(request):
    if request.method == 'GET':
        result, status = BannerService.list_admin()
    else:
        data, error = _json(request)
        if error:
            return error
        result, status = BannerService.create(data)
    return JsonResponse(result, status=status)


@csrf_exempt
@manager_required
@require_http_methods(['PATCH', 'DELETE'])
def banner_detail(request, banner_id):
    if request.method == 'DELETE':
        result, status = BannerService.delete(banner_id)
    else:
        data, error = _json(request)
        if error:
            return error
        result, status = BannerService.update(banner_id, data)
    return JsonResponse(result, status=status)


@csrf_exempt
@manager_required
@require_http_methods(['POST', 'DELETE'])
def banner_image(request, banner_id):
    if request.method == 'DELETE':
        result, status = BannerMediaService.remove(banner_id)
    else:
        result, status = BannerMediaService.upload(
            banner_id,
            request.FILES.get('image'),
        )
    return JsonResponse(result, status=status)


@csrf_exempt
@manager_required
@require_http_methods(['GET', 'POST'])
def rewards(request):
    if request.method == 'GET':
        result, status = RewardCatalogService.list_admin()
    else:
        data, error = _json(request)
        if error:
            return error
        result, status = RewardCatalogService.create(data)
    return JsonResponse(result, status=status)


@csrf_exempt
@manager_required
@require_http_methods(['PATCH', 'DELETE'])
def reward_detail(request, reward_id):
    if request.method == 'DELETE':
        result, status = RewardCatalogService.delete(reward_id)
    else:
        data, error = _json(request)
        if error:
            return error
        result, status = RewardCatalogService.update(reward_id, data)
    return JsonResponse(result, status=status)


@csrf_exempt
@manager_required
@require_http_methods(['POST', 'DELETE'])
def reward_image(request, reward_id):
    if request.method == 'DELETE':
        result, status = RewardMediaService.remove(reward_id)
    else:
        result, status = RewardMediaService.upload(
            reward_id,
            request.FILES.get('image'),
        )
    return JsonResponse(result, status=status)


urlpatterns = [
    path('marketing/banners', banners),
    path('marketing/banners/<int:banner_id>', banner_detail),
    path('marketing/banners/<int:banner_id>/image', banner_image),
    path('loyalty/rewards', rewards),
    path('loyalty/rewards/<int:reward_id>', reward_detail),
    path('loyalty/rewards/<int:reward_id>/image', reward_image),
]
