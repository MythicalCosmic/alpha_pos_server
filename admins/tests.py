"""Regression tests for admin user/inkassa bugs."""
from decimal import Decimal

import pytest

from admins.services.user_service import AdminUserService
from admins.services.inkassa_service import AdminInkassaService

pytestmark = pytest.mark.django_db


class TestUserRoleValidation:
    """Pre-fix: update_user accepted any string for role, allowing
    role='SUPERADMIN' or other invalid privilege escalation."""

    def test_invalid_role_rejected_on_update(self, regular_user):
        result, status = AdminUserService.update_user(
            regular_user.id, role='SUPERADMIN',
        )
        assert status == 422
        assert 'role' in result.get('errors', {})

    def test_valid_role_accepted_on_update(self, regular_user):
        result, status = AdminUserService.update_user(
            regular_user.id, role='CASHIER',
        )
        assert status == 200
        regular_user.refresh_from_db()
        assert regular_user.role == 'CASHIER'

    def test_invalid_status_rejected_on_update(self, regular_user):
        result, status = AdminUserService.update_user(
            regular_user.id, status='DELETED_SOFT',
        )
        assert status == 422
        assert 'status' in result.get('errors', {})

    def test_invalid_role_rejected_on_create(self):
        # Valid 4-digit PIN so the role check is what trips the rejection.
        result, status = AdminUserService.create_user(
            first_name='X', last_name='Y',
            role='ROOT', password='1234', email='x@y.local',
        )
        assert status == 422

    def test_non_pin_password_rejected_on_create(self):
        # Staff sign in with a 4-digit PIN: anything that isn't exactly
        # 4 digits (too short, too long, non-numeric) is rejected.
        for bad in ('abc', '123', '12345', '12a4'):
            result, status = AdminUserService.create_user(
                first_name='X', last_name='Y',
                role='CASHIER', password=bad, email='x@y.local',
            )
            assert status == 422
            assert 'password' in result.get('errors', {})

    def test_four_digit_pin_accepted_on_create(self):
        result, status = AdminUserService.create_user(
            first_name='Pin', last_name='User',
            role='CASHIER', password='4821', email='pin@y.local',
        )
        assert status == 201


class TestInkassaFloor:
    """Pre-fix: cashier could withdraw more than the register held, driving
    current_balance negative."""

    def test_withdrawal_exceeding_balance_rejected(self, admin_user):
        from base.models import CashRegister
        CashRegister.objects.create(current_balance=Decimal('100'))

        result, status = AdminInkassaService.perform(
            admin_user, {'cash': '500'},
        )
        assert status == 422
        register = CashRegister.objects.first()
        assert register.current_balance == Decimal('100')

    def test_negative_amount_rejected(self, admin_user):
        from base.models import CashRegister
        CashRegister.objects.create(current_balance=Decimal('100'))

        result, status = AdminInkassaService.perform(
            admin_user, {'cash': '-50'},
        )
        assert status == 422

    def test_valid_withdrawal_succeeds(self, admin_user):
        from base.models import CashRegister
        CashRegister.objects.create(current_balance=Decimal('1000'))

        result, status = AdminInkassaService.perform(
            admin_user, {'cash': '300'},
        )
        assert status == 200
        register = CashRegister.objects.first()
        assert register.current_balance == Decimal('700')


class TestInkassaTreasuryRouting:
    """Inkassa: only cash leaves the register; cash -> SAFE, cards -> BANK."""

    def test_cash_to_safe_card_to_bank(self, admin_user):
        from base.models import CashRegister, TreasuryAccount
        CashRegister.objects.create(current_balance=Decimal('1000'))
        result, status = AdminInkassaService.perform(
            admin_user, {'cash': '400', 'uzcard': '300', 'humo': '100'})
        assert status == 200
        # Only the 400 cash left the drawer (bug fix: cards never were in it).
        assert CashRegister.objects.first().current_balance == Decimal('600')
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('400')
        assert TreasuryAccount.objects.get(kind='BANK').balance == Decimal('400')
        assert Decimal(result['data']['cash_to_safe']) == Decimal('400')
        assert Decimal(result['data']['card_to_bank']) == Decimal('400')

    def test_card_only_inkassa_ignores_empty_register(self, admin_user):
        from base.models import CashRegister, TreasuryAccount
        CashRegister.objects.create(current_balance=Decimal('0'))
        result, status = AdminInkassaService.perform(admin_user, {'payme': '500'})
        assert status == 200  # card inkassa not bounded by cash drawer
        assert CashRegister.objects.first().current_balance == Decimal('0')
        assert TreasuryAccount.objects.get(kind='BANK').balance == Decimal('500')


