import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from admins.models import (
    ProductCostProfile,
    ProfitPeriodClose,
    ProfitabilityConfiguration,
)
from admins.services.profitability_service import (
    ProfitabilityError,
    approve_adjustment,
    classify_cashbox_expense,
    close_profit_period,
    create_adjustment,
    profitability_report,
    save_product_cost,
    save_recurring_cost,
    update_configuration,
)


pytestmark = pytest.mark.django_db


def _at(day, hour=12):
    return timezone.make_aware(
        datetime.combine(day, datetime.min.time()).replace(hour=hour),
        timezone.get_current_timezone(),
    )


def _paid_order(user, product, day, *, amount='100.00', quantity=1):
    from base.models import Order, OrderItem

    order = Order.objects.create(
        user=user,
        cashier=user,
        status=Order.Status.COMPLETED,
        is_paid=True,
        paid_at=_at(day),
        payment_method=Order.PaymentMethod.CASH,
        subtotal=Decimal(amount),
        total_amount=Decimal(amount),
        branch_id='branch1',
    )
    item = OrderItem.objects.create(
        order=order,
        product=product,
        quantity=quantity,
        price=Decimal(amount) / quantity,
        original_price=Decimal(amount) / quantity,
        branch_id='branch1',
    )
    return order, item


def _ready_config(admin_user, end, *, start=date(2026, 8, 15)):
    return update_configuration('branch1', {
        'reporting_start_date': start.isoformat(),
        'payroll_confirmed_through': end.isoformat(),
        'fixed_costs_confirmed_through': end.isoformat(),
    }, admin_user)


def test_verified_standard_cost_and_recurring_accrual_produce_net_profit(
    admin_user, product,
):
    start, end = date(2026, 8, 15), date(2026, 8, 31)
    _ready_config(admin_user, end)
    _paid_order(admin_user, product, date(2026, 8, 20), amount='100.00', quantity=2)
    save_product_cost('branch1', {
        'product_id': product.id,
        'treatment': 'STANDARD',
        'standard_unit_cost': '20.0000',
        'effective_from': start.isoformat(),
    }, admin_user)
    save_recurring_cost('branch1', {
        'name': 'August rent',
        'reporting_group': 'RENT',
        'monthly_amount': '3100.00',
        'start_date': '2026-08-01',
    }, admin_user)

    report = profitability_report(start, end, branch_id='branch1', live=True)

    assert report['status'] == 'PROVISIONAL'
    assert report['summary']['net_sales'] == '100.00'
    assert report['summary']['cogs'] == '40.00'
    assert report['summary']['gross_profit'] == '60.00'
    # 17/31 days of the 3,100 monthly schedule.
    assert report['summary']['rent'] == '1700.00'
    assert report['summary']['net_profit'] == '-1640.00'
    assert report['coverage']['costed_revenue_pct'] == '100.00'
    assert report['coverage']['can_close'] is True
    assert report['coverage']['blockers'] == []
    assert report['breakdown']['cost_sources'][1] == {
        'source': 'VERIFIED_STANDARD',
        'line_count': 1,
        'revenue': '100.00',
        'cost': '40.00',
    }


def test_missing_product_cost_is_visible_and_blocks_close(admin_user, product):
    end = date(2026, 8, 31)
    _ready_config(admin_user, end)
    _paid_order(admin_user, product, date(2026, 8, 20), amount='125.00')

    report = profitability_report('2026-08-15', '2026-08-31', branch_id='branch1')

    assert report['summary']['cogs'] == '0.00'
    assert report['coverage']['costed_revenue_pct'] == '0.00'
    assert report['coverage']['can_close'] is False
    assert report['coverage']['missing_cost_products'] == [{
        'product_id': product.id,
        'product_name': product.name,
        'quantity': 1,
        'revenue': '125.00',
    }]
    assert {
        row['code'] for row in report['coverage']['blockers']
    } == {'MISSING_PRODUCT_COSTS'}


