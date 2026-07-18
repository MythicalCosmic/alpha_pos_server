"""Server-side shift services.

Mutation/detail behavior remains owned by ``core.shifts.service``.  The cloud
admin list adds a richer filter/pagination contract and global KPIs without
changing the shared POS core used by the desktop edition.
"""
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.models import Shift
from core.shifts.service import (
    ShiftService as CoreShiftService,
    ShiftTemplateService,  # noqa: F401 - re-exported for shift_views
    _scope_shift_queryset,
)


_SHIFT_SORT_FIELDS = {
    'id', '-id',
    'start_time', '-start_time',
    'end_time', '-end_time',
    'status', '-status',
    'total_orders', '-total_orders',
    'total_revenue', '-total_revenue',
    'cash_collected', '-cash_collected',
    'user__first_name', '-user__first_name',
}


def _csv_values(value):
    if not value:
        return []
    return [
        item.strip().strip('"\'').upper()
        for item in str(value).strip().strip('[]').split(',')
        if item.strip().strip('"\'')
    ]


def _money(value):
    return str(Decimal(value or 0).quantize(Decimal('0.01')))


class ShiftService(CoreShiftService):
    @staticmethod
    def _filtered_admin_queryset(*, user_id=None, status=None,
                                 date_from=None, date_to=None,
                                 live_only=False, closed_only=False,
                                 datetime_from=None, datetime_to=None,
                                 from_at=None, to_at=None,
                                 tod_from=None, tod_to=None, actor=None):
        if live_only and closed_only:
            raise ValueError('live_only and closed_only cannot both be true')

        qs = Shift.objects.filter(is_deleted=False).select_related(
            'user', 'shift_template',
            'reconciliation', 'reconciliation__reconciled_by',
        )
        qs = _scope_shift_queryset(qs, actor)

        if user_id not in (None, ''):
            try:
                user_id = int(user_id)
            except (TypeError, ValueError):
                raise ValueError('cashier_id must be an integer')
            qs = qs.filter(user_id=user_id)

        requested_statuses = _csv_values(status)
        if requested_statuses:
            valid = set(Shift.Status.values)
            invalid = [value for value in requested_statuses if value not in valid]
            if invalid:
                raise ValueError(
                    'status must be ACTIVE, ENDED, COMPLETED, or ABANDONED'
                )
            qs = qs.filter(status__in=requested_statuses)

        has_window = any(value not in (None, '') for value in (
            date_from, date_to, tod_from, tod_to,
            datetime_from, datetime_to, from_at, to_at,
        ))
        window = None
        if has_window:
            from base.services.business_day import resolve_reporting_window
            window = resolve_reporting_window(
                date_from, date_to,
                tod_from=tod_from, tod_to=tod_to,
                datetime_from=datetime_from, datetime_to=datetime_to,
                from_at=from_at, to_at=to_at,
            )
            qs = window.filter(qs, 'start_time')
        parsed_from = window.date_from if window else None
        parsed_to = window.date_to if window else None

        if live_only:
            qs = qs.filter(status=Shift.Status.ACTIVE, end_time__isnull=True)
        elif closed_only:
            qs = qs.exclude(
                status=Shift.Status.ACTIVE,
                end_time__isnull=True,
            )
        return qs, requested_statuses, parsed_from, parsed_to, user_id, window

    @staticmethod
    def _global_summary(filtered, *, now):
        """Aggregate the complete filtered population, before sort/page."""
        base = filtered.order_by()
        total = base.count()
        live_rows = list(base.filter(
            status=Shift.Status.ACTIVE,
            end_time__isnull=True,
        ))
        closed = base.exclude(
            status=Shift.Status.ACTIVE,
            end_time__isnull=True,
        )
        closed_totals = closed.aggregate(
            orders=Coalesce(Sum('total_orders'), 0),
            revenue=Coalesce(
                Sum('total_revenue'), Decimal('0.00'),
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            cash=Coalesce(
                Sum('cash_collected'), Decimal('0.00'),
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
        )
        orders = int(closed_totals['orders'] or 0)
        revenue = Decimal(closed_totals['revenue'] or 0)
        cash = Decimal(closed_totals['cash'] or 0)
        for shift in live_rows:
            live_orders, live_revenue, live_cash = CoreShiftService._live_totals(
                shift, now,
            )
            orders += int(live_orders or 0)
            revenue += Decimal(live_revenue or 0)
            cash += Decimal(live_cash or 0)

        by_status = {value: 0 for value in Shift.Status.values}
        for row in base.values('status').annotate(count=Count('id')):
            by_status[row['status']] = row['count']

        reconciled = base.filter(
            reconciliation__is_deleted=False,
        ).count()
        from cashbox.models import ShiftPaymentTotal
        tender_rows = (
            ShiftPaymentTotal.objects.filter(
                is_deleted=False,
                shift__in=closed,
                branch_id=F('shift__branch_id'),
            )
            .values('method')
            .annotate(
                expected=Coalesce(
                    Sum('expected_amount'), Decimal('0.00'),
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
                confirmed=Coalesce(
                    Sum('confirmed_amount'), Decimal('0.00'),
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
            )
            .order_by('method')
        )
        expected_totals = {
            row['method']: Decimal(row['expected'] or 0) for row in tender_rows
        }
        confirmed_totals = {
            row['method']: Decimal(row['confirmed'] or 0) for row in tender_rows
        }
        # ACTIVE shifts have no frozen ShiftPaymentTotal rows yet. Derive their
        # tender split in one batched pass so global summary money remains
        # consistent with the visible live rows instead of silently omitting it.
        live_extras = CoreShiftService._batch_list_extras(live_rows, now=now)
        for shift in live_rows:
            for method, amount in (
                live_extras.get(shift.id, {}).get('expected_by_tender', {})
            ).items():
                expected_totals[method] = (
                    expected_totals.get(method, Decimal('0.00'))
                    + Decimal(amount)
                )
        expected_by_tender = {
            method: _money(amount)
            for method, amount in sorted(expected_totals.items())
        }
        confirmed_by_tender = {
            method: _money(amount)
            for method, amount in sorted(confirmed_totals.items())
        }
        total_expected = sum(
            (Decimal(value) for value in expected_by_tender.values()),
            Decimal('0.00'),
        )
        total_confirmed = sum(
            (Decimal(value) for value in confirmed_by_tender.values()),
            Decimal('0.00'),
        )
        return {
            'shift_count': total,
            'live_count': len(live_rows),
            'closed_count': total - len(live_rows),
            'reconciled_count': reconciled,
            'unreconciled_count': total - reconciled,
            'by_status': by_status,
            'total_orders': orders,
            'total_revenue': _money(revenue),
            'cash_collected': _money(cash),
            'expected_by_tender': expected_by_tender,
            'confirmed_by_tender': confirmed_by_tender,
            'total_expected_to_receive': _money(total_expected),
            'total_confirmed_received': _money(total_confirmed),
            'average_revenue_per_shift': _money(
                revenue / total if total else Decimal('0')
            ),
        }

    @staticmethod
    def list(page=1, per_page=20, user_id=None, status=None, date_from=None,
             date_to=None, live_only=False, closed_only=False,
             order_by='-start_time', datetime_from=None, datetime_to=None,
             from_at=None, to_at=None, tod_from=None, tod_to=None, actor=None):
        try:
            filtered, statuses, parsed_from, parsed_to, parsed_user, window = (
                ShiftService._filtered_admin_queryset(
                    user_id=user_id,
                    status=status,
                    date_from=date_from,
                    date_to=date_to,
                    live_only=live_only,
                    closed_only=closed_only,
                    datetime_from=datetime_from,
                    datetime_to=datetime_to,
                    from_at=from_at,
                    to_at=to_at,
                    tod_from=tod_from,
                    tod_to=tod_to,
                    actor=actor,
                )
            )
        except ValueError as exc:
            return ServiceResponse.validation_error({'filters': str(exc)})

        if order_by not in _SHIFT_SORT_FIELDS:
            order_by = '-start_time'
        now = timezone.now()
        summary = ShiftService._global_summary(filtered, now=now)

        paginator = Paginator(filtered.order_by(order_by, '-id'), per_page)
        page_obj = paginator.get_page(page)
        shifts = list(page_obj.object_list)
        extras = CoreShiftService._batch_list_extras(shifts, now=now)
        rows = [
            CoreShiftService._serialize_shift(
                shift,
                extras=extras.get(shift.id),
                now=now,
            )
            for shift in shifts
        ]
        pagination = {
            'page': page_obj.number,
            'current_page': page_obj.number,
            'per_page': per_page,
            'total': paginator.count,
            'total_shifts': paginator.count,
            'pages': paginator.num_pages,
            'total_pages': paginator.num_pages,
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
        }
        return ServiceResponse.success(data={
            'shifts': rows,
            'pagination': pagination,
            # Both names are supplied because deployed admin clients have used
            # each spelling. They reference the same global, unpaginated values.
            'summary': summary,
            'stats': summary,
            'filters': {
                'cashier_id': parsed_user,
                'statuses': statuses or None,
                'date_from': parsed_from.isoformat() if parsed_from else None,
                'date_to': parsed_to.isoformat() if parsed_to else None,
                'start_at': window.start_at.isoformat() if window else None,
                'end_at': window.end_at.isoformat() if window else None,
                'range_mode': window.mode if window else None,
                'live_only': bool(live_only),
                'closed_only': bool(closed_only),
                'order_by': order_by,
            },
        })