class TestTreasury:
    def test_transfer_bank_to_safe_with_fee(self, admin_user):
        from base.models import TreasuryAccount
        from base.services.treasury_service import TreasuryService
        TreasuryAccount.objects.create(kind='BANK', balance=Decimal('1000'))
        TreasuryAccount.objects.create(kind='SAFE', balance=Decimal('0'))
        res, st = TreasuryService.transfer('BANK', 'SAFE', '1000', fee='5', performed_by=admin_user)
        assert st == 200
        assert TreasuryAccount.objects.get(kind='BANK').balance == Decimal('0')
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('995')

    def test_transfer_insufficient_rejected(self, admin_user):
        from base.models import TreasuryAccount
        from base.services.treasury_service import TreasuryService
        TreasuryAccount.objects.create(kind='SAFE', balance=Decimal('100'))
        _, st = TreasuryService.transfer('SAFE', 'BANK', '500', performed_by=admin_user)
        assert st == 422

    def test_expense_from_safe(self, admin_user):
        from base.models import TreasuryAccount
        from base.services.treasury_service import TreasuryService
        TreasuryAccount.objects.create(kind='SAFE', balance=Decimal('300'))
        res, st = TreasuryService.record_expense('SAFE', '120', category='supplies', performed_by=admin_user)
        assert st == 201
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('180')

    def test_expense_insufficient_rejected(self, admin_user):
        from base.models import TreasuryAccount
        from base.services.treasury_service import TreasuryService
        TreasuryAccount.objects.create(kind='BANK', balance=Decimal('50'))
        _, st = TreasuryService.record_expense('BANK', '100', performed_by=admin_user)
        assert st == 422


class TestShiftHandoverReport:
    def test_report_smoke(self, cashier_user):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Shift
        from admins.services.shift_analytics_service import shift_handover_report
        s = Shift.objects.create(
            user=cashier_user, start_time=timezone.now() - timedelta(hours=2),
            status='ACTIVE')
        rep = shift_handover_report(s)
        assert set(['shift', 'receipts', 'products', 'distribution', 'peak_hour']) <= set(rep)
        assert rep['receipt_count'] == 0
        assert 'cash' in rep['shift']['money'] and 'card' in rep['shift']['money']


