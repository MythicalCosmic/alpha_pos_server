from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.http_validation import (
    QueryValidationError,
    iso_date,
    optional_int,
    positive_int,
    validate_period,
)
from base.models import AuditLog
from base.security.audit import audit
from base.security.idempotency import idempotent
from base.security.permissions import backoffice_permission_required
from base.services.business_day import day_window
from base.services.treasury_service import TreasuryService
from hr.services.expense_service import ExpenseService


def _filter_error(exc):
    return JsonResponse({
        'success': False,
        'code': 'FILTER_VALIDATION_ERROR',
        'message': 'One or more filters are invalid.',
        'errors': exc.errors,
    }, status=422)


@require_GET
@backoffice_permission_required('treasury.account.view')
def treasury_accounts(request):
    result, status = TreasuryService.get_accounts(actor=request.user)
    return JsonResponse(result, status=status)


@require_GET
@backoffice_permission_required('treasury.account.view')
def treasury_history(request):
    try:
        page = positive_int(request.GET, 'page', 1)
        per_page = positive_int(request.GET, 'per_page', 25, maximum=100)
        date_from = iso_date(request.GET, 'date_from')
        date_to = iso_date(request.GET, 'date_to')
        if date_from and date_to:
            validate_period(date_from, date_to)
        category_id = optional_int(request.GET, 'category_id')
        reference_id = optional_int(request.GET, 'reference_id')
        performed_by_id = optional_int(request.GET, 'performed_by_id')
    except QueryValidationError as exc:
        return _filter_error(exc)
    start_at = day_window(date_from)[0] if date_from else None
    end_at = day_window(date_to)[1] if date_to else None
    result, status = TreasuryService.history(
        account_kind=request.GET.get('account'),
        txn_type=request.GET.get('type'),
        page=page,
        per_page=per_page,
        actor=request.user,
        date_from=start_at,
        date_to=end_at,
        category_id=category_id,
        reference_type=request.GET.get('reference_type'),
        reference_id=reference_id,
        performed_by_id=performed_by_id,
        search=request.GET.get('search'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('treasury.transfer')
@idempotent(
    'treasury.transfer',
    required=True,
    expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def treasury_transfer(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = TreasuryService.transfer(
        from_kind=data.get('from'),
        to_kind=data.get('to'),
        amount=data.get('amount_uzs', data.get('amount')),
        fee=data.get('fee_uzs', data.get('fee', 0)),
        performed_by=request.user,
        description=data.get('description', ''),
        command_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
    )
    if result.get('success'):
        payload = result.get('data', {})
        audit(
            request,
            AuditLog.Action.TREASURY_TRANSFER,
            target_type='TreasuryAccount',
            metadata={
                'from': data.get('from'),
                'to': data.get('to'),
                'amount': payload.get('amount'),
                'fee': payload.get('fee'),
            },
        )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('expense.direct.pay')
@idempotent(
    'treasury.expense.direct',
    required=True,
    expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def treasury_expense(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    source = data.get('source_account', data.get('account'))
    result, status = ExpenseService.direct_pay(
        actor=request.user,
        action_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
        category_id=data.get('category_id'),
        amount_uzs=data.get('amount_uzs', data.get('amount')),
        requested_source=source,
        source_account=source,
        fee_uzs=data.get(
            'fee_uzs', data.get('fee', data.get('commission', 0))
        ),
        fee_percent=data.get('fee_percent'),
        description=data.get('description', ''),
        note=data.get('note', ''),
        expense_date=data.get('expense_date'),
        receipt_number=data.get('receipt_number', ''),
    )
    if result.get('success'):
        payload = result.get('data', {})
        audit(
            request,
            AuditLog.Action.TREASURY_EXPENSE,
            target_type='Expense',
            target_id=payload.get('expense_id'),
            metadata={
                'source_account': source,
                'category_id': data.get('category_id'),
            },
        )
    return JsonResponse(result, status=status)