def test_actual_stock_cost_and_physical_return_reverse_cogs(
    admin_user, product,
):
    from base.models import OrderRefund
    from stock.models import StockItem, StockLocation, StockTransaction, StockUnit

    end = date(2026, 8, 31)
    _ready_config(admin_user, end)
    order, order_item = _paid_order(
        admin_user, product, date(2026, 8, 20), amount='100.00',
    )
    unit = StockUnit.objects.create(
        name='piece', short_name='pc', unit_type='COUNT', branch_id='branch1',
    )
    location = StockLocation.objects.create(
        name='Kitchen', type='KITCHEN', branch_id='branch1',
    )
    stock_item = StockItem.objects.create(
        name='Meal ingredients', base_unit=unit, item_type='RAW',
        avg_cost_price='30.0000', branch_id='branch1',
    )
    sold = StockTransaction.objects.create(
        transaction_number='TRX-ACTUAL-SALE',
        stock_item=stock_item,
        location=location,
        movement_type='SALE_OUT',
        quantity='1.0000',
        unit=unit,
        base_quantity='1.0000',
        quantity_before='10.0000',
        quantity_after='9.0000',
        unit_cost='30.0000',
        total_cost='30.0000',
        order=order,
        order_item=order_item,
        user=admin_user,
        branch_id='branch1',
    )
    refund = OrderRefund.objects.create(
        order=order,
        amount='100.00',
        cash_amount='100.00',
        drawer_cash_amount='100.00',
        refunded_at=_at(date(2026, 8, 21)),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id='profitability-test-refund',
        branch_id='branch1',
    )
    returned = StockTransaction.objects.create(
        transaction_number='TRX-ACTUAL-RETURN',
        stock_item=stock_item,
        location=location,
        movement_type='RETURN_FROM_CUSTOMER',
        quantity='1.0000',
        unit=unit,
        base_quantity='1.0000',
        quantity_before='9.0000',
        quantity_after='10.0000',
        unit_cost='30.0000',
        total_cost='30.0000',
        reference_type='ORDER_REVERSAL',
        reference_id=order.id,
        order=order,
        order_item=order_item,
        user=admin_user,
        branch_id='branch1',
    )
    StockTransaction.objects.filter(pk=returned.pk).update(
        created_at=_at(date(2026, 8, 21)),
    )

    report = profitability_report('2026-08-20', '2026-08-21', branch_id='branch1')

    assert sold.order_item_id == order_item.id
    assert Decimal(refund.amount) == Decimal('100.00')
    assert report['summary']['net_sales'] == '0.00'
    assert report['summary']['cogs'] == '0.00'
    assert report['breakdown']['actual_inventory_return_credit'] == '30.00'
    assert report['coverage']['actual_cost_revenue_pct'] == '100.00'
    assert report['coverage']['can_close'] is True


def test_actual_cogs_nets_non_refund_line_returns(admin_user, product):
    from stock.models import StockItem, StockLocation, StockTransaction, StockUnit

    end = date(2026, 8, 31)
    _ready_config(admin_user, end)
    order, order_item = _paid_order(
        admin_user, product, date(2026, 8, 20), amount='100.00', quantity=2,
    )
    unit = StockUnit.objects.create(
        name='piece', short_name='pc', unit_type='COUNT', branch_id='branch1',
    )
    location = StockLocation.objects.create(
        name='Kitchen', type='KITCHEN', branch_id='branch1',
    )
    stock_item = StockItem.objects.create(
        name='Meal ingredients', base_unit=unit, item_type='RAW',
        avg_cost_price='30.0000', branch_id='branch1',
    )
    common = {
        'stock_item': stock_item,
        'location': location,
        'unit': unit,
        'quantity_before': '10.0000',
        'order': order,
        'order_item': order_item,
        'user': admin_user,
        'branch_id': 'branch1',
    }
    StockTransaction.objects.create(
        transaction_number='TRX-EDIT-SALE',
        movement_type='SALE_OUT', quantity='2.0000', base_quantity='2.0000',
        quantity_after='8.0000', unit_cost='30.0000', total_cost='60.0000',
        **common,
    )
    StockTransaction.objects.create(
        transaction_number='TRX-EDIT-RETURN',
        movement_type='RETURN_FROM_CUSTOMER', quantity='1.0000',
        base_quantity='1.0000', quantity_after='9.0000',
        unit_cost='30.0000', total_cost='30.0000', **common,
    )

    report = profitability_report(
        '2026-08-15', '2026-08-31', branch_id='branch1', live=True,
    )

    assert report['summary']['cogs'] == '30.00'
    actual = next(
        row for row in report['breakdown']['cost_sources']
        if row['source'] == 'ACTUAL_STOCK'
    )
    assert actual['cost'] == '30.00'
    assert report['coverage']['can_close'] is True


