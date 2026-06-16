"""Operator loyalty console (manager auth, mounted at api/admins/smartfood/).

The staff side of Smart Club — what a cashier/operator hits after scanning a
member's QR (SF-<telegram_id>) or a gift code:

GET  /loyalty/member?member_id=SF-123   look up a member (points, history, gifts)
POST /loyalty/scan    {member_id, amount}        award points for an in-store buy
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
from base.security.permissions import manager_required

from smartfood.services.loyalty_service import LoyaltyService


@csrf_exempt
@require_GET
@manager_required
def member(request):
    result, status = LoyaltyService.member(request.GET.get('member_id'))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
def scan_award(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = LoyaltyService.award_scan(
        data.get('member_id'), data.get('amount'), staff_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
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
@manager_required
def fulfill(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = LoyaltyService.fulfill(data.get('code'), staff_id=request.user.id)
    return JsonResponse(result, status=status)


urlpatterns = [
    path('loyalty/member', member),
    path('loyalty/scan', scan_award),
    path('loyalty/grant', grant),
    path('loyalty/fulfill', fulfill),
]
