"""AI Morning Briefing + Anomaly Watch (msg 37/38)."""
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.repositories.session import SessionRepository

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_session(admin_user):
    from base.models import Session
    payload = secrets.token_hex(32)
    Session.objects.create(
        user_id=admin_user, ip_address='127.0.0.1',
        payload=SessionRepository.hash_token(payload),
        expires_at=timezone.now() + timedelta(hours=1))
    return payload


def _cashier():
    from base.models import User
    return User.objects.create(email=f'c{secrets.token_hex(4)}@x.local', first_name='Ca',
                               last_name='Sh', role='CASHIER', status='ACTIVE', password='!')


def _order(cashier, status='COMPLETED', total='100', subtotal='100', dpct='0', damt='0'):
    from base.models import Order
    return Order.objects.create(
        user=cashier, cashier=cashier, status=status, is_paid=(status != 'CANCELED'),
        display_id=1, subtotal=Decimal(subtotal), total_amount=Decimal(total),
        discount_percent=Decimal(dpct), discount_amount=Decimal(damt),
        payment_method='CASH', paid_at=timezone.now())


# ── Morning Briefing ──

class TestBriefing:
    def test_caches_and_dismiss(self, admin_user):
        from stock.services.ai_briefing_service import AIBriefingService
        from stock.models import AIBriefing
        d1 = AIBriefingService.get_or_generate(admin_user.id)
        assert set(d1) >= {'id', 'generated_at', 'valid_until', 'dismissed', 'bullets'}
        assert d1['dismissed'] is False
        d2 = AIBriefingService.get_or_generate(admin_user.id)
        assert d2['id'] == d1['id']
        assert AIBriefing.objects.filter(user_id=admin_user.id).count() == 1  # one row per day
        AIBriefingService.dismiss(admin_user.id)
        assert AIBriefingService.get_or_generate(admin_user.id)['dismissed'] is True

    def test_fallback_bullets_shape(self):
        from stock.services.ai_briefing_service import AIBriefingService
        snap = {
            'sales': {'today': {'total_revenue_uzs': 500000, 'count': 12},
                      'top_products_today': [{'name': 'Burger', 'revenue_uzs': 120000}]},
            'inventory_health': {'summary': {'dead_stock_count': 2},
                                 'dead_stock': [{'name': 'Old Milk', 'days_since_last_movement': 40}]},
            'menu_engineering': {'summary': {'dogs': 3}},
        }
        bullets = AIBriefingService._fallback(snap)
        assert 1 <= len(bullets) <= 5
        assert all(set(b) >= {'icon', 'title', 'body', 'deep_link', 'ai_seed_prompt'} for b in bullets)

    def test_briefing_endpoint(self, admin_session):
        resp = Client().get('/api/admins/ai/briefing',
                            HTTP_AUTHORIZATION=f'Bearer {admin_session}')
        assert resp.status_code == 200
        assert 'bullets' in resp.json()['data']

    def test_fallback_bullets_are_trilingual(self):
        from stock.services.ai_briefing_service import AIBriefingService
        bullets = AIBriefingService._fallback(
            {'sales': {'today': {'total_revenue_uzs': 500000, 'count': 12}}})
        assert bullets
        b = bullets[0]
        assert set(b['title_i18n']) == {'uz', 'ru', 'en'}
        assert set(b['body_i18n']) == {'uz', 'ru', 'en'}
        assert b['title'] == b['title_i18n']['en']        # flat == English fallback
        assert all(b['title_i18n'][k] for k in ('uz', 'ru', 'en'))


# ── Anomaly Watch ──

class TestAnomaly:
    def test_detects_void_burst_and_unusual_discount(self):
        from stock.services.anomaly_service import AnomalyScanner
        from stock.models import Anomaly
        c = _cashier()
        for _ in range(5):                          # void burst: 5 cancels today
            _order(c, status='CANCELED', total='0')
        _order(_cashier(), dpct='50')               # unusual discount: 50%
        created = AnomalyScanner.scan()
        dets = {a.detector for a in created}
        assert 'CashierVoidBurst' in dets
        assert 'UnusualDiscount' in dets
        # idempotent: a second scan creates nothing new
        n = Anomaly.objects.count()
        AnomalyScanner.scan()
        assert Anomaly.objects.count() == n

    def test_low_stock_detector(self):
        from stock.models import StockItem, StockUnit
        from stock.services.anomaly_service import LowStockCrossed
        unit = StockUnit.objects.create(name='kg', short_name='kg')
        StockItem.objects.create(name='Flour', sku=f'F{secrets.token_hex(3)}',
                                 base_unit=unit, reorder_point=Decimal('10'),
                                 is_active=True)  # no stock levels -> total None -> below reorder
        cands = LowStockCrossed().scan(timezone.now())
        assert any(c['detector'] == 'LowStockCrossed' for c in cands)

    def test_ack_and_settings(self, admin_user):
        from stock.models import Anomaly
        from stock.services.anomaly_service import AnomalyService
        a = Anomaly.objects.create(detector='X', idempotency_key='k1', message='m')
        assert AnomalyService.ack(a.id, admin_user.id) is True
        a.refresh_from_db()
        assert a.acked_at is not None and a.acked_by == admin_user.id
        s = AnomalyService.update_settings(
            admin_user.id, muted_detectors=['RevenueDip'], quiet_start='22:00', quiet_end='08:00')
        assert s['muted_detectors'] == ['RevenueDip']
        assert s['quiet_start'] == '22:00' and s['quiet_end'] == '08:00'

    def test_anomalies_endpoint(self, admin_session):
        resp = Client().get('/api/admins/ai/anomalies?unacked=1',
                            HTTP_AUTHORIZATION=f'Bearer {admin_session}')
        assert resp.status_code == 200
        assert 'anomalies' in resp.json()['data']

    def test_anomaly_message_is_trilingual(self):
        from stock.services.anomaly_service import AnomalyScanner, AnomalyService
        from stock.models import Anomaly
        c = _cashier()
        for _ in range(5):
            _order(c, status='CANCELED', total='0')
        AnomalyScanner.scan()
        a = Anomaly.objects.filter(detector='CashierVoidBurst').first()
        assert a and set(a.message_i18n) == {'uz', 'ru', 'en'} and a.message == a.message_i18n['en']
        data = AnomalyService.list_anomalies()
        m = data['anomalies'][0]['message']
        assert isinstance(m, dict) and set(m) == {'uz', 'ru', 'en'} and m['en']
        # old-style row (no i18n) still serializes to a 3-key dict via fallback
        Anomaly.objects.create(detector='X', idempotency_key='k-old', message='hi')
        got = next(x for x in AnomalyService.list_anomalies()['anomalies'] if x['detector'] == 'X')
        assert got['message'] == {'uz': 'hi', 'ru': 'hi', 'en': 'hi'}
