import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db.models import Sum
from django.utils import timezone

from base.models import IdempotencyKey, Order, OrderPayment
from smartfood.models import BotConfig, LoyaltyTransaction, Redemption, Reward
from smartfood.services.cart_service import price_cart
from smartfood.services.loyalty_service import LoyaltyService

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('field,value', [
    ('loyalty_earn_per', -1), ('loyalty_point_value', -1),
    ('reward_valid_days', 0), ('reward_valid_days', 3651),
])
def test_operator_forms_reject_invalid_loyalty_configuration(cfg, field, value):
    from django.forms import modelform_factory

    form_type = modelform_factory(BotConfig, fields=[field])
    form = form_type(data={field: value}, instance=cfg)
    assert not form.is_valid()
    assert field in form.errors


def gift(customer, *, stock=2):
    LoyaltyService.record(customer.pk, 'GRANT', 500, reason='Opening award')
    return Reward.objects.create(name_en='Coffee', kind='CUSTOM', points_cost=100, stock=stock)


def post(client, path, data=None, key=None):
    headers = {'HTTP_IDEMPOTENCY_KEY': key} if key else {}
    return client.post(path, data=json.dumps(data or {}), content_type='application/json', **headers)


@pytest.mark.parametrize('basis,expected_earn', [
    ('PAID_MERCHANDISE', 0), ('MERCHANDISE_SUBTOTAL', 39),
])
def test_spend_cap_and_configurable_earn(cfg, customer, product, basis, expected_earn):
    cfg.loyalty_point_value = Decimal('50')
    cfg.loyalty_earning_basis = basis
    cfg.save()
    customer.loyalty_points = 2000
    priced = price_cart([{'product_id': product.pk, 'quantity': 1}],
                        points_used=2000, customer=customer)
    assert priced['points_used'] == 780
    assert priced['discount'] == Decimal('39000')
    assert priced['points_earned'] == expected_earn
    assert priced['loyalty_policy_snapshot']['earning_basis'] == basis


def test_fractional_point_never_loses_its_remaining_value(cfg, customer, product):
    cfg.loyalty_point_value = Decimal('10000.25')
    cfg.save()
    customer.loyalty_points = 50
    priced = price_cart([{'product_id': product.pk}], points_used=50, customer=customer)
    assert priced['points_used'] == 3
    assert priced['discount'] == Decimal('30000.75')


def test_reward_key_required_replay_and_changed_target(auth_client, customer):
    reward = gift(customer)
    path = f'/api/smartfood/rewards/{reward.pk}/redeem'
    assert post(auth_client, path).status_code == 422
    first = post(auth_client, path, key='gift-1')
    replay = post(auth_client, path, key='gift-1')
    assert first.status_code == replay.status_code == 201
    assert first.content == replay.content
    other = Reward.objects.create(name_en='Tea', points_cost=50)
    changed = post(auth_client, f'/api/smartfood/rewards/{other.pk}/redeem', key='gift-1')
    assert changed.status_code == 409
    assert changed.json()['code'] == 'IDEMPOTENCY_KEY_REUSED'
    customer.refresh_from_db()
    reward.refresh_from_db()
    assert customer.loyalty_points == 400 and reward.stock == 1
    assert Redemption.objects.count() == 1


def test_failure_rolls_back_gift_stock_points_and_command(auth_client, customer):
    reward = gift(customer)
    with patch.object(LoyaltyService, 'record', side_effect=RuntimeError('simulated ledger failure')):
        response = post(auth_client, f'/api/smartfood/rewards/{reward.pk}/redeem', key='failed')
        assert response.status_code == 500
    customer.refresh_from_db()
    reward.refresh_from_db()
    assert customer.loyalty_points == 500 and reward.stock == 2
    assert not Redemption.objects.exists()
    assert not IdempotencyKey.objects.filter(key='failed').exists()


