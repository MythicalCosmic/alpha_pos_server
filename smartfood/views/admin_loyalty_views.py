"""Operator loyalty console (explicit permissions, mounted at api/admins/smartfood/).

The staff side of Smart Club — what a cashier/operator hits after scanning a
member's QR (SF-<telegram_id>) or a gift code:

GET  /loyalty/member?member_id=SF-123   look up a member (points, history, gifts)
POST /loyalty/scan    {member_id, order_id}        award points for an in-store buy
POST /loyalty/grant   {member_id, points, reason} grant/deduct points manually
POST /loyalty/fulfill {code}                      mark a redeemed gift handed over

request.user is the operator — recorded on the ledger / redemption for the audit.
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.urls import path

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.auth import login_required
from base.security.permissions import permission_required
from base.security.atomic_command import atomic_command

from smartfood.services.loyalty_service import LoyaltyService
from smartfood.services.loyalty_input import loyalty_errors


@csrf_exempt
@require_GET
@login_required
@permission_required('loyalty.member.view')
def member(request):
    result, status = LoyaltyService.member(request.GET.get('member_id'))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@login_required
@permission_required('loyalty.award')
@atomic_command('loyalty.scan')
@loyalty_errors
def scan_award(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = LoyaltyService.award_scan(
        data.get('member_id'), staff_id=request.user.id, order_id=data.get('order_id'))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@login_required
@permission_required('loyalty.adjust')
@atomic_command('loyalty.grant')
def grant(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = LoyaltyService.grant(
        data.get('member_id'), data.get('points'),
        reason=data.get('reason', ''), staff_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@login_required
@permission_required('loyalty.fulfill')
@atomic_command('loyalty.fulfill')
@loyalty_errors
def fulfill(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = LoyaltyService.fulfill(data.get('code'), staff_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@login_required
@permission_required('loyalty.correct')
@atomic_command('loyalty.staff_cancel')
@loyalty_errors
def cancel(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = LoyaltyService.cancel(
        data.get('code'), data.get('reason'), staff_id=request.user.id,
    )
    return JsonResponse(result, status=status)


urlpatterns = [
    path('loyalty/member', member),
    path('loyalty/scan', scan_award),
    path('loyalty/grant', grant),
    path('loyalty/fulfill', fulfill),
    path('loyalty/cancel', cancel),
]
