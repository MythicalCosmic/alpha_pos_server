"""Server-side shift services.

Mutation/detail behavior remains owned by ``core.shifts.service``.  The cloud
admin list adds a richer filter/pagination contract and global KPIs without
changing the shared POS core used by the desktop edition.
"""
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date

from base.helpers.response import ServiceResponse
from base.models import Shift
from core.shifts.service import (
    ShiftService as CoreShiftService,
    ShiftTemplateService,
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
                                 live_only=False, closed_only=False):
        if live_only and closed_only:
            raise ValueError('live_only and closed_only cannot both be true')

        qs = Shift.objects.filter(is_deleted=False).select_related(
            'user', 'shift_template',
            'reconciliation', 'reconciliation__reconciled_by',
        )

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

        parsed_from = parse_date(str(date_from)) if date_from else None
        parsed_to = parse_date(str(date_to)) if date_to else None
        if date_from and parsed_from is None:
            raise ValueError('date_from must be YYYY-MM-DD')
        if date_to and parsed_to is None:
            raise ValueError('date_to must be YYYY-MM-DD')
        if parsed_from and parsed_to and parsed_from > parsed_to:
            raise ValueError('date_from must be on or before date_to')

        from base.services.business_day import range_window
        if parsed_from:
            lower, _ = range_window(parsed_from, parsed_from)
            qs = qs.filter(start_time__gte=lower)
        if parsed_to:
            _, upper = range_window(parsed_to, parsed_to)
            qs = qs.filter(start_time__lt=upper)

        if live_only:
            qs = qs.filter(status=Shift.Status.ACTIVE, end_time__isnull=True)
        elif closed_only:
            qs = qs.exclude(
                status=Shift.Status.ACTIVE,
                end_time__isnull=True,
            )
        return qs, requested_statuses, parsed_from, parsed_to, user_id

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
            'average_revenue_per_shift': _money(
                revenue / total if total else Decimal('0')
            ),
        }

    @staticmethod
    def list(page=1, per_page=20, user_id=None, status=None, date_from=None,
             date_to=None, live_only=False, closed_only=False,
             order_by='-start_time'):
        try:
            filtered, statuses, parsed_from, parsed_to, parsed_user = (
                ShiftService._filtered_admin_queryset(
                    user_id=user_id,
                    status=status,
                    date_from=date_from,
                    date_to=date_to,
                    live_only=live_only,
                    closed_only=closed_only,
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
                'live_only': bool(live_only),
                'closed_only': bool(closed_only),
                'order_by': order_by,
            },
        })
