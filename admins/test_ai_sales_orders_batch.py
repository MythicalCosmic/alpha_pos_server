"""AI chat create endpoint + empty-chat hiding (bug fix), sales dashboard (item 12),
orders ?include_items (item 14)."""
import secrets
from datetime import datetime, timedelta
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


def _aware(y, m, d, hh=12, mm=0):
    return timezone.make_aware(datetime(y, m, d, hh, mm))


def _order_at(created, total='100', otype='HALL', paid=True, status='COMPLETED', method='CASH'):
    from base.models import User, Order
    u = User.objects.create(email=f's{secrets.token_hex(4)}@x.local', first_name='a',
                            last_name='b', role='CASHIER', status='ACTIVE', password='!')
    o = Order.objects.create(user=u, cashier=u, status=status, is_paid=paid,
                             order_type=otype, display_id=Order.objects.count() + 1,
                             subtotal=Decimal(total), total_amount=Decimal(total),
                             payment_method=(method if paid else None),
                             paid_at=(created if paid else None))
    Order.objects.filter(pk=o.pk).update(created_at=created)
    return o


# ── AI chat: create endpoint + empty-chat hiding (the reported bug) ──

class TestAIChatCreate:
    def test_create_then_hidden_until_message(self, admin_user):
        from stock.services.ai_chat_service import AIChatService
        from stock.models import AIMessage
        chat = AIChatService.create_chat(admin_user.id)
        assert chat['id'] and chat['messages'] == []
        # an empty chat must NOT clutter the sidebar
        assert AIChatService.list_chats(admin_user.id) == []
        AIMessage.objects.create(chat_id=chat['id'], role=AIMessage.Role.USER, content='hi')
        listed = AIChatService.list_chats(admin_user.id)
        assert len(listed) == 1 and listed[0]['id'] == chat['id']

    def test_get_chat_returns_messages_with_timestamps(self, admin_user):
        from stock.services.ai_chat_service import AIChatService
        from stock.models import AIMessage
        chat = AIChatService.create_chat(admin_user.id)
        AIMessage.objects.create(chat_id=chat['id'], role=AIMessage.Role.USER, content='q')
        AIMessage.objects.create(chat_id=chat['id'], role=AIMessage.Role.ASSISTANT, content='a')
        got = AIChatService.get_chat(admin_user.id, chat['id'])
        assert [m['role'] for m in got['messages']] == ['user', 'assistant']
        assert got['messages'][0]['created_at']  # item 15: ISO timestamp present

    def test_post_create_endpoint_returns_id(self, admin_session):
        resp = Client().post('/api/admins/stock/ai/chats/', data='{}',
                             content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {admin_session}')
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert body['success'] and body['id']


# ── Sales dashboard (item 12) ──

class TestSalesDashboard:
    def test_shape_and_lengths(self):
        from admins.services.sales_dashboard_service import sales_dashboard, HM_DAYS, HM_HOURS
        data = sales_dashboard(range_token='7d')
        for k in ('monthRevenue', 'grossMargin', 'revenue30', 'expense30', 'lastMonthRev',
                  'dayLabels', 'HM_DAYS', 'HM_HOURS', 'heatMatrix', 'channelDays'):
            assert k in data, f'missing {k}'
        n = len(data['dayLabels'])
        assert n == 7
        assert len(data['revenue30']) == n == len(data['expense30']) == len(data['channelDays'])
        assert len(data['lastMonthRev']) == n
        assert data['HM_DAYS'] == HM_DAYS and data['HM_HOURS'] == HM_HOURS
        assert len(data['heatMatrix']) == 7
        assert all(len(r) == len(HM_HOURS) for r in data['heatMatrix'])

    def test_business_day_bucketing_and_channel(self):
        from admins.services.sales_dashboard_service import sales_dashboard
        # 01:00 on the 11th -> business day the 10th (cutover 03:00)
        _order_at(_aware(2026, 3, 11, 1, 0), total='500', otype='DELIVERY')
        data = sales_dashboard(date_from='2026-03-10', date_to='2026-03-11')
        i10 = data['dayLabels'].index('2026-03-10')
        assert data['revenue30'][i10] == '500'
        assert data['channelDays'][i10]['delivery'] == 1


# ── Orders ?include_items (item 14) ──

class TestIncludeItems:
    def test_include_items_toggle(self):
        from admins.services.order_service import AdminOrderService
        from base.models import OrderItem, Product, Category
        o = _order_at(_aware(2026, 3, 10, 12, 0))
        cat, _ = Category.objects.get_or_create(name='c', slug='c')
        p = Product.objects.create(name='A', price=Decimal('10'), category=cat)
        OrderItem.objects.create(order=o, product=p, quantity=2,
                                 price=Decimal('10'), original_price=Decimal('10'))
        res_with, _ = AdminOrderService.get_all_orders(include_items=True)
        res_wo, _ = AdminOrderService.get_all_orders(include_items=False)
        row_with = res_with['data']['orders'][0]
        row_wo = res_wo['data']['orders'][0]
        # items_count is the number of LINE items (1), not the quantity (2).
        assert 'items' in row_with and row_with['items_count'] == 1
        assert 'items' not in row_wo and row_wo['items_count'] == 1  # count stays