def test_refunds_are_applied_before_non_product_revenue(admin_user, product):
    from base.models import OrderRefund

    _ready_config(admin_user, date(2026, 8, 31))
    order, _item = _paid_order(
        admin_user, product, date(2026, 8, 20), amount='100.00',
    )
    save_product_cost('branch1', {
        'product_id': product.id,
        'treatment': 'ZERO',
        'effective_from': '2026-08-15',
    }, admin_user)
    OrderRefund.objects.create(
        order=order,
        amount='100.00',
        cash_amount='100.00',
        drawer_cash_amount='100.00',
        refunded_at=_at(date(2026, 8, 21)),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id='product-revenue-refund',
        branch_id='branch1',
    )

    report = profitability_report(
        '2026-08-15', '2026-08-31', branch_id='branch1', live=True,
    )

    assert report['summary']['net_sales'] == '0.00'
    assert report['breakdown']['product_revenue'] == '0.00'
    assert report['breakdown']['product_refunds'] == '100.00'
    assert report['breakdown']['non_product_revenue'] == '0.00'


def test_prelaunch_range_is_not_silently_mixed_with_profit_period(admin_user):
    ProfitabilityConfiguration.objects.create(
        branch_id='branch1',
        reporting_start_date=date(2026, 8, 15),
        updated_by=admin_user,
    )

    report = profitability_report('2026-07-01', '2026-07-31', branch_id='branch1')

    assert report['status'] == 'NOT_STARTED'
    assert report['range']['effective_from'] is None
    assert report['coverage']['blockers'][0]['code'] == 'REPORTING_NOT_STARTED'


def test_owner_draw_stays_out_of_profit_but_remains_in_cash_movement(
    admin_user, product,
):
    from base.models import Shift
    from cashbox.models import CashboxExpense
    from hr.models import CashTransaction

    end = date(2026, 8, 31)
    _ready_config(admin_user, end)
    _paid_order(admin_user, product, date(2026, 8, 20), amount='100.00')
    save_product_cost('branch1', {
        'product_id': product.id,
        'treatment': 'ZERO',
        'effective_from': '2026-08-15',
    }, admin_user)
    shift = Shift.objects.create(
        user=admin_user,
        start_time=_at(date(2026, 8, 20), 8),
        end_time=_at(date(2026, 8, 20), 18),
        status=Shift.Status.ENDED,
        branch_id='branch1',
    )
    payout = CashboxExpense.objects.create(
        shift=shift,
        amount='20.00',
        comment='Owner withdrawal',
        created_by=admin_user,
        branch_id='branch1',
    )
    CashboxExpense.objects.filter(pk=payout.pk).update(
        created_at=_at(date(2026, 8, 20), 15),
    )
    duplicate_ledger_row = CashTransaction.objects.create(
        type=CashTransaction.TransactionType.WITHDRAWAL,
        amount='20.00',
        description='Same owner withdrawal in the general cash ledger',
        payment_method=CashTransaction.PaymentMethod.CASH,
        balance_before='100.00',
        balance_after='80.00',
        performed_by=admin_user,
        branch_id='branch1',
    )
    CashTransaction.objects.filter(pk=duplicate_ledger_row.pk).update(
        created_at=_at(date(2026, 8, 20), 15),
    )

    unclassified = profitability_report(
        '2026-08-15', '2026-08-31', branch_id='branch1', live=True,
    )
    assert unclassified['summary']['net_profit'] == '100.00'
    assert unclassified['cash_flow']['known_net_movement'] == '60.00'
    assert unclassified['coverage']['unclassified_cashbox_count'] == 1

    with pytest.raises(ProfitabilityError) as missing_note:
        classify_cashbox_expense('branch1', payout.id, {
            'reporting_group': 'OWNER_DRAW',
            'cash_movement_represented_elsewhere': True,
        }, admin_user)
    assert 'note is required' in str(missing_note.value)

    classify_cashbox_expense('branch1', payout.id, {
        'reporting_group': 'OWNER_DRAW',
        'cash_movement_represented_elsewhere': True,
        'note': 'The same payout is present in the general cash ledger.',
    }, admin_user)
    classified = profitability_report(
        '2026-08-15', '2026-08-31', branch_id='branch1', live=True,
    )

    assert classified['summary']['net_profit'] == '100.00'
    assert classified['cash_flow']['known_net_movement'] == '80.00'
    assert classified['coverage']['can_close'] is True


