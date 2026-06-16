"""Smart Club loyalty program — ledger, redemption, staff scan/grant/fulfill."""
import pytest
from decimal import Decimal

from smartfood.models import (
    BotConfig, Customer, Reward, Redemption, LoyaltyTransaction,
)
from smartfood.services.loyalty_service import LoyaltyService


@pytest.fixture
def customer(db):
    return Customer.objects.create(telegram_id=55501, first_name='Test', loyalty_points=0)


@pytest.fixture
def reward(db):
    return Reward.objects.create(
        name_uz='Bepul kofe', name_en='Free coffee',
        kind=Reward.Kind.FREE_PRODUCT, points_cost=100, is_active=True)


@pytest.mark.django_db
def test_record_updates_balance_and_writes_ledger(customer):
    txn, bal = LoyaltyService.record(customer.id, LoyaltyTransaction.Kind.GRANT, 250, reason='welcome')
    assert bal == 250
    customer.refresh_from_db()
    assert customer.loyalty_points == 250
    assert txn.balance_after == 250 and txn.points == 250
    assert LoyaltyTransaction.objects.filter(customer=customer).count() == 1


@pytest.mark.django_db
def test_record_clamps_at_zero(customer):
    LoyaltyService.record(customer.id, LoyaltyTransaction.Kind.GRANT, 30)
    _, bal = LoyaltyService.record(customer.id, LoyaltyTransaction.Kind.ADJUST, -100)
    assert bal == 0


@pytest.mark.django_db
def test_redeem_deducts_points_and_mints_code(customer, reward):
    LoyaltyService.record(customer.id, LoyaltyTransaction.Kind.GRANT, 150)
    customer.refresh_from_db()
    result, status = LoyaltyService.redeem(customer, reward.id)
    assert status == 201, result
    code = result['data']['redemption']['code']
    assert code.startswith('GIFT-')
    customer.refresh_from_db()
    assert customer.loyalty_points == 50            # 150 - 100
    red = Redemption.objects.get(code=code)
    assert red.status == Redemption.Status.ISSUED and red.points_spent == 100
    assert LoyaltyTransaction.objects.filter(
        customer=customer, kind=LoyaltyTransaction.Kind.REDEEM).exists()


@pytest.mark.django_db
def test_redeem_rejects_when_insufficient(customer, reward):
    LoyaltyService.record(customer.id, LoyaltyTransaction.Kind.GRANT, 40)
    result, status = LoyaltyService.redeem(customer, reward.id)
    assert not result['success']
    customer.refresh_from_db()
    assert customer.loyalty_points == 40            # untouched
    assert not Redemption.objects.exists()


@pytest.mark.django_db
def test_per_customer_limit(customer, reward):
    reward.per_customer_limit = 1
    reward.save()
    LoyaltyService.record(customer.id, LoyaltyTransaction.Kind.GRANT, 500)
    r1, s1 = LoyaltyService.redeem(customer, reward.id)
    assert s1 == 201
    r2, s2 = LoyaltyService.redeem(customer, reward.id)
    assert not r2['success'] and 'limit' in r2['message'].lower()


@pytest.mark.django_db
def test_staff_scan_awards_by_amount(customer):
    cfg = BotConfig.load()
    cfg.loyalty_earn_per = Decimal('1000')          # 1 point per 1000 UZS
    cfg.save()
    result, status = LoyaltyService.award_scan('SF-55501', 25000)
    assert result['success'], result
    assert result['data']['awarded'] == 25          # 25000 / 1000
    customer.refresh_from_db()
    assert customer.loyalty_points == 25


@pytest.mark.django_db
def test_member_lookup_and_grant(customer):
    res, st = LoyaltyService.member('SF-55501')
    assert res['success'] and res['data']['member']['telegram_id'] == 55501
    LoyaltyService.grant('SF-55501', 60, reason='bonus')
    customer.refresh_from_db()
    assert customer.loyalty_points == 60


@pytest.mark.django_db
def test_fulfill_redemption(customer, reward):
    LoyaltyService.record(customer.id, LoyaltyTransaction.Kind.GRANT, 200)
    result, _ = LoyaltyService.redeem(customer, reward.id)
    code = result['data']['redemption']['code']
    res, st = LoyaltyService.fulfill(code)
    assert res['success'] and res['data']['redemption']['status'] == 'FULFILLED'
    # double fulfilment is rejected
    res2, _ = LoyaltyService.fulfill(code)
    assert not res2['success']
