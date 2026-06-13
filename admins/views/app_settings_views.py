from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from base.helpers.request import parse_json_body
from base.security.permissions import manager_required
from admins.services.app_settings_service import AppSettingsService


@csrf_exempt
@require_http_methods(["GET", "PUT"])
@manager_required
def app_settings(request):
    if request.method == "GET":
        result, status_code = AppSettingsService.get_all()
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        result, status_code = error
        return JsonResponse(result, status=status_code)

    result, status_code = AppSettingsService.update(**data)
    return JsonResponse(result, status=status_code)