def test_cancel_is_atomic_and_restores_stock_once(customer):
    reward = gift(customer)
    result, status = LoyaltyService.redeem(customer, reward.pk)
    assert status == 201
    red = Redemption.objects.get(pk=result['data']['redemption']['id'])
    snapshot = red.reward_snapshot
    reward.name_en = 'Changed'
    reward.discount_amount = 9999
    reward.save(update_fields=['name_en', 'discount_amount'])
    first, status = LoyaltyService.cancel(red.code, 'Changed my mind', customer_id=customer.pk)
    replay, status2 = LoyaltyService.cancel(red.code, 'Changed my mind', customer_id=customer.pk)
    assert status == status2 == 200 and first == replay
    customer.refresh_from_db()
    reward.refresh_from_db()
    red.refresh_from_db()
    assert customer.loyalty_points == 500 and reward.stock == 2
    assert red.reward_snapshot == snapshot
    assert red.txns.filter(kind='REFUND').count() == 1


def test_cancel_failure_does_not_restore_partial_state(customer):
    reward = gift(customer)
    result, _ = LoyaltyService.redeem(customer, reward.pk)
    red = Redemption.objects.get(pk=result['data']['redemption']['id'])
    with patch.object(Redemption, 'save', side_effect=RuntimeError('simulated failure')):
        with pytest.raises(RuntimeError):
            LoyaltyService.cancel(red.code, 'Cancel', customer_id=customer.pk)
    customer.refresh_from_db()
    reward.refresh_from_db()
    red.refresh_from_db()
    assert customer.loyalty_points == 400 and reward.stock == 1
    assert red.status == 'ISSUED' and not red.txns.filter(kind='REFUND').exists()


def test_expiration_returns_points_once_and_blocks_fulfillment(customer):
    reward = gift(customer)
    result, _ = LoyaltyService.redeem(customer, reward.pk)
    red = Redemption.objects.get(pk=result['data']['redemption']['id'])
    red.expires_at = timezone.now() - timedelta(seconds=1)
    red.save(update_fields=['expires_at'])
    assert LoyaltyService.fulfill(red.code)[1] == 400
    assert LoyaltyService.expire_due() == 1
    assert LoyaltyService.expire_due() == 0
    red.refresh_from_db()
    assert red.status == 'EXPIRED'
    customer.refresh_from_db()
    assert customer.loyalty_points == 500


@pytest.mark.parametrize('value', [True, False, 1.5, '1e3', 'NaN', 'Infinity', '', {}, [], 2**40])
def test_grant_rejects_invalid_points(customer, value):
    result, status = LoyaltyService.grant(f'SF-{customer.telegram_id}', value, reason='Adjustment')
    assert status == 422, result
    assert not customer.loyalty_txns.exists()


def test_grant_requires_reason_and_returns_fresh_member(customer):
    assert LoyaltyService.grant(f'SF-{customer.telegram_id}', 1)[1] == 422
    result, status = LoyaltyService.grant(f'SF-{customer.telegram_id}', 20, reason='Service recovery')
    assert status == 200
    assert result['data']['balance'] == result['data']['member']['points'] == 20


def test_negative_balance_matches_ledger_and_cannot_spend(customer):
    reward = gift(customer)
    LoyaltyService.record(customer.pk, 'ADJUST', -700, reason='Reversed spent earnings')
    customer.refresh_from_db()
    assert customer.loyalty_points == -200
    assert customer.loyalty_txns.aggregate(total=Sum('points'))['total'] == -200
    assert LoyaltyService.redeem(customer, reward.pk)[1] == 400
    assert LoyaltyService.get(customer)[0]['data']['available_points'] == 0


def test_no_value_free_delivery_reward_is_unavailable(customer):
    reward = gift(customer)
    reward.kind = 'FREE_DELIVERY'
    reward.save()
    assert LoyaltyService.rewards(customer)[0]['data']['items'] == []
    assert LoyaltyService.redeem(customer, reward.pk)[1] == 400


def receipt(customer, cashier):
    action = uuid4()
    order = Order.objects.create(
        user=cashier, cashier=cashier, branch_id='branch1', status='COMPLETED',
        phone_number=customer.phone_number, is_paid=True, paid_at=timezone.now(),
        subtotal='25000', total_amount='25000', payment_method='CASH', payment_action_id=action,
    )
    OrderPayment.objects.create(order=order, method='CASH', amount='25000',
                                payment_action_id=action, line_index=0, branch_id='branch1')
    return order