class TestShiftEndReconcileFlow:
    """end -> ENDED (stats visible, awaiting manager); reconcile -> COMPLETED."""

    def _active_shift(self, user):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Shift
        return Shift.objects.create(
            user=user, start_time=timezone.now() - timedelta(hours=1), status='ACTIVE')

    def test_end_sets_ended_then_reconcile_completes(self, cashier_user, admin_user):
        from admins.services.shift_service import ShiftService
        s = self._active_shift(cashier_user)
        res, st = ShiftService.end_shift(s.id, cashier_user.id, 'done')
        assert st == 200
        assert res['data']['status'] == 'ENDED'

        res2, st2 = ShiftService.reconcile(s.id, actual_cash='0', notes='', reconciled_by_id=admin_user.id)
        assert st2 == 201
        s.refresh_from_db()
        assert s.status == 'COMPLETED'

    def test_reconcile_requires_ended(self, cashier_user, admin_user):
        from admins.services.shift_service import ShiftService
        s = self._active_shift(cashier_user)  # still ACTIVE
        _, st = ShiftService.reconcile(s.id, actual_cash='0', notes='', reconciled_by_id=admin_user.id)
        assert st == 400

    def test_end_blocked_when_unpaid_open_cart(self, cashier_user, regular_user):
        from base.models import Order
        from admins.services.shift_service import ShiftService
        s = self._active_shift(cashier_user)
        # A genuinely in-progress sale: an OPEN cart that hasn't been paid (no
        # money in the drawer for it yet) — this still blocks the close.
        Order.objects.create(
            user=regular_user, cashier=cashier_user, status='OPEN',
            is_paid=False, display_id=1, subtotal='10.00', total_amount='10.00')
        _, st = ShiftService.end_shift(s.id, cashier_user.id, 'done')
        assert st == 400
        s.refresh_from_db()
        assert s.status == 'ACTIVE'  # close was refused

    def test_end_allowed_with_paid_kitchen_order(self, cashier_user, regular_user):
        from base.models import Order
        from admins.services.shift_service import ShiftService
        s = self._active_shift(cashier_user)
        # Paid order still PREPARING (kitchen hasn't marked it COMPLETED) is
        # settled and carries over — it must NOT block the close. This is the bug
        # that left tills open forever once paid orders piled up in the kitchen.
        Order.objects.create(
            user=regular_user, cashier=cashier_user, status='PREPARING',
            is_paid=True, display_id=1, subtotal='10.00', total_amount='10.00')
        res, st = ShiftService.end_shift(s.id, cashier_user.id, 'done')
        assert st == 200, res
        assert res['data']['status'] == 'ENDED'

    def test_end_allowed_when_order_completed(self, cashier_user, regular_user):
        from base.models import Order
        from admins.services.shift_service import ShiftService
        s = self._active_shift(cashier_user)
        Order.objects.create(
            user=regular_user, cashier=cashier_user, status='COMPLETED',
            is_paid=True, display_id=1, subtotal='10.00', total_amount='10.00')
        res, st = ShiftService.end_shift(s.id, cashier_user.id, 'done')
        assert st == 200
        assert res['data']['status'] == 'ENDED'

    def test_shift_detail_exposes_stats_and_settlement(self, cashier_user, admin_user):
        from admins.services.shift_service import ShiftService
        s = self._active_shift(cashier_user)
        res, st = ShiftService.get(s.id, actor=admin_user)
        assert st == 200
        assert 'stats' in res['data'] and 'settlement' in res['data']
        assert 'payment_mix' in res['data']['stats']
        assert 'category_stats' in res['data']['stats']
        assert isinstance(res['data']['settlement'], list)


class TestAdminInstantOrderParity:
    """is_instant must short-circuit the chef queue on the admin order path too
    (previously only the customer path honoured it)."""

    def test_admin_instant_only_order_born_ready(self, regular_user, category):
        from base.models import Product, Order
        from admins.services.order_service import AdminOrderService
        instant = Product.objects.create(
            name='Cola', price='5.00', category=category, is_instant=True)
        res, st = AdminOrderService.create_order(
            user_id=regular_user.id,
            items=[{'product_id': instant.id, 'quantity': 1}],
        )
        assert st == 201
        order = Order.objects.get(id=res['data']['order_id'])
        assert order.status == 'READY'
        assert order.ready_at is not None
        assert order.chef_queue_number is not None  # chef number allocated


class TestTodayDashboard:
    def test_get_today_includes_new_stat_keys(self, db):
        from admins.services.dashboard_service import get_today
        data = get_today()
        assert 'payment_breakdown_today' in data
        assert 'category_stats_today' in data
        assert {'units_sold', 'peak_hour', 'avg_prep_seconds', 'money_entered'} \
            <= set(data['today'])


