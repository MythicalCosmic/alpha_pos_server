"""Demand forecast service + endpoint tests.

Gemini is monkeypatched so the suite never makes real API calls. We
exercise the history aggregation, the parse path (including ``` fences),
and the endpoint's error mapping.
"""
import json
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
    o = Order.objects.create(
        user=user, phone_number='998900000001', order_type='PICKUP',
        status='COMPLETED', is_paid=True, payment_method='CASH',
        total_amount=Decimal('100'), subtotal=Decimal('100'),
        display_id=Order.objects.count() + 1,
    )
    OrderItem.objects.create(
        order=o, product=product, quantity=qty,
        price=Decimal('100'), original_price=Decimal('100'),
    )
    from base.models import Order as O
    O.objects.filter(pk=o.pk).update(
        created_at=timezone.now() - timedelta(hours=hours_ago),
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

    def test_calls_llm_and_parses_response(
        self, monkeypatch, regular_user,
    ):
        p = _make_product()
        _add_history(regular_user, p, 5, hours_ago=2)

        fake_response = json.dumps({
            'tomorrow': '2026-05-18',
            'predictions': [
                {'product_id': p.id, 'product_name': p.name,
                 'suggested_qty': 8, 'reason': 'Friday demand'},
            ],
        })
        monkeypatch.setattr(
            forecast_service, '_call_llm',
            lambda prompt: (fake_response, None),
        )
        data, err = forecast_service.forecast_tomorrow()
        assert err is None
        assert data['predictions'][0]['suggested_qty'] == 8

    def test_strips_markdown_code_fence(self, monkeypatch, regular_user):
        p = _make_product()
        _add_history(regular_user, p, 1, hours_ago=1)
        fake = '```json\n{"predictions": [{"product_id": 1, "suggested_qty": 3, "product_name": "X", "reason": "y"}]}\n```'
        monkeypatch.setattr(
            forecast_service, '_call_llm',
            lambda prompt: (fake, None),
        )
        data, err = forecast_service.forecast_tomorrow()
        assert err is None
        assert data['predictions'][0]['suggested_qty'] == 3

    def test_non_json_response_returns_parse_error(
        self, monkeypatch, regular_user,
    ):
        p = _make_product()
        _add_history(regular_user, p, 1, hours_ago=1)
        monkeypatch.setattr(
            forecast_service, '_call_llm',
            lambda prompt: ('not json at all', None),
        )
        data, err = forecast_service.forecast_tomorrow()
        assert err == 'parse_error'

    def test_llm_error_propagated(self, monkeypatch, regular_user):
        p = _make_product()
        _add_history(regular_user, p, 1, hours_ago=1)
        monkeypatch.setattr(
            forecast_service, '_call_llm',
            lambda prompt: (None, 'llm_key_missing'),
        )
        data, err = forecast_service.forecast_tomorrow()
        assert err == 'llm_key_missing'


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

    def test_llm_key_missing_returns_503(
        self, monkeypatch, admin_session, regular_user,
    ):
        p = _make_product()
        _add_history(regular_user, p, 1, hours_ago=1)
        monkeypatch.setattr(
            forecast_service, '_call_llm',
            lambda prompt: (None, 'llm_key_missing'),
        )
        client = Client()
        resp = client.get(
            '/api/admins/forecast/tomorrow',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 503

    def test_parse_error_returns_502(
        self, monkeypatch, admin_session, regular_user,
    ):
        p = _make_product()
        _add_history(regular_user, p, 1, hours_ago=1)
        monkeypatch.setattr(
            forecast_service, '_call_llm',
            lambda prompt: ('garbage', None),
        )
        client = Client()
        resp = client.get(
            '/api/admins/forecast/tomorrow',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 502

    def test_requires_admin(self):
        client = Client()
        resp = client.get('/api/admins/forecast/tomorrow')
        assert resp.status_code == 401
