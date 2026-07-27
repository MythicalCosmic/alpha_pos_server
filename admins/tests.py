"""Regression tests for admin user/inkassa bugs."""
from decimal import Decimal

import pytest

from admins.services.user_service import AdminUserService
from admins.services.inkassa_service import AdminInkassaService

pytestmark = pytest.mark.django_db


def _recognize_settlement(branch_id, tenders, admin_user, shift_id):
    """Seed the reconciliation-first treasury evidence Inkassa consumes."""
    from base.services.treasury_service import TreasuryService

    return TreasuryService.post_shift_settlement(
        shift_id,
        tenders,
        performed_by=admin_user,
        branch_id=branch_id,
    )


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
        admin_user.branch_id = 'cloud'
        admin_user.save(update_fields=['branch_id'])
        register = CashRegister.objects.create(current_balance=Decimal('100'))

        result, status = AdminInkassaService.perform(
            admin_user,
            {'cash': '500'},
            branch_id=register.branch_id,
            batch_key='floor-too-large',
        )
        assert status == 422
        register = CashRegister.objects.first()
        assert register.current_balance == Decimal('100')

    def test_negative_amount_rejected(self, admin_user):
        from base.models import CashRegister
        admin_user.branch_id = 'cloud'
        admin_user.save(update_fields=['branch_id'])
        register = CashRegister.objects.create(current_balance=Decimal('100'))

        result, status = AdminInkassaService.perform(
            admin_user,
            {'cash': '-50'},
            branch_id=register.branch_id,
            batch_key='floor-negative',
        )
        assert status == 422

    def test_valid_withdrawal_succeeds(self, admin_user):
        from base.models import CashRegister
        admin_user.branch_id = 'cloud'
        admin_user.save(update_fields=['branch_id'])
        register = CashRegister.objects.create(current_balance=Decimal('1000'))
        _recognize_settlement(
            register.branch_id, {'CASH': '300'}, admin_user, shift_id=9101,
        )

        result, status = AdminInkassaService.perform(
            admin_user,
            {'cash': '300'},
            branch_id=register.branch_id,
            batch_key='floor-valid',
        )
        assert status == 200, result
        register = CashRegister.objects.first()
        assert register.current_balance == Decimal('1000')
        assert Decimal(result['data']['balance_after']) == Decimal('700')


