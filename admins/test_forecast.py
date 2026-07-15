"""Demand forecast aggregation, deterministic model, and endpoint tests."""
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.repositories.session import SessionRepository
from admins.services import forecast_service


pytestmark = pytest.mark.django_db


def _add_history(user, product, qty, hours_ago):
    from base.models import Order, OrderItem
    paid_at = timezone.now() - timedelta(hours=hours_ago)
    o = Order.objects.create(
        user=user, phone_number='998900000001', order_type='PICKUP',
        status='COMPLETED', is_paid=True, payment_method='CASH',
        paid_at=paid_at,
        total_amount=Decimal('100'), subtotal=Decimal('100'),
        display_id=Order.objects.count() + 1,
    )
    OrderItem.objects.create(
        order=o, product=product, quantity=qty,
        price=Decimal('100'), original_price=Decimal('100'),
    )
    from base.models import Order as O
    O.objects.filter(pk=o.pk).update(
        created_at=paid_at,
    )
    return o


def _make_product(name='Pizza', slug='pizza'):
    from base.models import Category, Product
    cat, _ = Category.objects.get_or_create(name='c', slug=slug)
    return Product.objects.create(
        name=name, price=Decimal('100'), category=cat,
    )


class TestGatherHistory:
    def test_empty_history_returns_empty_products(self):
        data = forecast_service.gather_history()
        assert data['products'] == []

    def test_aggregates_quantity_per_product(self, regular_user):
        p = _make_product()
        _add_history(regular_user, p, 3, hours_ago=2)
        _add_history(regular_user, p, 2, hours_ago=26)
        data = forecast_service.gather_history()
        prod = data['products'][0]
        assert prod['name'] == 'Pizza'
        assert prod['total_qty'] == 5

    def test_by_weekday_and_hour_breakdown_present(self, regular_user):
        p = _make_product()
        _add_history(regular_user, p, 1, hours_ago=3)
        data = forecast_service.gather_history()
        prod = data['products'][0]
        assert sum(prod['by_weekday'].values()) == 1
        assert sum(prod['by_hour'].values()) == 1

    def test_top_n_limits_products(self, regular_user):
        from base.models import Category, Product
        cat, _ = Category.objects.get_or_create(name='c', slug='cat')
        for i in range(20):
            p = Product.objects.create(
                name=f'P{i}', price=Decimal('100'), category=cat,
            )
            _add_history(regular_user, p, i + 1, hours_ago=1)
        data = forecast_service.gather_history(top_n=5)
        assert len(data['products']) == 5
        # Sorted by total_qty desc — the top one has the highest qty.
        assert data['products'][0]['total_qty'] >= data['products'][-1]['total_qty']


class TestForecastTomorrow:
    def test_no_history_returns_early(self):
        data, err = forecast_service.forecast_tomorrow()
        assert err is None
        assert data['predictions'] == []
        assert data['reason'] == 'no_history'
        assert data['method'] == 'historical_weekday_blend'

    def test_returns_local_prediction(self, regular_user):
        p = _make_product()
        _add_history(regular_user, p, 5, hours_ago=2)
        data, err = forecast_service.forecast_tomorrow()
        assert err is None
        assert data['method'] == 'historical_weekday_blend'
        assert data['predictions'][0]['product_id'] == p.id
        assert data['predictions'][0]['suggested_qty'] >= 1

    def test_tomorrow_weekday_history_is_weighted(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        weekday = tomorrow.strftime('%a')
        history = {
            'window_days': 28,
            'products': [{
                'id': 1,
                'name': 'Burger',
                'total_qty': 28,
                'by_weekday': {weekday: 20},
            }],
        }
        prediction = forecast_service._local_predictions(history, tomorrow)[0]
        # Four matching weekdays average 5; blended with daily average 1.
        assert prediction['suggested_qty'] == 5
        assert weekday in prediction['reason']

    def test_old_history_payload_without_total_is_tolerated(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        history = {
            'window_days': 30,
            'products': [{
                'id': 1,
                'name': 'Burger',
                'by_weekday': {'Mon': 3, 'Tue': 2},
            }],
        }
        prediction = forecast_service._local_predictions(history, tomorrow)[0]
        assert prediction['suggested_qty'] >= 1


@pytest.fixture
def admin_session(admin_user):
    from base.models import Session
    payload = secrets.token_hex(32)
    Session.objects.create(
        user_id=admin_user, ip_address='127.0.0.1', payload=SessionRepository.hash_token(payload),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return payload


class TestForecastEndpoint:
    def test_admin_can_fetch_with_no_history(self, admin_session):
        client = Client()
        resp = client.get(
            '/api/admins/forecast/tomorrow',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 200
        data = resp.json()['data']
        assert data['predictions'] == []

    def test_forecast_does_not_require_llm_configuration(
        self, admin_session, regular_user,
    ):
        p = _make_product()
        _add_history(regular_user, p, 1, hours_ago=1)
        client = Client()
        resp = client.get(
            '/api/admins/forecast/tomorrow',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 200
        assert resp.json()['data']['method'] == 'historical_weekday_blend'

    def test_requires_admin(self):
        client = Client()
        resp = client.get('/api/admins/forecast/tomorrow')
        assert resp.status_code == 401