def test_payroll_accrues_but_approved_unpaid_expense_is_not_realized(
    admin_user, product,
):
    from hr.models import Employee, Expense, ExpenseCategory, SalaryPayment

    end = date(2026, 8, 31)
    _ready_config(admin_user, end)
    _paid_order(admin_user, product, date(2026, 8, 20), amount='2500.00')
    save_product_cost('branch1', {
        'product_id': product.id,
        'treatment': 'ZERO',
        'effective_from': '2026-08-15',
    }, admin_user)
    employee = Employee.objects.create(
        user=admin_user,
        position='Manager',
        hire_date=date(2026, 1, 1),
        base_salary='3100.00',
        branch_id='branch1',
    )
    SalaryPayment.objects.create(
        employee=employee,
        period_year=2026,
        period_month=8,
        base_amount='3100.00',
        net_amount='3100.00',
        status=SalaryPayment.Status.APPROVED,
        created_by=admin_user,
        approved_by=admin_user,
        branch_id='branch1',
    )
    SalaryPayment.objects.create(
        employee=employee,
        period_year=2026,
        period_month=9,
        base_amount='3100.00',
        net_amount='3100.00',
        status=SalaryPayment.Status.PENDING,
        created_by=admin_user,
        branch_id='branch1',
    )
    utilities = ExpenseCategory.objects.create(
        name='Utilities', reporting_group='UTILITIES',
    )
    Expense.objects.create(
        category=utilities,
        amount='310.00',
        description='Electricity',
        expense_date=date(2026, 8, 20),
        status=Expense.Status.APPROVED,
        created_by=admin_user,
        approved_by=admin_user,
        branch_id='branch1',
    )

    report = profitability_report(
        '2026-08-15', '2026-08-31', branch_id='branch1', live=True,
    )

    assert report['summary']['payroll'] == '1700.00'
    assert report['summary']['utilities'] == '0.00'
    assert report['summary']['net_profit'] == '800.00'
    assert report['coverage']['can_close'] is True
    assert all(
        blocker['code'] != 'PENDING_PAYROLL'
        for blocker in report['coverage']['blockers']
    )


