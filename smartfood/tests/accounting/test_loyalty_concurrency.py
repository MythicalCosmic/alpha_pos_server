from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections
from django.test import Client

from smartfood.models import Customer, LoyaltyTransaction, Redemption, Reward
from smartfood.services.loyalty_service import LoyaltyService

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.skipif(
    connection.vendor != 'postgresql', reason='Requires PostgreSQL concurrent transactions')]


def test_concurrent_same_key_returns_one_reward_and_exact_response(auth_client, customer):
    LoyaltyService.record(customer.pk, 'GRANT', 500, reason='Concurrency setup')
    reward = Reward.objects.create(name_en='Test gift', points_cost=100, stock=2)
    barrier = Barrier(2)
    defaults = dict(auth_client.defaults)
    cookies = auth_client.cookies.copy()

    def run():
        close_old_connections()
        client = Client(**defaults)
        client.cookies = cookies.copy()
        try:
            barrier.wait(timeout=10)
            response = client.post(f'/api/smartfood/rewards/{reward.pk}/redeem',
                data='{}', content_type='application/json', HTTP_IDEMPOTENCY_KEY='parallel-gift')
            return response.status_code, response.content
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert results[0] == results[1] and results[0][0] == 201, results
    assert Redemption.objects.count() == 1
    assert LoyaltyTransaction.objects.filter(kind='REDEEM').count() == 1
    customer.refresh_from_db(); reward.refresh_from_db()
    assert customer.loyalty_points == 400 and reward.stock == 1
