"""1C export tests — service + admin endpoint."""
import secrets
from datetime import date, timedelta
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from django.test import Client
from django.utils import timezone

from base.repositories.session import SessionRepository
from admins.services.export_service import build_export, parse_date_range


pytestmark = pytest.mark.django_db


def _make_order(user, total='100000', status='COMPLETED', is_paid=True,
                payment_method='CASH', phone='998901111111',
                created_at=None, display_id=None):
    from base.models import Order
    o = Order.objects.create(
        user=user, phone_number=phone, order_type='PICKUP',
        status=status, is_paid=is_paid, payment_method=payment_method,
        total_amount=Decimal(total), subtotal=Decimal(total),
        display_id=display_id or (Order.objects.count() + 1),
        paid_at=((created_at or timezone.now()) if is_paid else None),
    )
    if created_at:
        from base.models import Order as O
        O.objects.filter(pk=o.pk).update(created_at=created_at)
        o.refresh_from_db()
    return o


def _add_item(order, name, price, qty):
    from base.models import Category, OrderItem, Product
    cat, _ = Category.objects.get_or_create(name='c', slug=name.lower())
    p = Product.objects.create(name=name, price=Decimal(price), category=cat)
    OrderItem.objects.create(
        order=order, product=p, quantity=qty,
        price=Decimal(price), original_price=Decimal(price),
    )


# ---- service unit tests --------------------------------------------------

class TestParseDateRange:
    def test_happy_path(self):
        df, dt, err = parse_date_range('2026-05-01', '2026-05-17')
        assert err is None
        assert df == date(2026, 5, 1)
        assert dt == date(2026, 5, 17)

    def test_missing_from_returns_error(self):
        _, _, err = parse_date_range(None, '2026-05-17')
        assert err is not None

    def test_invalid_format_returns_error(self):
        _, _, err = parse_date_range('bad', '2026-05-17')
        assert err is not None

    def test_inverted_range_rejected(self):
        _, _, err = parse_date_range('2026-05-17', '2026-05-01')
        assert err is not None


class TestBuildExport:
    def test_includes_paid_completed_orders(self, regular_user):
        order = _make_order(regular_user)
        _add_item(order, 'Margherita', '50000', 2)
        xml, count = build_export(date(2026, 5, 1), timezone.localdate())
        assert count == 1
        root = ET.fromstring(xml)
        docs = root.findall('Документ')
        assert len(docs) == 1
        товары = docs[0].find('Товары').findall('Товар')
        assert len(товары) == 1
        assert товары[0].find('Наименование').text == 'Margherita'
        assert товары[0].find('Количество').text == '2'

    def test_excludes_unpaid_by_default(self, regular_user):
        _make_order(regular_user, is_paid=False)
        _, count = build_export(date(2026, 5, 1), timezone.localdate())
        assert count == 0

    def test_includes_unpaid_when_flagged(self, regular_user):
        _make_order(regular_user, is_paid=False)
        _, count = build_export(date(2026, 5, 1), timezone.localdate(),
                                include_unpaid=True)
        assert count == 1

    def test_excludes_cancelled(self, regular_user):
        _make_order(regular_user, status='CANCELED')
        _, count = build_export(date(2026, 5, 1), timezone.localdate(),
                                include_unpaid=True)
        assert count == 0

    def test_excludes_outside_window(self, regular_user):
        from datetime import datetime
        from django.utils import timezone as tz
        # Pin the order BEFORE the fixed window start. Using `now() - 60 days` was a
        # time bomb: once real time passed 2026-06-30 the order fell INSIDE the
        # [2026-05-01, today] window and the test began failing on its own.
        before = tz.make_aware(datetime(2026, 4, 1, 12, 0), tz.get_current_timezone())
        _make_order(regular_user, created_at=before)
        _, count = build_export(date(2026, 5, 1), timezone.localdate())
        assert count == 0

    def test_includes_payment_method_label(self, regular_user):
        order = _make_order(regular_user, payment_method='UZCARD')
        _add_item(order, 'Salad', '30000', 1)
        xml, _ = build_export(date(2026, 5, 1), timezone.localdate())
        root = ET.fromstring(xml)
        assert root.find('Документ').find('ФормаОплаты').text == 'Uzcard'

    def test_includes_phone(self, regular_user):
        order = _make_order(regular_user, phone='998900000001')
        _add_item(order, 'X', '1', 1)
        xml, _ = build_export(date(2026, 5, 1), timezone.localdate())
        root = ET.fromstring(xml)
        assert root.find('Документ').find('ТелефонКонтакта').text == '998900000001'

    def test_root_element_has_commerceml_version(self, regular_user):
        order = _make_order(regular_user)
        _add_item(order, 'X', '1', 1)
        xml, _ = build_export(date(2026, 5, 1), timezone.localdate())
        root = ET.fromstring(xml)
        assert root.tag == 'КоммерческаяИнформация'
        assert root.attrib['ВерсияСхемы'] == '2.05'


# ---- admin endpoint -----------------------------------------------------

@pytest.fixture
def admin_session(admin_user):
    from base.models import Session
    payload = secrets.token_hex(32)
    Session.objects.create(
        user_id=admin_user, ip_address='127.0.0.1', payload=SessionRepository.hash_token(payload),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return payload


def _auth(s):
    return {'HTTP_AUTHORIZATION': f'Bearer {s}'}


class TestExportEndpoint:
    def test_returns_xml_with_attachment_header(self, admin_session, regular_user):
        order = _make_order(regular_user)
        _add_item(order, 'X', '1', 1)
        client = Client()
        resp = client.get(
            f'/api/admins/exports/1c?from=2026-05-01&to={timezone.localdate().isoformat()}',
            **_auth(admin_session),
        )
        assert resp.status_code == 200
        assert resp['Content-Type'].startswith('application/xml')
        assert 'attachment' in resp['Content-Disposition']
        assert resp['X-Export-Count'] == '1'

    def test_missing_dates_returns_422(self, admin_session):
        client = Client()
        resp = client.get('/api/admins/exports/1c', **_auth(admin_session))
        assert resp.status_code == 422

    def test_requires_admin(self, cashier_user):
        from base.models import Session
        payload = secrets.token_hex(32)
        Session.objects.create(
            user_id=cashier_user, ip_address='127.0.0.1', payload=SessionRepository.hash_token(payload),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = Client()
        resp = client.get(
            '/api/admins/exports/1c?from=2026-05-01&to=2026-05-30',
            HTTP_AUTHORIZATION=f'Bearer {payload}',
        )
        assert resp.status_code == 403