def test_profitability_counts_canonical_paid_expense_and_fee_once(
    admin_user, product,
):
    from base.models import Shift, TreasuryAccount, TreasuryTransaction
    from cashbox.models import CashboxExpense
    from hr.models import Expense, ExpenseCategory

    end = date(2026, 8, 31)
    _ready_config(admin_user, end)
    _paid_order(admin_user, product, date(2026, 8, 20), amount='1000.00')
    save_product_cost('branch1', {
        'product_id': product.id,
        'treatment': 'ZERO',
        'effective_from': '2026-08-15',
    }, admin_user)
    category = ExpenseCategory.objects.create(
        code='CANONICAL_UTILITIES', name='Utilities',
        reporting_group='UTILITIES', allowed_sources=['DRAWER', 'BANK'],
    )
    common = {
        'category': category,
        'category_code_snapshot': category.code,
        'category_name_snapshot': category.name,
        'expense_date': date(2026, 8, 20),
        'created_by': admin_user,
        'branch_id': 'branch1',
    }
    Expense.objects.create(
        amount='100', status=Expense.Status.PENDING, **common,
    )
    Expense.objects.create(
        amount='200', status=Expense.Status.APPROVED, **common,
    )
    drawer_expense = Expense.objects.create(
        amount='300', status=Expense.Status.PAID,
        requested_source='DRAWER', paid_at=_at(date(2026, 8, 20)), **common,
    )
    shift = Shift.objects.create(
        user=admin_user,
        start_time=_at(date(2026, 8, 20), 8),
        status=Shift.Status.ACTIVE,
        branch_id='branch1',
    )
    cashbox = CashboxExpense.objects.create(
        shift=shift,
        canonical_category=category,
        canonical_expense=drawer_expense,
        amount='300',
        comment='Canonical drawer payment',
        created_by=admin_user,
        branch_id='branch1',
    )
    CashboxExpense.objects.filter(pk=cashbox.pk).update(
        created_at=_at(date(2026, 8, 20)),
    )
    bank = TreasuryAccount.objects.create(
        kind='BANK', balance='0', branch_id='branch1',
    )
    bank_payment = TreasuryTransaction.objects.create(
        account=bank,
        type=TreasuryTransaction.Type.EXPENSE,
        delta='-105',
        fee='5',
        balance_before='105',
        balance_after='0',
        branch_id='branch1',
    )
    Expense.objects.create(
        amount='100', fee_uzs='5', status=Expense.Status.PAID,
        requested_source='BANK', paid_at=_at(date(2026, 8, 20)),
        treasury_transaction=bank_payment, **common,
    )
    void_payment = TreasuryTransaction.objects.create(
        account=bank,
        type=TreasuryTransaction.Type.EXPENSE,
        delta='-50',
        balance_before='50',
        balance_after='0',
        branch_id='branch1',
    )
    void_reversal = TreasuryTransaction.objects.create(
        account=bank,
        type=TreasuryTransaction.Type.EXPENSE_REVERSAL,
        delta='50',
        balance_before='0',
        balance_after='50',
        reversal_of=void_payment,
        branch_id='branch1',
    )
    Expense.objects.create(
        amount='50', status=Expense.Status.VOIDED,
        requested_source='BANK', paid_at=_at(date(2026, 8, 20)),
        voided_at=_at(date(2026, 8, 21)),
        treasury_transaction=void_payment,
        treasury_reversal=void_reversal,
        **common,
    )

    report = profitability_report(
        '2026-08-15', '2026-08-31', branch_id='branch1', live=True,
    )

    assert report['summary']['utilities'] == '405.00'
    assert report['summary']['total_operating_expenses'] == '405.00'
    assert report['summary']['net_profit'] == '595.00'


def test_waste_stock_entry_posts_once_and_linked_reversal_removes_it(
    admin_user,
):
    from stock.models import StockItem, StockLocation, StockTransaction, StockUnit

    _ready_config(admin_user, date(2026, 8, 31))
    unit = StockUnit.objects.create(
        name='Waste kilogram', short_name='wkg', unit_type='WEIGHT',
        is_base_unit=True,
    )
    location = StockLocation.objects.create(
        name='Waste kitchen', type='KITCHEN', branch_id='branch1',
    )
    item = StockItem.objects.create(
        name='Waste ingredients', base_unit=unit, item_type='RAW',
        avg_cost_price='100', branch_id='branch1',
    )
    original = StockTransaction.objects.create(
        transaction_number='TRX-WASTE-PNL',
        stock_item=item,
        location=location,
        movement_type=StockTransaction.MovementType.WASTE,
        quantity='2',
        unit=unit,
        base_quantity='2',
        quantity_before='10',
        quantity_after='8',
        unit_cost='100',
        total_cost='200',
        reference_type='StockWaste',
        user=admin_user,
        branch_id='branch1',
    )
    StockTransaction.objects.filter(pk=original.pk).update(
        created_at=_at(date(2026, 8, 20)),
    )
    before_reversal = profitability_report(
        '2026-08-15', '2026-08-31', branch_id='branch1', live=True,
    )
    assert before_reversal['summary']['waste_spoilage'] == '200.00'

    reversal = StockTransaction.objects.create(
        transaction_number='TRX-WASTE-PNL-REVERSAL',
        stock_item=item,
        location=location,
        movement_type=StockTransaction.MovementType.ADJUSTMENT_PLUS,
        quantity='2',
        unit=unit,
        base_quantity='2',
        quantity_before='8',
        quantity_after='10',
        unit_cost='100',
        total_cost='200',
        reference_type='StockAdjustmentReversal',
        reference_id=original.id,
        reversal_of=original,
        user=admin_user,
        branch_id='branch1',
    )
    StockTransaction.objects.filter(pk=reversal.pk).update(
        created_at=_at(date(2026, 8, 21)),
    )
    after_reversal = profitability_report(
        '2026-08-15', '2026-08-31', branch_id='branch1', live=True,
    )
    assert after_reversal['summary']['waste_spoilage'] == '0.00'


