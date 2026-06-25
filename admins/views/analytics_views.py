"""Operational analytics endpoints."""
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET

from admins.services.analytics_service import (
    menu_engineering, shift_performance, staff_performance,
)
from admins.services.product_analytics_service import (
    products_categories, products_overview, products_pareto, products_trends,
)
from admins.services.shift_analytics_service import (
    cashier_shift_analytics, kitchen_shift_analytics, shift_handover_report,
)
from base.models import Shift
from base.security.permissions import manager_required, pos_staff_required


def _can_view_any_shift(user):
    """Managers/admins see every shift's financials; a plain cashier may only
    see their own (these endpoints expose revenue, cash variance and receipts —
    without this check any cashier could read a coworker's handover by id)."""
    return getattr(user, 'role', None) in ('ADMIN', 'MANAGER')


@require_GET
@pos_staff_required
def shift_perf_view(request, shift_id):
    try:
        shift = Shift.objects.select_related('user').get(
            id=shift_id, is_deleted=False,
        )
    except Shift.DoesNotExist:
        return JsonResponse(
            {'success': False, 'message': 'Shift not found'}, status=404,
        )
    if not _can_view_any_shift(request.user) and shift.user_id != request.user.id:
        return JsonResponse({'success': False, 'message': 'Forbidden'}, status=403)
    return JsonResponse({'success': True, 'data': shift_performance(shift)})


@require_GET
@manager_required
def menu_engineering_view(request):
    df_str = request.GET.get('from')
    dt_str = request.GET.get('to')
    df = parse_date(df_str) if df_str else None
    dt = parse_date(dt_str) if dt_str else None
    if not df or not dt:
        return JsonResponse(
            {'success': False, 'message': 'from and to (YYYY-MM-DD) are required'},
            status=422,
        )
    if df > dt:
        return JsonResponse(
            {'success': False, 'message': 'from must be on or before to'},
            status=422,
        )

    cogs_str = request.GET.get('cogs_fraction')
    cogs = None
    if cogs_str:
        try:
            cogs = Decimal(cogs_str)
        except (InvalidOperation, TypeError):
            return JsonResponse(
                {'success': False, 'message': 'cogs_fraction must be a decimal'},
                status=422,
            )
        if cogs <= 0 or cogs >= 1:
            return JsonResponse(
                {'success': False, 'message': 'cogs_fraction must be between 0 and 1 (exclusive)'},
                status=422,
            )

    kwargs = {'cogs_fraction': cogs} if cogs is not None else {}
    return JsonResponse(
        {'success': True, 'data': menu_engineering(df, dt, **kwargs)},
    )


def _parse_range(request):
    """(date_from, date_to, error_response). Defaults `to` = `from` for a
    single-day query; both default to today when omitted."""
    from django.utils import timezone
    df_str = request.GET.get('from')
    dt_str = request.GET.get('to')
    today = timezone.localdate()
    df = parse_date(df_str) if df_str else today
    dt = parse_date(dt_str) if dt_str else (df or today)
    if df is None or dt is None:
        return None, None, JsonResponse(
            {'success': False, 'message': 'from/to must be YYYY-MM-DD'}, status=422)
    if df > dt:
        return None, None, JsonResponse(
            {'success': False, 'message': 'from must be on or before to'}, status=422)
    return df, dt, None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@require_GET
@manager_required
def cashier_shift_analytics_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    user_id = _int_or_none(request.GET.get('user_id'))
    data = cashier_shift_analytics(df, dt, user_id=user_id)
    return JsonResponse({'success': True, 'data': data})


@require_GET
@pos_staff_required
def shift_report_view(request, shift_id):
    try:
        shift = Shift.objects.select_related('user', 'shift_template', 'reconciliation').get(
            id=shift_id, is_deleted=False,
        )
    except Shift.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Shift not found'}, status=404)
    if not _can_view_any_shift(request.user) and shift.user_id != request.user.id:
        return JsonResponse({'success': False, 'message': 'Forbidden'}, status=403)
    return JsonResponse({'success': True, 'data': shift_handover_report(shift)})


@require_GET
@manager_required
def kitchen_shift_analytics_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    user_id = _int_or_none(request.GET.get('user_id'))
    role = (request.GET.get('role') or 'WAITER').upper()
    target_min = _int_or_none(request.GET.get('target_prep_minutes'))
    kwargs = {'user_id': user_id, 'role': role}
    if target_min and target_min > 0:
        kwargs['target_prep_seconds'] = target_min * 60
    data = kitchen_shift_analytics(df, dt, **kwargs)
    return JsonResponse({'success': True, 'data': data})


# ── Products dashboard (item 9): overview / categories / pareto / trends ──
# All take ?from=&to= (YYYY-MM-DD); default to the current business day.

@require_GET
@manager_required
def products_overview_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    return JsonResponse({'success': True, 'data': products_overview(df, dt)})


@require_GET
@manager_required
def products_categories_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    return JsonResponse({'success': True, 'data': products_categories(df, dt)})


@require_GET
@manager_required
def products_pareto_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    return JsonResponse({'success': True, 'data': products_pareto(df, dt)})


@require_GET
@manager_required
def products_trends_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    top_n = _int_or_none(request.GET.get('top_n')) or 5
    top_n = max(1, min(top_n, 20))
    return JsonResponse({'success': True, 'data': products_trends(df, dt, top_n=top_n)})


def _parse_range_token(request):
    """Staff dashboard range: ?range=30d|7d|90d on the business calendar. An
    explicit ?from=&to= wins; otherwise the token (default 30d) is resolved to
    [business_today - (N-1) days, business_today]."""
    from datetime import timedelta
    from base.services.business_day import business_date

    if request.GET.get('from') or request.GET.get('to'):
        return _parse_range(request)

    token = (request.GET.get('range') or '30d').strip().lower()
    days = 30
    if token.endswith('d'):
        days = _int_or_none(token[:-1]) or 30
    elif token.endswith('m'):  # months -> ~30d each
        days = (_int_or_none(token[:-1]) or 1) * 30
    days = max(1, min(days, 366))
    d_to = business_date()
    d_from = d_to - timedelta(days=days - 1)
    return d_from, d_to, None


@require_GET
@manager_required
def staff_performance_view(request):
    df, dt, err = _parse_range_token(request)
    if err:
        return err
    return JsonResponse({'success': True, 'data': staff_performance(df, dt)})