def test_scan_uses_receipt_identity_and_blocks_second_program(cfg, customer, cashier, manager):
    from notifications.services.loyalty_service import maybe_accrue
    manager.branch_id = 'branch1'
    manager.save()
    order = receipt(customer, cashier)
    result, status = LoyaltyService.award_scan(
        f'SF-{customer.telegram_id}', 999999, staff_id=manager.pk, order_id=order.pk,
    )
    assert status == 200, result
    assert result['data']['awarded'] == result['data']['member']['points'] == 25
    assert LoyaltyService.award_scan(f'SF-{customer.telegram_id}', staff_id=manager.pk,
                                     order_id=order.pk)[1] == 200
    assert LoyaltyTransaction.objects.filter(kind='EARN_SCAN', pos_order=order).count() == 1
    assert maybe_accrue(order) is None


def test_telegram_and_unverified_receipts_never_earn_legacy_stamps(customer, cashier):
    from notifications.services.loyalty_service import maybe_accrue
    order = receipt(customer, cashier)
    order.order_origin = 'TELEGRAM'
    order.save(update_fields=['order_origin'])
    assert maybe_accrue(order) is None
    order.order_origin = 'POS'
    order.paid_at = None
    order.save(update_fields=['order_origin', 'paid_at'])
    assert maybe_accrue(order) is None


def test_operator_permissions_and_idempotent_adjustment(operator_client, manager, customer):
    path = '/api/admins/smartfood/loyalty/grant'
    body = {'member_id': f'SF-{customer.telegram_id}', 'points': 25, 'reason': 'Recovery'}
    assert post(operator_client, path, body, 'adjust-1').status_code == 403
    manager.permissions = ['loyalty.adjust']
    manager.save(update_fields=['permissions'])
    from django.core.cache import cache
    cache.clear()
    first = post(operator_client, path, body, 'adjust-1')
    replay = post(operator_client, path, dict(reversed(list(body.items()))), 'adjust-1')
    assert first.status_code == replay.status_code == 200
    assert first.content == replay.content
    assert customer.loyalty_txns.count() == 1


def test_refunded_receipt_reverses_earned_points_once(cfg, customer, cashier, manager):
    manager.branch_id = 'branch1'; manager.save(update_fields=['branch_id'])
    order = receipt(customer, cashier)
    assert LoyaltyService.award_scan(f'SF-{customer.telegram_id}', staff_id=manager.pk, order_id=order.pk)[1] == 200
    LoyaltyService.record(customer.pk, 'ADJUST', -25, reason='Spent earned balance')
    order.status = 'CANCELED'; order.save(update_fields=['status'])
    assert LoyaltyService.reverse_refunded_scan(order.pk) is True
    assert LoyaltyService.reverse_refunded_scan(order.pk) is False
    customer.refresh_from_db()
    assert customer.loyalty_points == -25
    assert customer.loyalty_txns.aggregate(total=Sum('points'))['total'] == -25


def test_dynamic_config_changes_future_calculation_and_keeps_saved_policy(cfg, customer, product):
    from smartfood.services.config_service import BotConfigService
    initial = price_cart([{'product_id': product.pk}], customer=customer)
    changed, status = BotConfigService.update({'loyalty_earning_basis': 'MERCHANDISE_SUBTOTAL'})
    assert status == 200 and len(changed['data']['loyalty_earning_basis_choices']) == 2
    assert initial['loyalty_policy_snapshot']['earning_basis'] == 'PAID_MERCHANDISE'
    assert price_cart([{'product_id': product.pk}], customer=customer)['loyalty_policy_snapshot']['earning_basis'] == 'MERCHANDISE_SUBTOTAL'
    assert BotConfigService.update({'loyalty_earning_basis': 'UNSUPPORTED'})[1] == 422


def test_read_only_audit_reports_balance_mismatch(customer):
    from io import StringIO
    from django.core.management import call_command
    customer.loyalty_points = 25; customer.save(update_fields=['loyalty_points'])
    output = StringIO()
    call_command('audit_loyalty_waiter', stdout=output)
    report = json.loads(output.getvalue())
    assert report['findings']['loyalty_balance_mismatch']['example_ids'] == [customer.pk]
    customer.refresh_from_db()
    assert customer.loyalty_points == 25 and not customer.loyalty_txns.exists()