@pytest.mark.django_db(transaction=True)
def test_period_close_is_idempotent_immutable_and_revisioned(
    monkeypatch, admin_user, product,
):
    start, end = date(2026, 7, 1), date(2026, 7, 31)
    _ready_config(admin_user, end, start=start)
    _paid_order(admin_user, product, date(2026, 7, 10), amount='100.00')
    ProductCostProfile.objects.create(
        branch_id='branch1',
        product=product,
        treatment='ZERO',
        effective_from=start,
        verified_by=admin_user,
        verified_at=timezone.now(),
    )
    monkeypatch.setattr(
        'admins.services.profitability_service.business_date',
        lambda: date(2026, 9, 1),
    )

    first, created = close_profit_period('branch1', '2026-07', admin_user)
    repeated, repeated_created = close_profit_period('branch1', '2026-07', admin_user)

    assert created is True
    assert repeated_created is False
    assert repeated.id == first.id
    assert first.revision == 1
    with pytest.raises(ValidationError):
        first.save()
    with pytest.raises(ValidationError):
        first.delete()

    adjustment = create_adjustment('branch1', {
        'effective_date': '2026-07-20',
        'direction': 'EXPENSE',
        'reporting_group': 'OPERATING',
        'amount': '5.00',
        'description': 'Late verified invoice',
    }, admin_user)
    approve_adjustment('branch1', adjustment['id'], admin_user)
    with pytest.raises(ProfitabilityError) as missing_reason:
        close_profit_period('branch1', '2026-07', admin_user)
    assert 'correction reason is required' in str(missing_reason.value)

    corrected, corrected_created = close_profit_period(
        'branch1', '2026-07', admin_user,
        correction_reason='Added the approved late invoice',
    )
    assert corrected_created is True
    assert corrected.revision == 2
    assert corrected.report_snapshot['summary']['net_profit'] == '95.00'
    assert ProfitPeriodClose.objects.count() == 2


def test_profitability_api_is_admin_only_and_returns_canonical_contract(
    client, admin_user, product,
):
    _ready_config(admin_user, date(2026, 8, 31))
    _paid_order(admin_user, product, date(2026, 8, 20), amount='50.00')
    save_product_cost('branch1', {
        'product_id': product.id,
        'treatment': 'ZERO',
        'effective_from': '2026-08-15',
    }, admin_user)

    path = '/api/admins/finance/profitability?from=2026-08-15&to=2026-08-31'
    assert client.get(path).status_code == 401
    login = client.post(
        '/api/admins/auth-login',
        data=json.dumps({'email': admin_user.email, 'password': 'adminpass'}),
        content_type='application/json',
    )
    assert login.status_code == 200

    response = client.get(path)

    assert response.status_code == 200, response.content
    data = response.json()['data']
    assert data['summary']['net_profit'] == '50.00'
    assert data['cash_flow']['known_inflows'] == '50.00'
    assert data['coverage']['can_close'] is True
    assert data['source_policy']['sales_clock'] == 'Order.paid_at'


