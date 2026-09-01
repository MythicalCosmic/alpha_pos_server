from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.http_validation import (
    QueryValidationError,
    boolean,
    iso_date,
    optional_int,
    positive_int,
)
from base.security.idempotency import idempotent
from base.security.audit import audit
from base.security.permissions import (
    backoffice_permission_required,
    backoffice_required,
    permission_denied_response,
    user_has_permission,
)
from hr.services import ExpenseCategoryService, ExpenseService
from base.models import AuditLog


def _filter_error(exc):
    return JsonResponse({
        'success': False,
        'code': 'FILTER_VALIDATION_ERROR',
        'message': 'One or more filters are invalid.',
        'errors': exc.errors,
    }, status=422)


def _expense_view_scope(request):
    if user_has_permission(request.user, 'expense.request.view_all'):
        return True, None
    if user_has_permission(request.user, 'expense.request.view_own'):
        return False, None
    return False, permission_denied_response(
        request,
        'expense.request.view_own or expense.request.view_all',
    )


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@backoffice_required
def expense_categories(request):
    permission = (
        'expense.category.view' if request.method == 'GET'
        else 'expense.category.manage'
    )
    if denied := permission_denied_response(request, permission):
        return denied
    if request.method == 'GET':
        try:
            include_inactive = boolean(request.GET, 'include_inactive', False)
            page = positive_int(request.GET, 'page', 1)
            per_page = positive_int(request.GET, 'per_page', 100, maximum=100)
        except QueryValidationError as exc:
            return _filter_error(exc)
        if include_inactive and not user_has_permission(
            request.user,
            'expense.category.manage',
        ):
            return JsonResponse({
                'success': False,
                'code': 'EXPENSE_CATEGORY_INACTIVE_FORBIDDEN',
                'message': 'Manager category permission is required.',
            }, status=403)
        result, status = ExpenseCategoryService.list(
            page=page,
            per_page=per_page,
            search=request.GET.get('search'),
            include_inactive=include_inactive,
        )
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = ExpenseCategoryService.create(actor=request.user, **data)
    if result.get('success'):
        category = result['data']['category']
        audit(
            request,
            AuditLog.Action.EXPENSE_CATEGORY_CREATE,
            target_type='ExpenseCategory',
            target_id=category['id'],
            metadata={
                'code': category['code'],
                'name': category['name'],
                'allowed_sources': category['allowed_sources'],
            },
        )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['GET', 'PATCH'])
@backoffice_required
def expense_category_detail(request, category_id):
    permission = (
        'expense.category.view' if request.method == 'GET'
        else 'expense.category.manage'
    )
    if denied := permission_denied_response(request, permission):
        return denied
    if request.method == 'GET':
        result, status = ExpenseCategoryService.get(category_id)
    else:
        data, error = parse_json_body(request)
        if error:
            return json_response(error)
        result, status = ExpenseCategoryService.update(
            category_id,
            actor=request.user,
            **data,
        )
        if result.get('success'):
            category = result['data']['category']
            audit(
                request,
                AuditLog.Action.EXPENSE_CATEGORY_UPDATE,
                target_type='ExpenseCategory',
                target_id=category_id,
                metadata={
                    'code': category['code'],
                    'changed_fields': sorted(data),
                },
            )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('expense.category.manage')
def expense_category_deactivate(request, category_id):
    result, status = ExpenseCategoryService.deactivate(
        category_id,
        actor=request.user,
    )
    if result.get('success'):
        category = result['data']['category']
        audit(
            request,
            AuditLog.Action.EXPENSE_CATEGORY_DEACTIVATE,
            target_type='ExpenseCategory',
            target_id=category_id,
            metadata={'code': category['code'], 'is_active': False},
        )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@backoffice_required
def expenses(request):
    if request.method == 'POST':
        if denied := permission_denied_response(request, 'expense.request.create'):
            return denied
        data, error = parse_json_body(request)
        if error:
            return json_response(error)
        result, status = ExpenseService.create(actor=request.user, **data)
        return JsonResponse(result, status=status)
    view_all, denied = _expense_view_scope(request)
    if denied:
        return denied
    try:
        page = positive_int(request.GET, 'page', 1)
        per_page = positive_int(request.GET, 'per_page', 25, maximum=100)
        category_id = optional_int(request.GET, 'category_id')
        date_from = iso_date(request.GET, 'date_from')
        date_to = iso_date(request.GET, 'date_to')
    except QueryValidationError as exc:
        return _filter_error(exc)
    result, status = ExpenseService.list(
        page=page,
        per_page=per_page,
        status=request.GET.get('status'),
        category_id=category_id,
        date_from=date_from,
        date_to=date_to,
        search=request.GET.get('search'),
        actor=request.user,
        view_all=view_all,
    )
    return JsonResponse(result, status=status)


@require_GET
@backoffice_required
def expense_detail(request, expense_id):
    view_all, denied = _expense_view_scope(request)
    if denied:
        return denied
    result, status = ExpenseService.get(
        expense_id,
        actor=request.user,
        view_all=view_all,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('expense.request.approve')
def expense_approve(request, expense_id):
    result, status = ExpenseService.approve(expense_id, actor=request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('expense.request.approve')
def expense_reject(request, expense_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = ExpenseService.reject(
        expense_id,
        actor=request.user,
        reason=data.get('reason'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('expense.request.pay')
@idempotent(
    'expense.request.pay',
    required=True,
    expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def expense_pay(request, expense_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = ExpenseService.pay(
        expense_id,
        actor=request.user,
        source_account=data.get('source_account'),
        fee_uzs=data.get('fee_uzs'),
        fee_percent=data.get('fee_percent'),
        note=data.get('note', ''),
        action_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def expense_cancel(request, expense_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = ExpenseService.cancel(
        expense_id,
        actor=request.user,
        can_approve=user_has_permission(request.user, 'expense.request.approve'),
        reason=data.get('reason', ''),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('expense.request.void')
@idempotent(
    'expense.request.void',
    required=True,
    expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def expense_void(request, expense_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = ExpenseService.void(
        expense_id,
        actor=request.user,
        reason=data.get('reason'),
        action_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
    )
    return JsonResponse(result, status=status)