class TestInkassaTreasuryRouting:
    """Inkassa is physical register movement/audit, never revenue recognition."""

    def test_mixed_inkassa_does_not_credit_treasury(self, admin_user):
        from base.models import CashRegister, TreasuryAccount
        admin_user.branch_id = 'cloud'
        admin_user.save(update_fields=['branch_id'])
        register = CashRegister.objects.create(current_balance=Decimal('1000'))
        _recognize_settlement(
            register.branch_id,
            {'CASH': '400', 'UZCARD': '300', 'HUMO': '100'},
            admin_user,
            shift_id=9201,
        )
        safe_before = TreasuryAccount.objects.get(kind='SAFE').balance
        result, status = AdminInkassaService.perform(
            admin_user,
            {'cash': '400', 'uzcard': '300', 'humo': '100'},
            branch_id=register.branch_id,
            batch_key='routing-mixed',
        )
        assert status == 200, result
        # Cloud records a durable 400 cash command; it cannot overwrite the
        # branch-owned raw register before the desktop acknowledges it.
        assert CashRegister.objects.first().current_balance == Decimal('1000')
        assert Decimal(result['data']['balance_after']) == Decimal('600')
        assert TreasuryAccount.objects.get(kind='SAFE').balance == safe_before
        assert Decimal(result['data']['cash_to_safe']) == Decimal('0')
        assert Decimal(result['data']['card_to_bank']) == Decimal('0')
        assert result['data']['treasury_posting']['status'] == 'not_posted'

    def test_card_only_inkassa_ignores_empty_register(self, admin_user):
        from base.models import CashRegister, TreasuryAccount
        admin_user.branch_id = 'cloud'
        admin_user.save(update_fields=['branch_id'])
        register = CashRegister.objects.create(current_balance=Decimal('0'))
        _recognize_settlement(
            register.branch_id, {'PAYME': '500'}, admin_user, shift_id=9202,
        )
        safe_before = TreasuryAccount.objects.get(kind='SAFE').balance
        result, status = AdminInkassaService.perform(
            admin_user,
            {'payme': '500'},
            branch_id=register.branch_id,
            batch_key='routing-payme',
        )
        assert status == 200, result  # card inkassa not bounded by cash drawer
        assert CashRegister.objects.first().current_balance == Decimal('0')
        assert TreasuryAccount.objects.get(kind='SAFE').balance == safe_before
        assert result['data']['treasury_posting']['status'] == 'not_posted'


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
        admin_user.branch_id = 'cloud'
        admin_user.save(update_fields=['branch_id'])
        s = self._active_shift(cashier_user)
        res, st = ShiftService.end_shift(s.id, cashier_user.id, 'done')
        assert st == 200
        assert res['data']['status'] == 'ENDED'

        res2, st2 = ShiftService.reconcile(
            s.id,
            actual_cash='0',
            notes='',
            reconciled_by_id=admin_user.id,
            actor=admin_user,
        )
        assert st2 == 201
        s.refresh_from_db()
        assert s.status == 'COMPLETED'

    def test_reconcile_requires_ended(self, cashier_user, admin_user):
        from admins.services.shift_service import ShiftService
        admin_user.branch_id = 'cloud'
        admin_user.save(update_fields=['branch_id'])
        s = self._active_shift(cashier_user)  # still ACTIVE
        _, st = ShiftService.reconcile(
            s.id,
            actual_cash='0',
            notes='',
            reconciled_by_id=admin_user.id,
            actor=admin_user,
        )
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
        admin_user.branch_id = 'cloud'
        admin_user.save(update_fields=['branch_id'])
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

    def test_admin_instant_only_order_born_ready(
        self, regular_user, cashier_user, category,
    ):
        from django.conf import settings
        from django.utils import timezone
        from base.models import Product, Order, Shift
        from admins.services.order_service import AdminOrderService
        Shift.objects.create(
            user=cashier_user,
            status=Shift.Status.ACTIVE,
            start_time=timezone.now(),
            branch_id=settings.BRANCH_ID,
        )
        instant = Product.objects.create(
            name='Cola', price='5.00', category=category, is_instant=True)
        res, st = AdminOrderService.create_order(
            user_id=regular_user.id,
            cashier_id=cashier_user.id,
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
    manager dashboard cards need (canonical payment_mix, paid_orders, items_sold, avg_prep,
    peak_hour, expenses_total, cancelled_*, net_revenue) — computed in O(1)
    queries for the whole page, not per row."""

    LIST_KEYS = ('net_revenue', 'expenses_total', 'cancelled_orders_count',
                 'cancelled_orders_value', 'payment_mix', 'paid_orders', 'items_sold',
                 'avg_prep_seconds', 'peak_hour', 'expected_by_tender',
                 'cash_to_receive', 'noncash_to_receive',
                 'all_tenders_to_receive',
                 'total_expected_to_receive_scope',
                 'total_expected_to_receive', 'settlement', 'reconciled_count',
                 'cashbox_expenses')

    def _list_rows(self, cashier_user, **kw):
        from admins.services.shift_service import ShiftService
        res, st = ShiftService.list(user_id=cashier_user.id, per_page=50, **kw)
        assert st == 200
        return {r['id']: r for r in res['data']['shifts']}

    def _active_shift(self, user, hours_ago=2):
        from django.conf import settings
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Shift
        return Shift.objects.create(
            user=user, start_time=timezone.now() - timedelta(hours=hours_ago),
            status='ACTIVE', branch_id=settings.BRANCH_ID,
            treasury_settlement_eligible=True)

    def test_row_has_all_fields_and_correct_values(self, cashier_user, regular_user, category):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Order, OrderItem, Product
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
        o1.ready_at = o1.created_at + timedelta(seconds=100)
        o1.save(update_fields=['ready_at'])
        o2.ready_at = o2.created_at + timedelta(seconds=200)
        o2.save(update_fields=['ready_at'])
        # a cancelled order (lost value) + a drawer expense
        Order.objects.create(
            user=regular_user, cashier=cashier_user, status='CANCELED',
            is_paid=False, display_id=3, subtotal='30.00', total_amount='30.00',
            branch_id=s.branch_id)
        CashboxExpense.objects.create(
            shift=s, amount='20.00', branch_id=s.branch_id,
        )

        row = self._list_rows(cashier_user)[s.id]
        for k in self.LIST_KEYS:
            assert k in row, f'missing list field {k}'
        # Canonical tenders: {tender: amount}. UZCARD folds into `card`; an order can
        # contribute to two tenders, so the paid-order count is its own field.
        assert row['payment_mix']['cash'] == '100.00'
        assert row['payment_mix']['card'] == '50.00'
        assert row['paid_orders'] == 2
        assert row['items_sold'] == 5
        assert row['avg_prep_seconds'] == 150
        # peak_hour is now an 'HH:00-HH:00' label string (item 11), not a dict.
        import re
        assert isinstance(row['peak_hour'], str) and re.match(r'^\d{2}:00-\d{2}:00$', row['peak_hour'])
        assert row['cancelled_orders_count'] == 1
        assert row['cancelled_orders_value'] == '30.00'
        assert row['expenses_total'] == '20.00'
        assert row['expected_by_tender'] == {
            'CASH': '80.00',
            'UZCARD': '50.00',
            'HUMO': '0.00',
            'CARD': '0.00',
            'PAYME': '0.00',
        }
        assert row['cash_to_receive'] == '80.00'
        assert row['noncash_to_receive'] == '50.00'
        assert row['all_tenders_to_receive'] == '130.00'
        assert row['total_expected_to_receive_scope'] == 'ALL_TENDERS'
        assert row['total_expected_to_receive'] == '130.00'
        assert row['reconciled_count'] == 0
        assert row['settlement'] == []
        assert row['cashbox_expenses'][0] == {
            'id': row['cashbox_expenses'][0]['id'],
            'shift_id': s.id,
            'amount': '20.00',
            'category': None,
            'category_id': None,
            'description': '',
            'comment': '',
            'paid_at': row['cashbox_expenses'][0]['created_at'],
            'created_at': row['cashbox_expenses'][0]['created_at'],
            'paid_by': None,
            'status': 'RECORDED',
        }
        assert row['total_revenue'] == '150.00'        # live: two paid orders
        # The cancelled ticket was never paid, so it is operational lost value,
        # not money to subtract a second time from realized revenue.
        assert row['net_revenue'] == '130.00'          # 150 - 20 expenses
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
        from admins.services.shift_service import ShiftService
        listed, status = ShiftService.list(
            user_id=cashier_user.id, per_page=50,
        )
        assert status == 200, listed
        summary = listed['data']['summary']
        assert summary['expected_by_tender'] == row['expected_by_tender']
        assert summary['cash_to_receive'] == '80.00'
        assert summary['noncash_to_receive'] == '50.00'
        assert summary['all_tenders_to_receive'] == '130.00'
        assert summary['total_expected_to_receive_scope'] == 'ALL_TENDERS'
        assert summary['total_expected_to_receive'] == '130.00'

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
        assert row['cash_to_receive'] == '0.00'
        assert row['noncash_to_receive'] == '0.00'
        assert row['all_tenders_to_receive'] == '0.00'
        assert row['total_expected_to_receive_scope'] == 'ALL_TENDERS'
        assert row['net_revenue'] == '0.00'

    def test_partial_frozen_tenders_fall_back_to_complete_derived_totals(
        self, cashier_user, regular_user,
    ):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Order, OrderPayment
        from cashbox.models import ShiftPaymentTotal
        from admins.services.shift_service import ShiftService

        shift = self._active_shift(cashier_user)
        paid_at = timezone.now()
        order = Order.objects.create(
            user=regular_user,
            cashier=cashier_user,
            branch_id=shift.branch_id,
            status=Order.Status.COMPLETED,
            is_paid=True,
            payment_method='HUMO',
            paid_at=paid_at,
            display_id=991,
            subtotal='70.00',
            total_amount='70.00',
        )
        OrderPayment.objects.create(
            order=order,
            method='HUMO',
            amount='70.00',
            branch_id=shift.branch_id,
        )
        shift.status = 'ENDED'
        shift.end_time = paid_at + timedelta(seconds=1)
        shift.total_orders = 1
        shift.total_revenue = '70.00'
        shift.cash_collected = '0.00'
        shift.save(update_fields=[
            'status', 'end_time', 'total_orders', 'total_revenue',
            'cash_collected',
        ])
        # Simulate a child-by-child sync window: only one of the five frozen
        # tender rows has arrived. This must not hide the other four identities
        # or suppress the canonical order/payment fallback.
        ShiftPaymentTotal.objects.create(
            shift=shift,
            method='HUMO',
            expected_amount='70.00',
            counted_amount='70.00',
            difference='0.00',
            branch_id=shift.branch_id,
        )

        listed, status = ShiftService.list(
            user_id=cashier_user.id, per_page=50,
        )

        assert status == 200, listed
        row = next(
            value for value in listed['data']['shifts']
            if value['id'] == shift.id
        )
        assert row['expected_by_tender'] == {
            'CASH': '0.00',
            'UZCARD': '0.00',
            'HUMO': '70.00',
            'CARD': '0.00',
            'PAYME': '0.00',
        }
        assert row['tender_totals_source'] == (
            'DERIVED_INCOMPLETE_FROZEN'
        )
        assert row['frozen_tender_evidence_complete'] is False

        summary = listed['data']['summary']
        assert summary['expected_by_tender'] == row['expected_by_tender']
        assert summary['total_expected_to_receive'] == '70.00'
        assert summary['tender_attribution_complete'] is True
        assert summary['unattributed_expected_amount'] == '0.00'
        assert summary['tender_totals_sources'] == {
            'frozen_closed_shifts': 0,
            'derived_closed_shifts': 1,
            'derived_live_shifts': 0,
            'partial_frozen_shifts': 1,
            'unavailable_shifts': 0,
        }

    def test_full_zero_frozen_set_cannot_hide_unattributed_mixed_sale(
        self, cashier_user, regular_user,
    ):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Order
        from cashbox.models import PAYMENT_METHODS, ShiftPaymentTotal
        from admins.services.shift_service import ShiftService

        shift = self._active_shift(cashier_user)
        paid_at = timezone.now()
        Order.objects.create(
            user=regular_user,
            cashier=cashier_user,
            branch_id=shift.branch_id,
            status=Order.Status.COMPLETED,
            is_paid=True,
            payment_method='MIXED',
            paid_at=paid_at,
            display_id=992,
            subtotal='100.00',
            total_amount='100.00',
        )
        shift.status = 'ENDED'
        shift.end_time = paid_at + timedelta(seconds=1)
        shift.total_orders = 1
        shift.total_revenue = '100.00'
        shift.cash_collected = '0.00'
        shift.save(update_fields=[
            'status', 'end_time', 'total_orders', 'total_revenue',
            'cash_collected',
        ])
        for method in PAYMENT_METHODS:
            ShiftPaymentTotal.objects.create(
                shift=shift,
                method=method,
                expected_amount='0.00',
                counted_amount='0.00',
                difference='0.00',
                branch_id=shift.branch_id,
            )

        listed, status = ShiftService.list(
            user_id=cashier_user.id, per_page=50,
        )

        assert status == 200, listed
        row = next(
            value for value in listed['data']['shifts']
            if value['id'] == shift.id
        )
        assert row['expected_by_tender'] == {
            'CASH': '0.00',
            'UZCARD': '0.00',
            'HUMO': '0.00',
            'CARD': '0.00',
            'PAYME': '0.00',
            'UNKNOWN': '100.00',
        }
        assert row['tender_totals_source'] == (
            'DERIVED_INCOMPLETE_FROZEN'
        )
        assert row['frozen_tender_evidence_complete'] is False
        assert row['tender_attribution_complete'] is False
        assert row['cash_to_receive_complete'] is False
        assert row['cash_to_receive'] is None
        assert row['noncash_to_receive_complete'] is False
        assert row['noncash_to_receive'] is None
        assert row['all_tenders_to_receive'] == '100.00'
        assert row['unattributed_expected_amount'] == '100.00'
        assert row['frozen_tender_evidence_issues'] == [
            'UNATTRIBUTED_TENDER_EVIDENCE',
        ]

        summary = listed['data']['summary']
        assert summary['expected_by_tender'] == row['expected_by_tender']
        assert summary['total_expected_to_receive'] == '100.00'
        assert summary['cash_to_receive'] is None
        assert summary['cash_to_receive_complete'] is False
        assert summary['noncash_to_receive'] is None
        assert summary['all_tenders_to_receive'] == '100.00'
        assert (
            summary['awaiting_reconciliation_cash_to_receive']
            is None
        )
        assert (
            summary[
                'awaiting_reconciliation_cash_to_receive_complete'
            ]
            is False
        )
        assert (
            summary['awaiting_reconciliation_all_tenders_to_receive']
            == '100.00'
        )
        assert summary['tender_attribution_complete'] is False
        assert summary['unattributed_expected_amount'] == '100.00'
        assert summary['unattributed_expected_absolute_amount'] == '100.00'
        assert summary['unattributed_shift_count'] == 1
        assert summary['tender_evidence_issue_counts'] == {
            'UNATTRIBUTED_TENDER_EVIDENCE': 1,
        }
        assert summary['frozen_tender_discrepancy_shifts'] == 1
        assert summary['tender_totals_sources'] == {
            'frozen_closed_shifts': 0,
            'derived_closed_shifts': 1,
            'derived_live_shifts': 0,
            'partial_frozen_shifts': 1,
            'unavailable_shifts': 0,
        }

    def test_summary_fails_closed_when_tender_evidence_is_unavailable(
        self, cashier_user, monkeypatch,
    ):
        from django.utils import timezone
        from base.models import Shift
        from admins.services.shift_service import (
            CoreShiftService,
            ShiftService,
        )

        shift = self._active_shift(cashier_user)

        def unavailable_extras(shifts, now=None):
            return {
                item.id: {
                    'financial_evidence_available': False,
                    'expenses_total': None,
                    'refunds_total': None,
                    'cancelled_orders_value': None,
                    'expected_by_tender': {},
                    'cash_to_receive': None,
                    'cash_to_receive_complete': False,
                    'noncash_to_receive': None,
                    'noncash_to_receive_complete': False,
                    'all_tenders_to_receive': None,
                    'all_tenders_to_receive_complete': False,
                    'unattributed_expected_amount': None,
                    'tender_totals_source': 'UNAVAILABLE',
                }
                for item in shifts
            }

        monkeypatch.setattr(
            CoreShiftService,
            '_batch_list_extras',
            staticmethod(unavailable_extras),
        )

        summary = ShiftService._global_summary(
            Shift.objects.filter(pk=shift.pk),
            now=timezone.now(),
        )

        assert summary['expected_by_tender'] == {}
        assert summary['tender_attribution_complete'] is False
        assert summary['financial_totals_complete'] is False
        assert summary['total_orders'] is None
        assert summary['total_revenue'] is None
        assert summary['cash_collected'] is None
        assert summary['cash_to_receive'] is None
        assert summary['all_tenders_to_receive'] is None
        assert summary['awaiting_reconciliation_cash_to_receive'] == '0.00'
        assert summary['tender_totals_sources'] == {
            'frozen_closed_shifts': 0,
            'derived_closed_shifts': 0,
            'derived_live_shifts': 1,
            'partial_frozen_shifts': 0,
            'unavailable_shifts': 1,
        }

    def test_awaiting_reconciliation_kpi_uses_complete_filtered_population(
        self, cashier_user, admin_user,
    ):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import (
            CashReconciliation, Order, OrderPayment, Shift,
        )
        from cashbox.models import PAYMENT_METHODS, ShiftPaymentTotal
        from admins.services.shift_service import ShiftService

        now = timezone.now()
        branch = cashier_user.branch_id

        def shift_with_cash(*, start, end, status, amount, display_id):
            shift = Shift.objects.create(
                user=cashier_user,
                branch_id=branch,
                start_time=start,
                end_time=end,
                status=status,
                total_orders=1,
                total_revenue=amount,
                cash_collected=amount,
            )
            paid_at = start + (end - start) / 2 if end else now
            order = Order.objects.create(
                user=cashier_user,
                cashier=cashier_user,
                branch_id=branch,
                display_id=display_id,
                status=Order.Status.COMPLETED,
                is_paid=True,
                payment_method=Order.PaymentMethod.CASH,
                subtotal=amount,
                total_amount=amount,
                paid_at=paid_at,
            )
            OrderPayment.objects.create(
                order=order,
                method=Order.PaymentMethod.CASH,
                amount=amount,
                branch_id=branch,
            )
            return shift

        ended = shift_with_cash(
            start=now - timedelta(hours=8),
            end=now - timedelta(hours=7),
            status=Shift.Status.ENDED,
            amount='100.00',
            display_id=1001,
        )
        completed = shift_with_cash(
            start=now - timedelta(hours=6),
            end=now - timedelta(hours=5),
            status=Shift.Status.COMPLETED,
            amount='30.00',
            display_id=1002,
        )
        shift_with_cash(
            start=now - timedelta(hours=4),
            end=now - timedelta(hours=3),
            status=Shift.Status.ABANDONED,
            amount='20.00',
            display_id=1003,
        )
        shift_with_cash(
            start=now - timedelta(minutes=30),
            end=None,
            status=Shift.Status.ACTIVE,
            amount='50.00',
            display_id=1004,
        )
        for method in PAYMENT_METHODS:
            amount = '30.00' if method == 'CASH' else '0.00'
            ShiftPaymentTotal.objects.create(
                shift=completed,
                branch_id=branch,
                method=method,
                expected_amount=amount,
                counted_amount=amount,
                confirmed_amount=amount,
                difference='0.00',
            )
        CashReconciliation.objects.create(
            shift=completed,
            branch_id=branch,
            expected_cash='30.00',
            actual_cash='30.00',
            difference='0.00',
            reconciled_by=admin_user,
            treasury_posted_at=now,
        )

        listed, status = ShiftService.list(
            user_id=cashier_user.id,
            per_page=1,
        )

        assert status == 200, listed
        assert len(listed['data']['shifts']) == 1
        assert listed['data']['pagination']['total'] == 4
        summary = listed['data']['summary']
        assert summary['cash_to_receive'] == '200.00'
        assert summary['cash_to_receive_scope'] == 'ALL_FILTERED_SHIFTS'
        assert summary['unreconciled_count'] == 3
        assert summary['awaiting_reconciliation_count'] == 1
        assert summary['awaiting_reconciliation_scope'] == (
            'ENDED_WITHOUT_RECONCILIATION'
        )
        assert (
            summary['awaiting_reconciliation_cash_to_receive']
            == '100.00'
        )
        assert summary[
            'awaiting_reconciliation_cash_to_receive_complete'
        ] is True
        assert summary[
            'awaiting_reconciliation_all_tenders_to_receive'
        ] == '100.00'
        assert ended.id != completed.id

    def test_confirmed_summary_requires_reconciliation_and_stays_frozen(
        self, cashier_user, admin_user,
    ):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import (
            CashReconciliation, Order, OrderPayment, Shift,
        )
        from cashbox.models import PAYMENT_METHODS, ShiftPaymentTotal
        from admins.services.shift_service import ShiftService

        now = timezone.now()
        branch = cashier_user.branch_id
        legacy_reconciled = Shift.objects.create(
            user=cashier_user,
            branch_id=branch,
            start_time=now - timedelta(hours=6),
            end_time=now - timedelta(hours=5),
            status=Shift.Status.COMPLETED,
        )
        legacy_reconciled_order = Order.objects.create(
            user=cashier_user,
            cashier=cashier_user,
            branch_id=branch,
            display_id=1099,
            status=Order.Status.COMPLETED,
            is_paid=True,
            payment_method=Order.PaymentMethod.HUMO,
            subtotal='40.00',
            total_amount='40.00',
            paid_at=now - timedelta(hours=5, minutes=30),
        )
        OrderPayment.objects.create(
            order=legacy_reconciled_order,
            method=Order.PaymentMethod.HUMO,
            amount='40.00',
            branch_id=branch,
        )
        for method in PAYMENT_METHODS:
            ShiftPaymentTotal.objects.create(
                shift=legacy_reconciled,
                branch_id=branch,
                method=method,
                expected_amount='0.00',
                counted_amount='0.00',
                confirmed_amount='0.00',
                difference='0.00',
            )
        CashReconciliation.objects.create(
            shift=legacy_reconciled,
            branch_id=branch,
            expected_cash='0.00',
            actual_cash='0.00',
            difference='0.00',
            reconciled_by=admin_user,
            # No treasury_posted_at: historical reconciliation proves CASH only.
        )
        reconciled = Shift.objects.create(
            user=cashier_user,
            branch_id=branch,
            start_time=now - timedelta(hours=4),
            end_time=now - timedelta(hours=3),
            status=Shift.Status.COMPLETED,
        )
        for index, amount in enumerate(('100.00', '50.00'), start=1):
            order = Order.objects.create(
                user=cashier_user,
                cashier=cashier_user,
                branch_id=branch,
                display_id=1100 + index,
                status=Order.Status.COMPLETED,
                is_paid=True,
                payment_method=Order.PaymentMethod.HUMO,
                subtotal=amount,
                total_amount=amount,
                paid_at=(
                    now - timedelta(hours=3, minutes=40 - index * 10)
                ),
            )
            OrderPayment.objects.create(
                order=order,
                method=Order.PaymentMethod.HUMO,
                amount=amount,
                branch_id=branch,
            )
        for method in PAYMENT_METHODS:
            amount = '100.00' if method == 'HUMO' else '0.00'
            ShiftPaymentTotal.objects.create(
                shift=reconciled,
                branch_id=branch,
                method=method,
                expected_amount=amount,
                counted_amount=amount,
                confirmed_amount=amount,
                difference='0.00',
            )
        CashReconciliation.objects.create(
            shift=reconciled,
            branch_id=branch,
            expected_cash='0.00',
            actual_cash='0.00',
            difference='0.00',
            reconciled_by=admin_user,
            treasury_posted_at=now,
        )

        legacy = Shift.objects.create(
            user=cashier_user,
            branch_id=branch,
            start_time=now - timedelta(hours=2),
            end_time=now - timedelta(hours=1),
            status=Shift.Status.ENDED,
        )
        legacy_order = Order.objects.create(
            user=cashier_user,
            cashier=cashier_user,
            branch_id=branch,
            display_id=1201,
            status=Order.Status.COMPLETED,
            is_paid=True,
            payment_method=Order.PaymentMethod.UZCARD,
            subtotal='20.00',
            total_amount='20.00',
            paid_at=now - timedelta(minutes=90),
        )
        OrderPayment.objects.create(
            order=legacy_order,
            method=Order.PaymentMethod.UZCARD,
            amount='20.00',
            branch_id=branch,
        )
        ShiftPaymentTotal.objects.create(
            shift=legacy,
            branch_id=branch,
            method='UZCARD',
            expected_amount='20.00',
            counted_amount='20.00',
            confirmed_amount='999.00',
            difference='0.00',
        )

        listed, status = ShiftService.list(
            user_id=cashier_user.id,
            per_page=1,
        )

        assert status == 200, listed
        summary = listed['data']['summary']
        assert summary['confirmed_by_tender']['HUMO'] == '100.00'
        assert summary['confirmed_by_tender']['CASH'] == '0.00'
        assert summary['confirmed_by_tender']['UZCARD'] == '0.00'
        assert summary['confirmed_by_tender']['UZCARD'] != '999.00'
        assert summary['total_confirmed_received'] is None
        assert summary['known_total_confirmed_received'] == '100.00'
        assert summary['confirmed_all_tenders_complete'] is False
        assert summary['legacy_cash_only_reconciliation_count'] == 1
        assert summary['expected_by_tender']['HUMO'] == '100.00'
        assert summary['all_tenders_to_receive'] is None
        assert summary['all_tenders_to_receive_complete'] is False
        assert summary['frozen_tender_discrepancy_shifts'] >= 1

    def test_confirmed_summary_requires_full_posted_tender_bundle(
        self, cashier_user, admin_user,
    ):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import CashReconciliation, Shift
        from cashbox.models import ShiftPaymentTotal
        from admins.services.shift_service import ShiftService

        now = timezone.now()
        shift = Shift.objects.create(
            user=cashier_user,
            branch_id=cashier_user.branch_id,
            start_time=now - timedelta(hours=2),
            end_time=now - timedelta(hours=1),
            status=Shift.Status.COMPLETED,
        )
        for method, amount in (
            ('CASH', '30.00'),
            ('HUMO', '70.00'),
        ):
            ShiftPaymentTotal.objects.create(
                shift=shift,
                branch_id=cashier_user.branch_id,
                method=method,
                expected_amount=amount,
                counted_amount=amount,
                confirmed_amount=amount,
                difference='0.00',
            )
        CashReconciliation.objects.create(
            shift=shift,
            branch_id=cashier_user.branch_id,
            expected_cash='30.00',
            actual_cash='30.00',
            difference='0.00',
            reconciled_by=admin_user,
            treasury_posted_at=now,
        )

        listed, status = ShiftService.list(
            user_id=cashier_user.id,
            per_page=1,
        )

        assert status == 200, listed
        summary = listed['data']['summary']
        assert summary['confirmed_by_tender'] == {
            'CASH': '30.00',
            'HUMO': '70.00',
        }
        assert summary['known_total_confirmed_received'] == '100.00'
        assert summary['total_confirmed_received'] is None
        assert summary['confirmed_all_tenders_complete'] is False
        assert summary['legacy_cash_only_reconciliation_count'] == 0
        assert (
            summary[
                'incomplete_posted_all_tender_reconciliation_count'
            ]
            == 1
        )

    def test_reconciled_shift_exposes_tenders_expenses_and_posting(
        self, cashier_user, admin_user, regular_user,
    ):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Order, OrderPayment
        from cashbox.models import (
            CashboxExpense, CashboxExpenseCategory, ShiftPaymentTotal,
        )
        from admins.services.shift_service import ShiftService

        admin_user.branch_id = 'cloud'
        admin_user.save(update_fields=['branch_id'])
        shift = self._active_shift(cashier_user)
        shift.status = 'ENDED'
        shift.end_time = timezone.now()
        shift.save(update_fields=['status', 'end_time'])
        amounts = {
            'CASH': '100.00',
            'HUMO': '20.00',
            'UZCARD': '30.00',
            'CARD': '40.00',
            'PAYME': '50.00',
        }
        # CASH gross is 110; the 10 drawer expense below makes its canonical
        # amount-to-hand-over 100. Every other tender remains unchanged.
        sales = {
            'CASH': '110.00',
            'HUMO': '20.00',
            'UZCARD': '30.00',
            'CARD': '40.00',
            'PAYME': '50.00',
        }
        for index, (method, amount) in enumerate(sales.items(), start=1):
            order = Order.objects.create(
                user=regular_user,
                cashier=cashier_user,
                branch_id=shift.branch_id,
                status=Order.Status.COMPLETED,
                is_paid=True,
                payment_method=method,
                paid_at=shift.end_time - timedelta(seconds=1),
                display_id=index,
                subtotal=amount,
                total_amount=amount,
            )
            OrderPayment.objects.create(
                order=order,
                method=method,
                amount=amount,
                branch_id=shift.branch_id,
            )
        for method, amount in amounts.items():
            ShiftPaymentTotal.objects.create(
                shift=shift,
                method=method,
                expected_amount=amount,
                counted_amount=amount,
                difference='0.00',
                branch_id=shift.branch_id,
            )
        category = CashboxExpenseCategory.objects.create(name='Supplies')
        expense = CashboxExpense.objects.create(
            shift=shift,
            amount='10.00',
            category=category,
            comment='paper',
            created_by=cashier_user,
            branch_id=shift.branch_id,
        )
        from cashbox.services.drawer import expected_payment_totals
        canonical = {
            method: str(value)
            for method, value in expected_payment_totals(shift).items()
        }
        assert canonical == amounts
        # Reconciliation is fail-closed until the immutable close bundle is
        # present. This test builds the same manifest end_shift publishes after
        # all tender and expense rows have been persisted/synced.
        from core.shifts.service import _build_settlement_manifest
        frozen_rows = list(
            ShiftPaymentTotal.objects.filter(shift=shift).order_by('method')
        )
        shift.settlement_manifest = _build_settlement_manifest(
            shift, frozen_rows,
        )
        shift.save(update_fields=['settlement_manifest'])

        result, status = ShiftService.reconcile(
            shift.id,
            actual_cash='100.00',
            notes='manager accepted',
            reconciled_by_id=admin_user.id,
            actor=admin_user,
            confirmed=amounts,
        )
        assert status == 201, result
        assert result['data']['treasury_posting']['total'] == '240.00'

        detail, detail_status = ShiftService.get(shift.id, actor=admin_user)
        assert detail_status == 200, detail
        data = detail['data']
        assert data['expected_by_tender'] == amounts
        assert data['total_expected_to_receive'] == '240.00'
        assert data['reconciled_count'] == 5
        assert {row['method'] for row in data['settlement']} == set(amounts)
        assert {row['status'] for row in data['settlement']} == {'CONFIRMED'}
        assert data['treasury_posting']['status'] == 'posted'
        assert data['treasury_posting']['total'] == '240.00'
        assert data['cashbox_expenses'][0]['id'] == expense.id
        assert data['cashbox_expenses'][0]['description'] == 'paper'
        assert data['cashbox_expenses'][0]['category'] == 'Supplies'
        assert data['cashbox_expenses'][0]['paid_by']['id'] == cashier_user.id
        assert data['cashbox_expenses'][0]['status'] == 'RECORDED'

        listed, list_status = ShiftService.list(
            user_id=cashier_user.id, per_page=50,
        )
        assert list_status == 200, listed
        row = next(r for r in listed['data']['shifts'] if r['id'] == shift.id)
        assert row['expected_by_tender'] == amounts
        assert row['reconciled_count'] == 5
        summary = listed['data']['summary']
        assert summary['expected_by_tender'] == amounts
        assert summary['confirmed_by_tender'] == amounts
        assert summary['total_expected_to_receive'] == '240.00'
        assert summary['total_confirmed_received'] == '240.00'

    def test_null_payment_method_counts_as_cash(self, cashier_user, regular_user):
        from django.utils import timezone
        from base.models import Order
        s = self._active_shift(cashier_user)
        Order.objects.create(
            user=regular_user, cashier=cashier_user, status='COMPLETED',
            is_paid=True, payment_method=None, display_id=1,
            subtotal='40.00', total_amount='40.00', paid_at=timezone.now())
        row = self._list_rows(cashier_user)[s.id]
        assert row['payment_mix']['cash'] == '40.00'
        assert row['payment_mix']['card'] == '0.00'
        assert row['paid_orders'] == 1

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
        assert row['payment_mix']['card'] == '70.00'   # HUMO folds into card
        assert row['total_revenue'] == '70.00'

    def test_two_shifts_same_cashier_no_double_count(self, cashier_user, regular_user):
        """A boundary-instant order (shift2.start == shift1.end) must count in
        EXACTLY ONE shift (the later one), never both."""
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Order, Shift
        from django.conf import settings
        t0 = timezone.now() - timedelta(hours=4)
        t1 = timezone.now() - timedelta(hours=2)
        t2 = timezone.now() - timedelta(minutes=1)
        s1 = Shift.objects.create(user=cashier_user, start_time=t0, end_time=t1,
                                  status='COMPLETED', total_revenue='100.00',
                                  total_orders=1, cash_collected='100.00',
                                  branch_id=settings.BRANCH_ID)
        s2 = Shift.objects.create(user=cashier_user, start_time=t1, end_time=t2,
                                  status='COMPLETED', total_revenue='200.00',
                                  total_orders=2, cash_collected='200.00',
                                  branch_id=settings.BRANCH_ID)

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
        c1 = rows[s1.id]['paid_orders']
        c2 = rows[s2.id]['paid_orders']
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

    def test_live_summary_query_count_is_constant_in_shift_count(
        self, cashier_user, regular_user,
    ):
        """Global KPIs and paged rows reuse one batched live-evidence pass.

        Before the regression fix, each live shift called _live_totals once in
        the summary and once in serialization, adding several queries per row.
        """
        from datetime import timedelta
        from django.conf import settings
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone
        from base.models import Order, Shift, User
        from base.security.hashing import hash_password
        from admins.services.shift_service import ShiftService

        password_hash = hash_password('1234')

        def live_shift(index, user=None):
            user = user or User.objects.create(
                first_name='Scale',
                last_name=str(index),
                email=f'shift-scale-{index}@test.local',
                password=password_hash,
                role=User.RoleChoices.CASHIER,
                status=User.UserStatus.ACTIVE,
            )
            start = timezone.now() - timedelta(hours=1)
            shift = Shift.objects.create(
                user=user,
                start_time=start,
                status=Shift.Status.ACTIVE,
                branch_id=settings.BRANCH_ID,
            )
            Order.objects.create(
                user=regular_user,
                cashier=user,
                branch_id=shift.branch_id,
                status=Order.Status.COMPLETED,
                is_paid=True,
                payment_method='CASH',
                display_id=2000 + index,
                subtotal='10.00',
                total_amount='10.00',
                paid_at=start + timedelta(minutes=1),
            )
            return shift

        live_shift(0, user=cashier_user)
        live_shift(1)
        # Warm lazy imports/caches before comparing database query counts.
        ShiftService.list(per_page=50)
        with CaptureQueriesContext(connection) as small:
            small_result, small_status = ShiftService.list(per_page=50)
        assert small_status == 200, small_result

        for index in range(2, 8):
            live_shift(index)
        with CaptureQueriesContext(connection) as large:
            large_result, large_status = ShiftService.list(per_page=50)
        assert large_status == 200, large_result

        assert len(large) == len(small), (
            'live-shift N+1 regression: '
            f'{len(small)} queries for 2 shifts vs {len(large)} for 8'
        )
        assert small_result['data']['summary']['total_revenue'] == '20.00'
        assert large_result['data']['summary']['total_revenue'] == '80.00'