def test_setup_write_apis_support_frontend_workflow(client, admin_user, product):
    from cashbox.models import CashboxExpenseCategory

    login = client.post(
        '/api/admins/auth-login',
        data=json.dumps({'email': admin_user.email, 'password': 'adminpass'}),
        content_type='application/json',
    )
    assert login.status_code == 200

    configured = client.patch(
        '/api/admins/finance/profitability/setup',
        data=json.dumps({
            'reporting_start_date': '2026-08-15',
            'payroll_confirmed_through': '2026-08-31',
            'fixed_costs_confirmed_through': '2026-08-31',
        }),
        content_type='application/json',
    )
    assert configured.status_code == 200, configured.content
    assert configured.json()['data']['saved'] is True

    cost = client.post(
        '/api/admins/finance/profitability/product-costs',
        data=json.dumps({
            'product_id': product.id,
            'treatment': 'STANDARD',
            'standard_unit_cost': '3.2500',
            'effective_from': '2026-08-15',
        }),
        content_type='application/json',
    )
    assert cost.status_code == 201, cost.content
    assert cost.json()['data']['verified'] is True

    overlap = client.post(
        '/api/admins/finance/profitability/product-costs',
        data=json.dumps({
            'product_id': product.id,
            'treatment': 'ZERO',
            'effective_from': '2026-08-20',
        }),
        content_type='application/json',
    )
    assert overlap.status_code == 422
    assert 'overlap' in overlap.json()['message'].lower()

    recurring = client.post(
        '/api/admins/finance/profitability/recurring-costs',
        data=json.dumps({
            'name': 'Monthly rent',
            'reporting_group': 'RENT',
            'monthly_amount': '1000.00',
            'start_date': '2026-08-01',
        }),
        content_type='application/json',
    )
    assert recurring.status_code == 201, recurring.content

    category = CashboxExpenseCategory.objects.create(
        name='Electricity', branch_id='branch1',
    )
    invalid_group = client.put(
        f'/api/admins/finance/profitability/categories/cashbox/{category.id}',
        data=json.dumps({'reporting_group': 'OTHER_INCOME'}),
        content_type='application/json',
    )
    assert invalid_group.status_code == 422
    categorized = client.put(
        f'/api/admins/finance/profitability/categories/cashbox/{category.id}',
        data=json.dumps({'reporting_group': 'UTILITIES'}),
        content_type='application/json',
    )
    assert categorized.status_code == 200, categorized.content
    assert categorized.json()['data']['reporting_group'] == 'UTILITIES'

    draft = client.post(
        '/api/admins/finance/profitability/adjustments',
        data=json.dumps({
            'effective_date': '2026-08-20',
            'direction': 'INCOME',
            'amount': '10.00',
            'description': 'Verified rebate',
        }),
        content_type='application/json',
    )
    assert draft.status_code == 201, draft.content
    adjustment_id = draft.json()['data']['id']
    approved = client.post(
        f'/api/admins/finance/profitability/adjustments/{adjustment_id}/approve',
        data='{}',
        content_type='application/json',
    )
    assert approved.status_code == 200, approved.content
    assert approved.json()['data']['status'] == 'APPROVED'

    disposable = client.post(
        '/api/admins/finance/profitability/adjustments',
        data=json.dumps({
            'effective_date': '2026-08-21',
            'direction': 'EXPENSE',
            'reporting_group': 'OPERATING',
            'amount': '1.00',
            'description': 'Mistaken draft',
        }),
        content_type='application/json',
    )
    assert disposable.status_code == 201, disposable.content
    disposable_id = disposable.json()['data']['id']
    deleted = client.delete(
        f'/api/admins/finance/profitability/adjustments/{disposable_id}',
    )
    assert deleted.status_code == 200, deleted.content
    assert deleted.json()['data'] == {'id': disposable_id, 'deleted': True}

    setup = client.get('/api/admins/finance/profitability/setup')
    assert setup.status_code == 200, setup.content
    data = setup.json()['data']
    assert any(row['id'] == product.id for row in data['products'])
    assert len(data['product_costs']) == 1
    assert len(data['recurring_costs']) == 1
    assert data['adjustments'][0]['status'] == 'APPROVED'