class TestShiftListExtras:
    """GET /api/admins/shifts list serializer: the batched per-shift metrics the
    manager dashboard cards need (payment_mix w/ counts, items_sold, avg_prep,
    peak_hour, expenses_total, cancelled_*, net_revenue) — computed in O(1)
    queries for the whole page, not per row."""

    LIST_KEYS = ('net_revenue', 'expenses_total', 'cancelled_orders_count',
                 'cancelled_orders_value', 'payment_mix', 'items_sold',
                 'avg_prep_seconds', 'peak_hour')

    def _list_rows(self, cashier_user, **kw):
        from admins.services.shift_service import ShiftService
        res, st = ShiftService.list(user_id=cashier_user.id, per_page=50, **kw)
        assert st == 200
        return {r['id']: r for r in res['data']['shifts']}

    def _active_shift(self, user, hours_ago=2):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Shift
        return Shift.objects.create(
            user=user, start_time=timezone.now() - timedelta(hours=hours_ago),
            status='ACTIVE')

    def test_row_has_all_fields_and_correct_values(self, cashier_user, regular_user, category):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Order, OrderItem, Product, Shift
        from cashbox.models import CashboxExpense

        s = self._active_shift(cashier_user)
        prod = Product.objects.create(name='Tea', price='10.00', category=category)
        now = timezone.now()
        o1 = Order.objects.create(
            user=regular_user, cashier=cashier_user, status='COMPLETED',
            is_paid=True, payment_method='CASH', display_id=1,
            subtotal='100.00', total_amount='100.00', paid_at=now)
        OrderItem.objects.create(order=o1, product=prod, quantity=3, price='100.00')
        o2 = Order.objects.create(
            user=regular_user, cashier=cashier_user, status='COMPLETED',
            is_paid=True, payment_method='UZCARD', display_id=2,
            subtotal='50.00', total_amount='50.00', paid_at=now)
        OrderItem.objects.create(order=o2, product=prod, quantity=2, price='50.00')
        # prep: o1 100s, o2 200s -> avg 150
        o1.ready_at = o1.created_at + timedelta(seconds=100); o1.save(update_fields=['ready_at'])
        o2.ready_at = o2.created_at + timedelta(seconds=200); o2.save(update_fields=['ready_at'])
        # a cancelled order (lost value) + a drawer expense
        Order.objects.create(
            user=regular_user, cashier=cashier_user, status='CANCELED',
            is_paid=False, display_id=3, subtotal='30.00', total_amount='30.00')
        CashboxExpense.objects.create(shift=s, amount='20.00')

        row = self._list_rows(cashier_user)[s.id]
        for k in self.LIST_KEYS:
            assert k in row, f'missing list field {k}'
        assert row['payment_mix']['CASH'] == {'amount': '100.00', 'count': 1}
        assert row['payment_mix']['UZCARD'] == {'amount': '50.00', 'count': 1}
        assert row['items_sold'] == 5
        assert row['avg_prep_seconds'] == 150
        # peak_hour is now an 'HH:00-HH:00' label string (item 11), not a dict.
        import re
        assert isinstance(row['peak_hour'], str) and re.match(r'^\d{2}:00-\d{2}:00$', row['peak_hour'])
        assert row['cancelled_orders_count'] == 1
        assert row['cancelled_orders_value'] == '30.00'
        assert row['expenses_total'] == '20.00'
        assert row['total_revenue'] == '150.00'        # live: two paid orders
        assert row['net_revenue'] == '100.00'          # 150 - 20 expenses - 30 cancelled
        # item 11 FE-named fields:
        assert row['gross_revenue'] == '150.00'
        assert row['card_collected'] == '50.00'        # 150 total - 100 cash (UZCARD)
        assert row['cancelled_count'] == 1
        assert row['cancelled_amount'] == '30.00'
        assert row['avg_ticket'] == '75.00'            # 150 / 2 paid orders
        assert row['avg_prep_time'] == 150
        assert row['items_sold'] == 5
        assert row['variance'] is None and row['reported'] is None  # not reconciled
        assert row['is_live_stats'] is True

    def test_empty_shift_returns_typed_defaults(self, cashier_user):
        s = self._active_shift(cashier_user)
        row = self._list_rows(cashier_user)[s.id]
        assert row['payment_mix'] == {}
        assert row['items_sold'] == 0
        assert row['peak_hour'] is None
        assert row['avg_prep_seconds'] is None
        assert row['expenses_total'] == '0.00'
        assert row['cancelled_orders_count'] == 0
        assert row['cancelled_orders_value'] == '0.00'
        assert row['net_revenue'] == '0.00'

    def test_null_payment_method_counts_as_cash(self, cashier_user, regular_user):
        from django.utils import timezone
        from base.models import Order
        s = self._active_shift(cashier_user)
        Order.objects.create(
            user=regular_user, cashier=cashier_user, status='COMPLETED',
            is_paid=True, payment_method=None, display_id=1,
            subtotal='40.00', total_amount='40.00', paid_at=timezone.now())
        row = self._list_rows(cashier_user)[s.id]
        assert row['payment_mix'].get('CASH') == {'amount': '40.00', 'count': 1}
        assert 'UZCARD' not in row['payment_mix']

    def test_active_shift_is_live_and_counts_now(self, cashier_user, regular_user):
        from django.utils import timezone
        from base.models import Order
        s = self._active_shift(cashier_user)
        Order.objects.create(
            user=regular_user, cashier=cashier_user, status='COMPLETED',
            is_paid=True, payment_method='HUMO', display_id=1,
            subtotal='70.00', total_amount='70.00', paid_at=timezone.now())
        row = self._list_rows(cashier_user)[s.id]
        assert row['is_live_stats'] is True
        assert row['payment_mix']['HUMO'] == {'amount': '70.00', 'count': 1}
        assert row['total_revenue'] == '70.00'

    def test_two_shifts_same_cashier_no_double_count(self, cashier_user, regular_user):
        """A boundary-instant order (shift2.start == shift1.end) must count in
        EXACTLY ONE shift (the later one), never both."""
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Order, Shift
        t0 = timezone.now() - timedelta(hours=4)
        t1 = timezone.now() - timedelta(hours=2)
        t2 = timezone.now() - timedelta(minutes=1)
        s1 = Shift.objects.create(user=cashier_user, start_time=t0, end_time=t1,
                                  status='COMPLETED', total_revenue='100.00',
                                  total_orders=1, cash_collected='100.00')
        s2 = Shift.objects.create(user=cashier_user, start_time=t1, end_time=t2,
                                  status='COMPLETED', total_revenue='200.00',
                                  total_orders=2, cash_collected='200.00')

        def mkorder(display_id, when):
            o = Order.objects.create(
                user=regular_user, cashier=cashier_user, status='COMPLETED',
                is_paid=True, payment_method='CASH', display_id=display_id,
                subtotal='100.00', total_amount='100.00', paid_at=when)
            Order.objects.filter(id=o.id).update(created_at=when)
            return o

        mkorder(1, t0 + timedelta(minutes=30))   # strictly inside s1
        mkorder(2, t1 + timedelta(minutes=30))   # strictly inside s2
        mkorder(3, t1)                            # boundary -> s2 (latest start wins)

        rows = self._list_rows(cashier_user)
        c1 = rows[s1.id]['payment_mix']['CASH']['count']
        c2 = rows[s2.id]['payment_mix']['CASH']['count']
        assert c1 == 1, f's1 should own only its interior order, got {c1}'
        assert c2 == 2, f's2 should own its order + the boundary one, got {c2}'
        assert c1 + c2 == 3                       # exactly 3 orders, no duplication

    def test_query_count_is_constant_in_rows(self, cashier_user, regular_user, category):
        """The FE O(1) checklist: query count for one paged response must NOT grow
        with the number of shifts on the page."""
        from datetime import timedelta
        from django.utils import timezone
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from base.models import Order, OrderItem, Product, Shift
        from admins.services.shift_service import ShiftService

        prod = Product.objects.create(name='X', price='5.00', category=category)

        def completed_shift(idx):
            start = timezone.now() - timedelta(hours=(10 - idx))   # non-overlapping windows
            end = start + timedelta(minutes=30)
            s = Shift.objects.create(
                user=cashier_user, start_time=start, end_time=end,
                status='COMPLETED', total_revenue='10.00', total_orders=1,
                cash_collected='10.00')
            when = start + timedelta(minutes=1)
            o = Order.objects.create(
                user=regular_user, cashier=cashier_user, status='COMPLETED',
                is_paid=True, payment_method='CASH', display_id=1000 + idx,
                subtotal='10.00', total_amount='10.00', paid_at=when)
            Order.objects.filter(id=o.id).update(created_at=when)
            OrderItem.objects.create(order=o, product=prod, quantity=1, price='10.00')
            return s

        for i in range(2):
            completed_shift(i)
        with CaptureQueriesContext(connection) as small:
            ShiftService.list(user_id=cashier_user.id, per_page=50)
        n_small = len(small)

        for i in range(2, 6):
            completed_shift(i)
        with CaptureQueriesContext(connection) as big:
            ShiftService.list(user_id=cashier_user.id, per_page=50)
        n_big = len(big)

        assert n_big == n_small, \
            f'O(rows) regression: {n_small} queries for 2 shifts vs {n_big} for 6'
