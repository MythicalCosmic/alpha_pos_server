"""SAFE / BANK treasury endpoints: balances, transfers, expenses, ledger."""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from base.helpers.request import parse_json_body, validate_pagination
from base.helpers.response import json_response
from base.security.permissions import manager_required, pos_staff_required
from base.security.idempotency import idempotent
from base.security.audit import audit
from base.models import AuditLog
from base.services.treasury_service import TreasuryService


@csrf_exempt
@require_GET
@pos_staff_required
def treasury_accounts(request):
    # Read-only balances — cashiers see SAFE/BANK so they know what they spend from.
    result, status_code = TreasuryService.get_accounts()
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@pos_staff_required
def treasury_history(request):
    # Cashiers can view the ledger (incl. the expenses they file); only
    # transfers (below) stay manager-only.
    page, per_page = validate_pagination(request)
    result, status_code = TreasuryService.history(
        account_kind=request.GET.get('account'),
        txn_type=request.GET.get('type'),
        page=page, per_page=per_page,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@manager_required
@idempotent('treasury.transfer')
def treasury_transfer(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status_code = TreasuryService.transfer(
        from_kind=data.get('from'),
        to_kind=data.get('to'),
        amount=data.get('amount'),
        fee=data.get('fee', 0),
        performed_by=request.user,
        description=data.get('description', ''),
    )
    if result.get('success'):
        payload = result.get('data', {})
        audit(
            request, AuditLog.Action.TREASURY_TRANSFER,
            target_type='TreasuryAccount',
            metadata={
                'from': data.get('from'), 'to': data.get('to'),
                'amount': payload.get('amount'), 'fee': payload.get('fee'),
            },
        )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@pos_staff_required
@idempotent('treasury.expense')
def treasury_expense(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status_code = TreasuryService.record_expense(
        account_kind=data.get('account'),
        amount=data.get('amount'),
        category=data.get('category', ''),
        description=data.get('description', ''),
        performed_by=request.user,
        fee=data.get('fee', 0) or data.get('commission', 0),
    )
    if result.get('success'):
        txn = result.get('data', {}).get('transaction', {})
        audit(
            request, AuditLog.Action.TREASURY_EXPENSE,
            target_type='TreasuryTransaction', target_id=txn.get('id'),
            metadata={
                'account': data.get('account'), 'amount': txn.get('delta'),
                'category': data.get('category'),
            },
        )
    return JsonResponse(result, status=status_code)
