"""The shift list reuses the summary's evidence pass for its page rows.

The reuse must be invisible: every response has to equal the one produced by
running the evidence pass again over the page. It must also stand down for
sorts and ties where that equality is not guaranteed.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from admins.services import shift_service
from admins.services.shift_service import ShiftService
from base.models import Order, OrderPayment, Shift
from core.shifts.service import ShiftService as CoreShiftService

pytestmark = pytest.mark.django_db

NOW = timezone.now().replace(microsecond=0)


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(shift_service.timezone, 'now', lambda: NOW)


def _shift(user, *, start, end, status):
    return Shift.objects.create(
        user=user, branch_id=user.branch_id, start_time=start, end_time=end,
        status=status, total_orders=0, total_revenue=Decimal('0.00'),
        cash_collected=Decimal('0.00'),
    )


def _paid_order(user, *, paid_at, amount, display_id, method='CASH'):
    order = Order.objects.create(
        user=user, cashier=user, branch_id=user.branch_id, display_id=display_id,
        status=Order.Status.COMPLETED, is_paid=True, payment_method=method,
        subtotal=amount, total_amount=amount, paid_at=paid_at,
    )
    OrderPayment.objects.create(order=order, method=method, amount=amount, branch_id=user.branch_id)
    return order


@pytest.fixture
def overlapping_shifts(cashier_user):
    """Shifts whose windows overlap, like a shift left open and force-closed later."""
    long_open = _shift(cashier_user, start=NOW - timedelta(days=3), end=NOW - timedelta(hours=1),
                       status=Shift.Status.ENDED)
    morning = _shift(cashier_user, start=NOW - timedelta(hours=10), end=NOW - timedelta(hours=6),
                     status=Shift.Status.ENDED)
    evening = _shift(cashier_user, start=NOW - timedelta(hours=5), end=NOW - timedelta(hours=2),
                     status=Shift.Status.COMPLETED)
    live = _shift(cashier_user, start=NOW - timedelta(minutes=40), end=None, status=Shift.Status.ACTIVE)
    for number, (hours_ago, amount, method) in enumerate([
        (60, '10.00', 'CASH'), (8, '20.00', 'CASH'), (7, '15.00', 'UZCARD'),
        (4, '30.00', 'CASH'), (3, '12.00', 'PAYME'), (0.5, '50.00', 'CASH'),
        (0.2, '7.00', 'HUMO'),
    ], start=1):
        _paid_order(cashier_user, paid_at=NOW - timedelta(hours=hours_ago),
                    amount=amount, display_id=5000 + number, method=method)
    return [long_open, morning, evening, live]


@pytest.fixture(autouse=True)
def cloud_admin(admin_user):
    # A cloud admin sees every branch, as the owner does in the admin table.
    admin_user.branch_id = 'cloud'
    admin_user.save(update_fields=['branch_id'])


def _list(admin_user, **kwargs):
    result, status = ShiftService.list(actor=admin_user, **kwargs)
    assert status == 200, result
    return result['data']


def _without_reuse(monkeypatch):
    monkeypatch.setattr(ShiftService, '_page_extras_from_summary',
                        staticmethod(lambda *args, **kwargs: None))


def _count_evidence_passes(monkeypatch):
    calls = []
    original = CoreShiftService._batch_list_extras

    def counting(shifts, now=None):
        calls.append(len(shifts))
        return original(shifts, now=now)

    monkeypatch.setattr(CoreShiftService, '_batch_list_extras', staticmethod(counting))
    return calls


@pytest.mark.parametrize('page,per_page', [(1, 2), (2, 2), (1, 100)])
def test_reused_page_rows_equal_a_fresh_evidence_pass(
    admin_user, overlapping_shifts, frozen_now, monkeypatch, page, per_page,
):
    reused = _list(admin_user, page=page, per_page=per_page)
    with monkeypatch.context() as patch:
        _without_reuse(patch)
        fresh = _list(admin_user, page=page, per_page=per_page)
    assert reused == fresh
    assert reused['shifts'], 'fixture must put shifts on the page'


def test_default_sort_runs_the_evidence_pass_once(admin_user, overlapping_shifts, frozen_now, monkeypatch):
    calls = _count_evidence_passes(monkeypatch)
    _list(admin_user, per_page=100)
    assert calls == [len(overlapping_shifts)]


def test_later_pages_keep_their_own_page_pass(admin_user, overlapping_shifts, frozen_now, monkeypatch):
    # Page 2 holds the older shifts; the newer ones on page 1 out-rank them for
    # overlapping orders, so the summary's pass is not the page's answer.
    calls = _count_evidence_passes(monkeypatch)
    _list(admin_user, page=2, per_page=2)
    assert calls == [len(overlapping_shifts), 2]


@pytest.mark.parametrize('order_by', ['start_time', '-total_revenue'])
def test_other_sorts_keep_their_own_page_pass(admin_user, overlapping_shifts, frozen_now, monkeypatch, order_by):
    calls = _count_evidence_passes(monkeypatch)
    _list(admin_user, per_page=2, order_by=order_by)
    assert calls == [len(overlapping_shifts), 2]


def test_start_time_tie_at_the_page_boundary_is_recomputed(
    admin_user, cashier_user, overlapping_shifts, frozen_now, monkeypatch,
):
    morning = overlapping_shifts[1]
    _shift(cashier_user, start=morning.start_time, end=morning.end_time, status=Shift.Status.ENDED)
    calls = _count_evidence_passes(monkeypatch)
    # Newest first: live, evening, then the two shifts that share a start.
    _list(admin_user, per_page=3)
    assert calls == [len(overlapping_shifts) + 1, 3]