def test_partial_product_cost_patch_preserves_unverified_state(
    admin_user, product,
):
    created = save_product_cost('branch1', {
        'product_id': product.id,
        'treatment': 'ZERO',
        'effective_from': '2026-08-15',
        'verified': False,
        'note': 'Needs an invoice',
    }, admin_user)

    updated = save_product_cost(
        'branch1', {'note': 'Invoice still pending'}, admin_user,
        profile_id=created['id'],
    )

    assert updated['verified'] is False
    assert updated['verified_at'] is None
    profile = ProductCostProfile.objects.get(pk=created['id'])
    assert profile.verified_by_id is None
    assert profile.note == 'Invoice still pending'


@pytest.mark.parametrize('bad_date', ['garbage', '2026-99-99'])
def test_malformed_optional_dates_return_422(
    client, admin_user, product, bad_date,
):
    login = client.post(
        '/api/admins/auth-login',
        data=json.dumps({'email': admin_user.email, 'password': 'adminpass'}),
        content_type='application/json',
    )
    assert login.status_code == 200

    requests = [
        (
            'post', '/api/admins/finance/profitability/product-costs',
            {
                'product_id': product.id,
                'treatment': 'ZERO',
                'effective_from': '2026-08-15',
                'effective_to': bad_date,
            },
            'effective_to',
        ),
        (
            'post', '/api/admins/finance/profitability/recurring-costs',
            {
                'name': 'Rent',
                'reporting_group': 'RENT',
                'monthly_amount': '100.00',
                'start_date': '2026-08-01',
                'end_date': bad_date,
            },
            'end_date',
        ),
        (
            'patch', '/api/admins/finance/profitability/setup',
            {'payroll_confirmed_through': bad_date},
            'payroll_confirmed_through',
        ),
    ]
    for method, path, payload, field in requests:
        response = getattr(client, method)(
            path, data=json.dumps(payload), content_type='application/json',
        )
        assert response.status_code == 422, response.content
        assert response.json()['errors'][field] == 'Invalid date'


def test_setup_paginates_all_payouts_beyond_old_200_row_cutoff(
    client, admin_user,
):
    from base.models import Shift
    from cashbox.models import CashboxExpense

    update_configuration('branch1', {
        'reporting_start_date': '2026-08-15',
    }, admin_user)
    shift = Shift.objects.create(
        user=admin_user,
        start_time=_at(date(2026, 8, 19), 8),
        status=Shift.Status.ACTIVE,
        branch_id='branch1',
    )
    CashboxExpense.objects.bulk_create([
        CashboxExpense(
            shift=shift,
            amount='1.00',
            comment=f'Payout {index}',
            created_by=admin_user,
            branch_id='branch1',
        )
        for index in range(205)
    ])
    login = client.post(
        '/api/admins/auth-login',
        data=json.dumps({'email': admin_user.email, 'password': 'adminpass'}),
        content_type='application/json',
    )
    assert login.status_code == 200

    response = client.get(
        '/api/admins/finance/profitability/setup'
        '?payout_page=3&payout_page_size=100&payout_status=unresolved'
    )

    assert response.status_code == 200, response.content
    data = response.json()['data']
    assert len(data['cashbox_expenses']) == 5
    assert data['cashbox_expenses_pagination'] == {
        'page': 3,
        'page_size': 100,
        'total_items': 205,
        'total_pages': 3,
        'has_previous': True,
        'has_next': False,
        'status': 'unresolved',
    }


def test_profit_close_requests_repeatable_read_on_postgresql(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from admins.services import profitability_service

    cursor = MagicMock()
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    fake_connection = SimpleNamespace(
        vendor='postgresql', cursor=lambda: cursor_context,
    )
    monkeypatch.setattr(profitability_service, 'connection', fake_connection)

    profitability_service._use_repeatable_read_snapshot()

    cursor.execute.assert_called_once_with(
        'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'
    )
