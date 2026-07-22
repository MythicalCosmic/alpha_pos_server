"""Operational analytics endpoints."""
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET

from admins.services.analytics_service import (
    menu_engineering, shift_performance, staff_performance,
)
from admins.services.comparison_service import compare_periods
from admins.services.product_analytics_service import (
    products_affinity, products_categories, products_overview, products_pareto,
    products_trends,
)
from admins.services.shift_analytics_service import (
    cashier_shift_analytics, kitchen_shift_analytics, shift_handover_report,
)
from admins.services.shift_window import effective_shift_end
from admins.services.workbook_export_service import build_shift_report_workbook
from admins.views.export_response import xlsx_attachment
from base.models import Shift
from base.security.permissions import manager_required, pos_staff_required
from base.security.rate_limit import rate_limit


MAX_SHIFT_EXPORT_RECEIPTS = 5000


def _can_view_any_shift(user):
    """Managers/admins see every shift's financials; a plain cashier may only
    see their own (these endpoints expose revenue, cash variance and receipts —
    without this check any cashier could read a coworker's handover by id)."""
    return getattr(user, 'role', None) in ('ADMIN', 'MANAGER')


def _visible_shift(request, shift_id):
    """Load one shift and enforce the shared report/performance ownership rule."""
    try:
        shift = Shift.objects.select_related(
            'user', 'shift_template', 'reconciliation',
        ).get(id=shift_id, is_deleted=False)
    except Shift.DoesNotExist:
        return None, JsonResponse(
            {'success': False, 'message': 'Shift not found'}, status=404,
        )
    if not _can_view_any_shift(request.user) and shift.user_id != request.user.id:
        return None, JsonResponse(
            {'success': False, 'message': 'Forbidden'}, status=403,
        )
    return shift, None


def _shift_export_receipt_count(shift):
    """Cheap guard before materializing an anomalously large XLSX report."""
    from base.models import Order

    end = effective_shift_end(shift)
    return Order.objects.filter(
        is_deleted=False,
        cashier_id=shift.user_id,
        branch_id=shift.branch_id,
        created_at__gte=shift.start_time,
        created_at__lt=end,
    ).count()


@require_GET
@pos_staff_required
def shift_perf_view(request, shift_id):
    shift, error = _visible_shift(request, shift_id)
    if error is not None:
        return error
    return JsonResponse({'success': True, 'data': shift_performance(shift)})


@require_GET
@manager_required
def menu_engineering_view(request):
    df_str = request.GET.get('from')
    dt_str = request.GET.get('to')
    has_custom = any(request.GET.get(name) for name in (
        'datetime_from', 'datetime_to', 'from_at', 'to_at',
    ))
    df = parse_date(df_str) if df_str else None
    dt = parse_date(dt_str) if dt_str else None
    if not has_custom and (not df or not dt):
        return JsonResponse(
            {'success': False, 'message': 'from and to (YYYY-MM-DD) are required'},
            status=422,
        )
    if not has_custom and df > dt:
        return JsonResponse(
            {'success': False, 'message': 'from must be on or before to'},
            status=422,
        )
    if has_custom:
        df, dt, error = _parse_range(request)
        if error:
            return error

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

    window, error = _window_or_error(request, df, dt)
    if error:
        return error
    kwargs = {'cogs_fraction': cogs} if cogs is not None else {}
    return JsonResponse(
        {'success': True, 'data': menu_engineering(
            window.date_from, window.date_to, window=window, **kwargs,
        )},
    )


def _parse_range(request):
    """(date_from, date_to, error_response). Defaults `to` = `from` for a
    single-day query; both default to today when omitted."""
    from base.services.business_day import business_date
    # ISO datetimes have canonical precedence, even when stale/invalid legacy
    # date params are also present during a rolling FE deployment.
    if any(request.GET.get(name) for name in (
        'datetime_from', 'datetime_to', 'from_at', 'to_at',
    )):
        from base.services.business_day import resolve_reporting_window
        try:
            window = resolve_reporting_window(
                datetime_from=request.GET.get('datetime_from'),
                datetime_to=request.GET.get('datetime_to'),
                from_at=request.GET.get('from_at'),
                to_at=request.GET.get('to_at'),
            )
        except ValueError as exc:
            return None, None, JsonResponse(
                {'success': False, 'message': str(exc),
                 'errors': {'range': str(exc)}}, status=422,
            )
        return window.date_from, window.date_to, None

    df_str = request.GET.get('from')
    dt_str = request.GET.get('to')
    # Default "today" = current BUSINESS day (03:00 cutover), matching
    # _parse_range_token and every business-day report, not the calendar date.
    today = business_date()
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


def _resolved_window(request, date_from, date_to):
    """Resolve canonical ISO datetimes, aliases, or legacy date/clock inputs."""
    from base.services.business_day import resolve_reporting_window
    return resolve_reporting_window(
        date_from,
        date_to,
        datetime_from=request.GET.get('datetime_from'),
        datetime_to=request.GET.get('datetime_to'),
        from_at=request.GET.get('from_at'),
        to_at=request.GET.get('to_at'),
        tod_from=request.GET.get('tod_from'),
        tod_to=request.GET.get('tod_to'),
    )


def _window_or_error(request, date_from, date_to):
    try:
        return _resolved_window(request, date_from, date_to), None
    except ValueError as exc:
        return None, JsonResponse(
            {'success': False, 'message': str(exc),
             'errors': {'range': str(exc)}},
            status=422,
        )


@require_GET
@manager_required
def cashier_shift_analytics_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    user_id = _int_or_none(request.GET.get('user_id'))
    window, err = _window_or_error(request, df, dt)
    if err:
        return err
    data = cashier_shift_analytics(
        window.date_from, window.date_to, user_id=user_id, window=window,
    )
    return JsonResponse({'success': True, 'data': data})


