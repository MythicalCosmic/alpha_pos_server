"""Admin-only HTTP contract for profitability reporting and setup."""

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from admins.models import ProductCostProfile, ProfitAdjustment, RecurringCost
from admins.services.profitability_service import (
    ProfitabilityError,
    approve_adjustment,
    classify_cashbox_expense,
    close_profit_period,
    create_adjustment,
    delete_draft_adjustment,
    profitability_report,
    resolve_branch_id,
    save_product_cost,
    save_recurring_cost,
    serialize_adjustment,
    serialize_product_cost,
    serialize_recurring_cost,
    set_expense_category_group,
    setup_data,
    update_configuration,
)
from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import admin_required
from base.services.business_day import request_window_params


def _branch(request, payload=None):
    payload = payload or {}
    return payload.get('branch_id') or request.GET.get('branch_id')


def _error(exc):
    body = {
        'success': False,
        'message': str(exc),
        'errors': exc.errors,
    }
    if exc.report is not None:
        body['data'] = {'report': exc.report}
    return JsonResponse(body, status=422)


def _json(request):
    payload, error = parse_json_body(request)
    if error:
        return None, json_response(error)
    return payload, None


@require_GET
@admin_required
def profitability(request):
    try:
        data = profitability_report(
            branch_id=_branch(request),
            live=request.GET.get('live', '').lower() in {'1', 'true', 'yes'},
            **request_window_params(request.GET),
        )
    except (ProfitabilityError, ValueError) as exc:
        if not isinstance(exc, ProfitabilityError):
            exc = ProfitabilityError(str(exc), errors={'range': str(exc)})
        return _error(exc)
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_http_methods(['GET', 'PATCH'])
@admin_required
def setup(request):
    if request.method == 'GET':
        try:
            data = setup_data(
                _branch(request),
                payout_page=request.GET.get('payout_page'),
                payout_page_size=request.GET.get('payout_page_size'),
                payout_status=request.GET.get('payout_status', 'all'),
            )
        except ProfitabilityError as exc:
            return _error(exc)
        return JsonResponse({'success': True, 'data': data})
    payload, response = _json(request)
    if response:
        return response
    try:
        data = update_configuration(_branch(request, payload), payload, request.user)
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@admin_required
def product_costs(request):
    if request.method == 'GET':
        try:
            branch_id = resolve_branch_id(_branch(request))
            rows = ProductCostProfile.objects.filter(
                branch_id=branch_id,
            ).select_related('product')
        except ProfitabilityError as exc:
            return _error(exc)
        return JsonResponse({
            'success': True,
            'data': [serialize_product_cost(row) for row in rows],
        })
    payload, response = _json(request)
    if response:
        return response
    try:
        data = save_product_cost(_branch(request, payload), payload, request.user)
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data}, status=201)


@csrf_exempt
@require_http_methods(['PATCH'])
@admin_required
def product_cost_detail(request, profile_id):
    payload, response = _json(request)
    if response:
        return response
    try:
        data = save_product_cost(
            _branch(request, payload), payload, request.user, profile_id=profile_id,
        )
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@admin_required
def recurring_costs(request):
    if request.method == 'GET':
        try:
            branch_id = resolve_branch_id(_branch(request))
            rows = RecurringCost.objects.filter(branch_id=branch_id)
        except ProfitabilityError as exc:
            return _error(exc)
        return JsonResponse({
            'success': True,
            'data': [serialize_recurring_cost(row) for row in rows],
        })
    payload, response = _json(request)
    if response:
        return response
    try:
        data = save_recurring_cost(_branch(request, payload), payload, request.user)
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data}, status=201)


@csrf_exempt
@require_http_methods(['PATCH'])
@admin_required
def recurring_cost_detail(request, cost_id):
    payload, response = _json(request)
    if response:
        return response
    try:
        data = save_recurring_cost(
            _branch(request, payload), payload, request.user, cost_id=cost_id,
        )
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@admin_required
def adjustments(request):
    if request.method == 'GET':
        try:
            branch_id = resolve_branch_id(_branch(request))
            rows = ProfitAdjustment.objects.filter(branch_id=branch_id)
        except ProfitabilityError as exc:
            return _error(exc)
        return JsonResponse({
            'success': True,
            'data': [serialize_adjustment(row) for row in rows[:200]],
        })
    payload, response = _json(request)
    if response:
        return response
    try:
        data = create_adjustment(_branch(request, payload), payload, request.user)
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data}, status=201)


@csrf_exempt
@require_POST
@admin_required
def adjustment_approve(request, adjustment_id):
    payload, response = _json(request)
    if response:
        return response
    try:
        data = approve_adjustment(
            _branch(request, payload), adjustment_id, request.user,
        )
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_http_methods(['DELETE'])
@admin_required
def adjustment_detail(request, adjustment_id):
    try:
        data = delete_draft_adjustment(
            _branch(request), adjustment_id,
        )
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_http_methods(['PUT'])
@admin_required
def cashbox_classification(request, expense_id):
    payload, response = _json(request)
    if response:
        return response
    try:
        data = classify_cashbox_expense(
            _branch(request, payload), expense_id, payload, request.user,
        )
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_http_methods(['PUT'])
@admin_required
def expense_category_group(request, source, category_id):
    payload, response = _json(request)
    if response:
        return response
    try:
        data = set_expense_category_group(
            source, category_id, payload.get('reporting_group'),
        )
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({'success': True, 'data': data})


@csrf_exempt
@require_POST
@admin_required
def period_close(request):
    payload, response = _json(request)
    if response:
        return response
    try:
        closed, created = close_profit_period(
            _branch(request, payload),
            payload.get('period'),
            request.user,
            correction_reason=payload.get('correction_reason', ''),
        )
    except ProfitabilityError as exc:
        return _error(exc)
    return JsonResponse({
        'success': True,
        'data': {
            'id': closed.id,
            'created': created,
            'period_start': closed.period_start.isoformat(),
            'period_end': closed.period_end.isoformat(),
            'revision': closed.revision,
            'closed_at': closed.closed_at.isoformat(),
            'report': closed.report_snapshot,
        },
    }, status=201 if created else 200)
