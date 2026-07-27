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

        by_status = {value: 0 for value in Shift.Status.values}
        for row in base.values('status').annotate(count=Count('id')):
            by_status[row['status']] = row['count']

        reconciled_qs = base.filter(
            reconciliation__is_deleted=False,
        )
        reconciled_shift_ids = set(
            reconciled_qs.values_list('id', flat=True)
        )
        posted_reconciled_shift_ids = set(
            reconciled_qs.filter(
                reconciliation__treasury_posted_at__isnull=False,
            ).values_list('id', flat=True)
        )
        reconciled = len(reconciled_shift_ids)
        from cashbox.models import PAYMENT_METHODS, ShiftPaymentTotal
        settlement_rows = ShiftPaymentTotal.objects.filter(
            is_deleted=False,
            shift__in=closed,
            branch_id=F('shift__branch_id'),
        )
        closed_rows = list(closed)
        evidence_rows = [*closed_rows, *live_rows]
        evidence_extras = CoreShiftService._batch_list_extras(
            evidence_rows, now=now,
        )
        unavailable_shift_ids = {
            shift.id
            for shift in evidence_rows
            if evidence_extras.get(shift.id, {}).get(
                'tender_totals_source',
            ) == 'UNAVAILABLE'
        }
        complete_frozen_shift_ids = {
            shift.id
            for shift in closed_rows
            if evidence_extras.get(shift.id, {}).get(
                'frozen_tender_evidence_complete',
            ) is True
        }
        settlement_shift_ids = set(
            settlement_rows.values_list('shift_id', flat=True).distinct()
        )
        posted_methods_by_shift = {
            shift_id: set() for shift_id in posted_reconciled_shift_ids
        }
        for shift_id, method in settlement_rows.filter(
            shift_id__in=posted_reconciled_shift_ids,
        ).values_list('shift_id', 'method'):
            posted_methods_by_shift.setdefault(shift_id, set()).add(method)
        required_tender_methods = set(PAYMENT_METHODS)
        posted_full_method_shift_ids = {
            shift_id
            for shift_id, methods in posted_methods_by_shift.items()
            if methods == required_tender_methods
        }
        incomplete_posted_all_tender_shift_ids = (
            posted_reconciled_shift_ids - posted_full_method_shift_ids
        )
        tender_rows = (
            settlement_rows.filter(
                shift_id__in=posted_reconciled_shift_ids,
                method__in=PAYMENT_METHODS,
            ).exclude(method='CASH')
            .values('method')
            .annotate(
                confirmed=Coalesce(
                    Sum('confirmed_amount'), Decimal('0.00'),
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
            )
            .order_by('method')
        )
        confirmed_totals = {
            row['method']: Decimal(row['confirmed'] or 0) for row in tender_rows
        }
        confirmed_totals['CASH'] = Decimal(
            reconciled_qs.aggregate(
                cash=Coalesce(
                    Sum('reconciliation__actual_cash'),
                    Decimal('0.00'),
                    output_field=DecimalField(
                        max_digits=20, decimal_places=2,
                    ),
                ),
            )['cash'] or 0
        )
        legacy_cash_only_reconciliation_count = (
            len(reconciled_shift_ids - posted_reconciled_shift_ids)
        )
        confirmed_all_tenders_complete = (
            legacy_cash_only_reconciliation_count == 0
            and not incomplete_posted_all_tender_shift_ids
        )
        derived_closed_rows = [
            shift for shift in closed_rows
            if (
                shift.id not in complete_frozen_shift_ids
                and shift.id not in reconciled_shift_ids
            )
        ]
        partial_frozen_count = len(
            settlement_shift_ids - complete_frozen_shift_ids
        )
        frozen_closed_count = len(complete_frozen_shift_ids)
        evidence_issue_counts = {}
        discrepancy_shift_ids = set()
        incomplete_attribution_shift_ids = set()
        incomplete_cash_shift_ids = set()
        incomplete_noncash_shift_ids = set()
        incomplete_all_tenders_shift_ids = set()
        absolute_unattributed = Decimal('0.00')
        unattributed_evidence_count = 0
        for shift in evidence_rows:
            extras = evidence_extras.get(shift.id, {})
            for issue in extras.get('frozen_tender_evidence_issues', []):
                if (
                    shift.status == Shift.Status.ACTIVE
                    and not shift.end_time
                    and issue == 'NO_FROZEN_TENDER_ROWS'
                ):
                    continue
                evidence_issue_counts[issue] = (
                    evidence_issue_counts.get(issue, 0) + 1
                )
            if (
                shift.id in settlement_shift_ids
                and extras.get('frozen_tender_discrepancies')
            ):
                discrepancy_shift_ids.add(shift.id)
            if extras.get('tender_attribution_complete') is not True:
                incomplete_attribution_shift_ids.add(shift.id)
            if extras.get('cash_to_receive_complete') is not True:
                incomplete_cash_shift_ids.add(shift.id)
            if extras.get('noncash_to_receive_complete') is not True:
                incomplete_noncash_shift_ids.add(shift.id)
            if extras.get('all_tenders_to_receive_complete') is not True:
                incomplete_all_tenders_shift_ids.add(shift.id)
            unattributed = extras.get('unattributed_expected_amount')
            if unattributed is not None:
                absolute_unattributed += abs(Decimal(unattributed))
            event_count = extras.get('unattributed_evidence_count')
            if event_count is not None:
                unattributed_evidence_count += int(event_count)
        # The batch pass already read and bucketed all live order/payment/refund
        # evidence. Reuse its private totals instead of issuing several queries
        # per live shift here and again while serializing the page.
        for shift in live_rows:
            extras = evidence_extras.get(shift.id, {})
            live_totals = extras.get('_live_totals')
            if (
                extras.get('tender_totals_source') == 'UNAVAILABLE'
                or live_totals is None
            ):
                # Do not retry a failed evidence pass and accidentally turn a
                # partial second snapshot into an apparently complete summary.
                continue
            live_orders = live_totals['total_orders']
            live_revenue = live_totals['total_revenue']
            live_cash = live_totals['cash_collected']
            orders += int(live_orders or 0)
            revenue += Decimal(live_revenue or 0)
            cash += Decimal(live_cash or 0)
        # Aggregate every shift from the core verdict. Trusted frozen rows keep
        # their immutable amounts; incomplete/mismatched rows have already
        # failed closed to the canonical derived map (including UNKNOWN).
        expected_totals = {}
        for shift in evidence_rows:
            for method, amount in (
                evidence_extras.get(shift.id, {}).get(
                    'expected_by_tender', {}
                )
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
        cash_to_receive = Decimal(
            expected_by_tender.get('CASH', '0.00'),
        )
        noncash_to_receive = sum(
            (
                Decimal(value)
                for method, value in expected_by_tender.items()
                if method not in {'CASH', 'UNKNOWN'}
            ),
            Decimal('0.00'),
        )
        unknown_expected = Decimal(
            expected_by_tender.get('UNKNOWN', '0.00')
        )
        financial_totals_complete = not unavailable_shift_ids
        cash_to_receive_complete = (
            financial_totals_complete and not incomplete_cash_shift_ids
        )
        noncash_to_receive_complete = (
            financial_totals_complete and not incomplete_noncash_shift_ids
        )
        all_tenders_to_receive_complete = (
            financial_totals_complete
            and not incomplete_all_tenders_shift_ids
        )
        settlement_totals_complete = all_tenders_to_receive_complete

        # Outstanding handover money is only ENDED shifts with no manager
        # reconciliation. ACTIVE, reconciled COMPLETED, and ABANDONED rows must
        # never inflate a "cash still to receive" KPI.
        awaiting_rows = [
            shift for shift in closed_rows
            if (
                shift.status == Shift.Status.ENDED
                and shift.id not in reconciled_shift_ids
            )
        ]
        awaiting_unavailable_ids = {
            shift.id for shift in awaiting_rows
            if evidence_extras.get(shift.id, {}).get(
                'tender_totals_source',
            ) == 'UNAVAILABLE'
        }
        awaiting_incomplete_cash_ids = {
            shift.id for shift in awaiting_rows
            if evidence_extras.get(shift.id, {}).get(
                'cash_to_receive_complete',
            ) is not True
        }
        awaiting_incomplete_noncash_ids = {
            shift.id for shift in awaiting_rows
            if evidence_extras.get(shift.id, {}).get(
                'noncash_to_receive_complete',
            ) is not True
        }
        awaiting_incomplete_all_tenders_ids = {
            shift.id for shift in awaiting_rows
            if evidence_extras.get(shift.id, {}).get(
                'all_tenders_to_receive_complete',
            ) is not True
        }
        awaiting_expected_totals = {}
        for shift in awaiting_rows:
            for method, amount in (
                evidence_extras.get(shift.id, {}).get(
                    'expected_by_tender', {}
                )
            ).items():
                awaiting_expected_totals[method] = (
                    awaiting_expected_totals.get(
                        method, Decimal('0.00'),
                    )
                    + Decimal(amount)
                )
        awaiting_expected_by_tender = {
            method: _money(amount)
            for method, amount in sorted(awaiting_expected_totals.items())
        }
        awaiting_total = sum(
            (
                Decimal(value)
                for value in awaiting_expected_by_tender.values()
            ),
            Decimal('0.00'),
        )
        awaiting_cash = Decimal(
            awaiting_expected_by_tender.get('CASH', '0.00')
        )
        awaiting_noncash = sum(
            (
                Decimal(value)
                for method, value in awaiting_expected_by_tender.items()
                if method not in {'CASH', 'UNKNOWN'}
            ),
            Decimal('0.00'),
        )
        awaiting_totals_available = (
            not awaiting_unavailable_ids
            and not awaiting_incomplete_all_tenders_ids
        )
        awaiting_cash_complete = (
            awaiting_totals_available
            and not awaiting_incomplete_cash_ids
        )
        awaiting_noncash_complete = (
            awaiting_totals_available
            and not awaiting_incomplete_noncash_ids
        )
        return {
            'shift_count': total,
            'live_count': len(live_rows),
            'closed_count': total - len(live_rows),
            'reconciled_count': reconciled,
            'unreconciled_count': total - reconciled,
            'unreconciled_count_scope':
                'ALL_FILTERED_WITHOUT_RECONCILIATION',
            'by_status': by_status,
            'total_orders': (
                orders if financial_totals_complete else None
            ),
            'total_revenue': (
                _money(revenue) if financial_totals_complete else None
            ),
            'cash_collected': (
                _money(cash) if financial_totals_complete else None
            ),
            'expected_by_tender': expected_by_tender,
            'confirmed_by_tender': confirmed_by_tender,
            # Explicit settlement semantics for clients: physical banknotes
            # must be compared with cash_to_receive, never the legacy
            # all-tender total_expected_to_receive field.
            'cash_to_receive': (
                _money(cash_to_receive)
                if cash_to_receive_complete else None
            ),
            'cash_to_receive_scope': 'ALL_FILTERED_SHIFTS',
            'cash_to_receive_complete': cash_to_receive_complete,
            'noncash_to_receive': (
                _money(noncash_to_receive)
                if noncash_to_receive_complete else None
            ),
            'noncash_to_receive_complete': noncash_to_receive_complete,
            'all_tenders_to_receive': (
                _money(total_expected)
                if settlement_totals_complete else None
            ),
            'all_tenders_to_receive_complete':
                all_tenders_to_receive_complete,
            'total_expected_to_receive_scope': 'ALL_TENDERS',
            'total_expected_to_receive': (
                _money(total_expected)
                if settlement_totals_complete else None
            ),
            'settlement_totals_complete': settlement_totals_complete,
            'financial_totals_complete': financial_totals_complete,
            'awaiting_reconciliation_count': len(awaiting_rows),
            'awaiting_reconciliation_scope':
                'ENDED_WITHOUT_RECONCILIATION',
            'awaiting_reconciliation_expected_by_tender': (
                awaiting_expected_by_tender
                if awaiting_totals_available else None
            ),
            'awaiting_reconciliation_cash_to_receive': (
                _money(awaiting_cash)
                if awaiting_cash_complete else None
            ),
            'awaiting_reconciliation_cash_to_receive_complete':
                awaiting_cash_complete,
            'awaiting_reconciliation_noncash_to_receive': (
                _money(awaiting_noncash)
                if awaiting_noncash_complete else None
            ),
            'awaiting_reconciliation_noncash_to_receive_complete':
                awaiting_noncash_complete,
            'awaiting_reconciliation_all_tenders_to_receive': (
                _money(awaiting_total)
                if awaiting_totals_available else None
            ),
            'awaiting_reconciliation_totals_available':
                awaiting_totals_available,
            'awaiting_reconciliation_unavailable_shift_count':
                len(awaiting_unavailable_ids),
            'total_confirmed_received': (
                _money(total_confirmed)
                if confirmed_all_tenders_complete else None
            ),
            'known_total_confirmed_received': _money(total_confirmed),
            'confirmed_all_tenders_complete':
                confirmed_all_tenders_complete,
            'legacy_cash_only_reconciliation_count':
                legacy_cash_only_reconciliation_count,
            'incomplete_posted_all_tender_reconciliation_count':
                len(incomplete_posted_all_tender_shift_ids),
            # UNKNOWN=0 proves only that no known amount was left without a
            # tender identity. It cannot prove completeness when the derived
            # evidence pass itself failed and returned UNAVAILABLE for a shift.
            'tender_attribution_complete': (
                unknown_expected == 0
                and not unavailable_shift_ids
                and not incomplete_attribution_shift_ids
            ),
            'unattributed_expected_amount': _money(unknown_expected),
            'unattributed_expected_absolute_amount': _money(
                absolute_unattributed
            ),
            'unattributed_evidence_count': unattributed_evidence_count,
            'unattributed_shift_count': len(
                incomplete_attribution_shift_ids - unavailable_shift_ids
            ),
            'tender_evidence_issue_counts': dict(
                sorted(evidence_issue_counts.items())
            ),
            'frozen_tender_discrepancy_shifts': len(discrepancy_shift_ids),
            'tender_totals_sources': {
                'frozen_closed_shifts': frozen_closed_count,
                'derived_closed_shifts': len(derived_closed_rows),
                'derived_live_shifts': len(live_rows),
                'partial_frozen_shifts': partial_frozen_count,
                'unavailable_shifts': len(unavailable_shift_ids),
            },
            'average_revenue_per_shift': (
                _money(revenue / total if total else Decimal('0'))
                if financial_totals_complete else None
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