@require_GET
@pos_staff_required
def shift_report_view(request, shift_id):
    shift, error = _visible_shift(request, shift_id)
    if error is not None:
        return error
    return JsonResponse({'success': True, 'data': shift_handover_report(shift)})


@require_GET
@rate_limit('admin_shift_report_export', max_attempts=10, window=60)
@pos_staff_required
def shift_report_export_view(request, shift_id):
    """Download the canonical handover report without recomputing its figures."""
    shift, error = _visible_shift(request, shift_id)
    if error is not None:
        return error
    receipt_count = _shift_export_receipt_count(shift)
    if receipt_count > MAX_SHIFT_EXPORT_RECEIPTS:
        return JsonResponse(
            {
                'success': False,
                'message': (
                    'Shift report is too large for an immediate download; '
                    'contact support for an archive export.'
                ),
                'receipt_count': receipt_count,
                'max_receipts': MAX_SHIFT_EXPORT_RECEIPTS,
            },
            status=413,
        )
    report = shift_handover_report(shift)
    payload = build_shift_report_workbook(report)
    local_date = timezone.localtime(shift.start_time).date().isoformat()
    filename = f'alpha-pos-shift-{shift.id}-report-{local_date}.xlsx'
    return xlsx_attachment(
        payload,
        filename,
        count=report.get('receipt_count', 0),
    )


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
    window, err = _window_or_error(request, df, dt)
    if err:
        return err
    data = kitchen_shift_analytics(
        window.date_from, window.date_to, window=window, **kwargs,
    )
    return JsonResponse({'success': True, 'data': data})


# ── Products dashboard (item 9): overview / categories / pareto / trends ──
# All take ?from=&to= (YYYY-MM-DD); default to the current business day.

@require_GET
@manager_required
def products_overview_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    window, err = _window_or_error(request, df, dt)
    if err:
        return err
    return JsonResponse({'success': True, 'data': products_overview(
        window.date_from, window.date_to, window=window,
    )})


@require_GET
@manager_required
def products_categories_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    window, err = _window_or_error(request, df, dt)
    if err:
        return err
    return JsonResponse({'success': True, 'data': products_categories(
        window.date_from, window.date_to, window=window,
    )})


@require_GET
@manager_required
def products_pareto_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    window, err = _window_or_error(request, df, dt)
    if err:
        return err
    return JsonResponse({'success': True, 'data': products_pareto(
        window.date_from, window.date_to, window=window,
    )})


@require_GET
@manager_required
def products_trends_view(request):
    df, dt, err = _parse_range(request)
    if err:
        return err
    top_n = _int_or_none(request.GET.get('top_n')) or 5
    top_n = max(1, min(top_n, 20))
    window, err = _window_or_error(request, df, dt)
    if err:
        return err
    return JsonResponse({'success': True, 'data': products_trends(
        window.date_from, window.date_to, top_n=top_n, window=window,
    )})


def _parse_range_token(request):
    """Staff dashboard range: ?range=30d|7d|90d on the business calendar. An
    explicit ?from=&to= wins; otherwise the token (default 30d) is resolved to
    [business_today - (N-1) days, business_today]."""
    from datetime import timedelta
    from base.services.business_day import business_date

    if request.GET.get('from') or request.GET.get('to'):
        return _parse_range(request)

    token = (request.GET.get('range') or '30d').strip().lower()
    if token == 'today':
        d = business_date()
        return d, d, None
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
    window, err = _window_or_error(request, df, dt)
    if err:
        return err
    return JsonResponse({'success': True, 'data': staff_performance(
        window.date_from, window.date_to, window=window,
    )})


@require_GET
@manager_required
def products_affinity_view(request):
    """GET /analytics/products/affinity?range=30d&limit=10 — market-basket
    co-occurrence for the Products chord chart (item 16)."""
    df, dt, err = _parse_range_token(request)
    if err:
        return err
    limit = _int_or_none(request.GET.get('limit')) or 10
    window, err = _window_or_error(request, df, dt)
    if err:
        return err
    return JsonResponse({'success': True, 'data': products_affinity(
        window.date_from, window.date_to, limit=limit, window=window,
    )})


@require_GET
@manager_required
def comparison_view(request):
    """GET /analytics/comparison — Compare-Periods page. Two ranges (A primary,
    B baseline) with every sales metric side by side + deltas.

    ?a_start=&a_end=&b_start=&b_end= (YYYY-MM-DD, all required)
    &granularity=day|week|month  &branch_id=<optional>  &tz=Asia/Tashkent
    """
    parts = {}
    for name in ('a_start', 'a_end', 'b_start', 'b_end'):
        raw = request.GET.get(name)
        d = parse_date(raw) if raw else None
        if d is None:
            return JsonResponse(
                {'success': False,
                 'message': f'{name} is required (YYYY-MM-DD)'}, status=422)
        parts[name] = d
    if parts['a_start'] > parts['a_end'] or parts['b_start'] > parts['b_end']:
        return JsonResponse(
            {'success': False, 'message': 'each range start must be on or before its end'},
            status=422)

    granularity = (request.GET.get('granularity') or 'day').strip().lower()
    if granularity not in ('day', 'week', 'month'):
        return JsonResponse(
            {'success': False, 'message': 'granularity must be day, week or month'},
            status=422)

    branch_id = (request.GET.get('branch_id') or '').strip() or None
    tz_name = (request.GET.get('tz') or 'Asia/Tashkent').strip()

    data = compare_periods(
        parts['a_start'], parts['a_end'], parts['b_start'], parts['b_end'],
        granularity=granularity, branch_id=branch_id, tz_name=tz_name,
    )
    return JsonResponse({'success': True, 'data': data})
