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

    def test_list_query_count_does_not_grow_per_order(self, order_factory):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from admins.services.order_service import AdminOrderService

        order_factory()
        with CaptureQueriesContext(connection) as one_order_queries:
            result, status = AdminOrderService.get_all_orders(per_page=100)
        assert status == 200, result

        for _ in range(5):
            order_factory()
        with CaptureQueriesContext(connection) as many_order_queries:
            result, status = AdminOrderService.get_all_orders(per_page=100)
        assert status == 200, result

        assert len(many_order_queries) == len(one_order_queries)


# ── Product affinity / market-basket (item 16) ──

def _product(name, price='10000'):
    from base.models import Category, Product
    cat, _ = Category.objects.get_or_create(name='c', slug='c')
    return Product.objects.create(name=name, price=Decimal(price), category=cat)


def _order_with(products, paid=True, status='COMPLETED'):
    from base.models import OrderItem
    o = _order_at(_aware(2026, 3, 10, 12, 0), paid=paid, status=status)
    for p in products:
        OrderItem.objects.create(order=o, product=p, quantity=1,
                                 price=p.price, original_price=p.price)
    return o


class TestProductAffinity:
    def test_cooccurrence_counts_and_exclusions(self):
        from datetime import date
        from admins.services.product_analytics_service import products_affinity
        A, B, C = _product('A'), _product('B'), _product('C')
        _order_with([A, B, C])                            # paid
        _order_with([A, B])                               # paid
        _order_with([A, C])                               # paid
        _order_with([A, B], status='CANCELED')            # excluded (cancelled)
        _order_with([A, B], paid=False, status='OPEN')    # excluded (unpaid)

        data = products_affinity(date(2026, 3, 10), date(2026, 3, 10), limit=10)
        assert data['totalOrders'] == 3                   # only the 3 paid, non-cancelled
        by_id = {p['id']: p for p in data['products']}
        assert by_id[A.id]['orders'] == 3
        assert by_id[B.id]['orders'] == 2 and by_id[C.id]['orders'] == 2
        assert data['products'][0]['id'] == A.id          # most orders first
        # every pair: a<b (by index), count>0
        assert all(p['a'] < p['b'] and p['count'] > 0 for p in data['pairs'])
        idx_to_id = [p['id'] for p in data['products']]
        counts = {tuple(sorted((idx_to_id[p['a']], idx_to_id[p['b']]))): p['count']
                  for p in data['pairs']}
        assert counts[tuple(sorted((A.id, B.id)))] == 2
        assert counts[tuple(sorted((A.id, C.id)))] == 2
        assert counts[tuple(sorted((B.id, C.id)))] == 1
        assert len(data['pairs']) == 3

    def test_topn_limit_drops_outside_pairs(self):
        from datetime import date
        from admins.services.product_analytics_service import products_affinity
        A, B, C = _product('A'), _product('B'), _product('C')
        _order_with([A, B, C]); _order_with([A, B]); _order_with([A, C])
        data = products_affinity(date(2026, 3, 10), date(2026, 3, 10), limit=2)
        assert len(data['products']) == 2
        ids = {p['id'] for p in data['products']}
        assert A.id in ids                                # A always top (orders=3)
        idx_to_id = [p['id'] for p in data['products']]
        for p in data['pairs']:                           # only intra-top-N pairs survive
            assert idx_to_id[p['a']] in ids and idx_to_id[p['b']] in ids
        assert len(data['pairs']) <= 1


# ── Order number (item 4) ──

class TestOrderNumber:
    def test_allocator_monotonic_no_wrap_and_per_branch(self):
        from base.repositories.order import OrderRepository
        vals = [OrderRepository.next_order_number() for _ in range(105)]
        assert vals == list(range(1, 106))                # never wraps at 100 (unlike display_id)
        assert OrderRepository.next_order_number(scope='branch-x') == 1  # independent per branch

    def test_order_number_in_admin_serializer(self):
        from base.models import Order
        from admins.services.order_service import AdminOrderService
        o = _order_at(_aware(2026, 3, 10, 12, 0))
        Order.objects.filter(pk=o.pk).update(order_number=7)
        res, _ = AdminOrderService.get_all_orders(include_items=False)
        assert res['data']['orders'][0]['order_number'] == 7


# ── Operations dashboard (item 17) ──

class TestOperationsDashboard:
    def test_shape(self):
        from admins.services.operations_dashboard_service import operations_dashboard
        data = operations_dashboard()
        assert set(data) >= {'tableGrid', 'funnel', 'prepByCategory', 'ordersByHour'}
        assert len(data['ordersByHour']) == 14
        assert data['ordersByHour'][0]['hour'] == '09' and data['ordersByHour'][-1]['hour'] == '22'
        assert [f['status'] for f in data['funnel']] == \
            ['OPEN', 'PREPARING', 'READY', 'COMPLETED', 'CANCELED']

    def test_table_grid_and_funnel_live(self):
        from base.models import Order, Place, Table, User
        from admins.services.operations_dashboard_service import operations_dashboard
        u = User.objects.create(email=f'op{secrets.token_hex(4)}@x.local', first_name='a',
                                last_name='b', role='CASHIER', status='ACTIVE', password='!')
        place = Place.objects.create(name='Hall')
        t = Table.objects.create(place=place, number='5')
        Order.objects.create(user=u, cashier=u, table=t, status='READY', is_paid=False,
                             display_id=1, subtotal=Decimal('0'), total_amount=Decimal('0'))
        data = operations_dashboard()                       # created_at = now -> today's window
        grid = {g['id']: g for g in data['tableGrid']}
        assert grid[t.id]['status'] == 'ready' and grid[t.id]['orders'] == 1
        assert {f['status']: f['count'] for f in data['funnel']}['READY'] >= 1


# ── AI page-context preamble (item: page-context injection) ──

class TestContextPreamble:
    def test_builds_current_view(self):
        from stock.services.ai_assistant_service import AIStockAssistant
        p = AIStockAssistant._context_preamble({
            'route': '/dashboard', 'range_from': '2026-06-14', 'range_to': '2026-06-28',
            'filters': {'category': 'Pizza'}, 'visible_data_keys': ['monthRevenue', 'heatMatrix']})
        assert p.startswith('CURRENT VIEW:')
        assert '/dashboard' in p and '2026-06-14' in p and 'Pizza' in p and 'monthRevenue' in p
        assert p.endswith('\n\n')

    def test_empty_context_returns_blank(self):
        from stock.services.ai_assistant_service import AIStockAssistant
        assert AIStockAssistant._context_preamble(None) == ''
        assert AIStockAssistant._context_preamble({}) == ''
        assert AIStockAssistant._context_preamble({'unknown': 'x'}) == ''
