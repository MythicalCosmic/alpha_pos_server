"""Heavy tests for the Smart Food customer-delivery backend.

Risk surfaces: Telegram initData HMAC auth, dynamic bot on/off + no-cashier
gating, server-side money recompute (sizes + toppings), runtime stop-selling,
the cashier-dispatch flow (attribution + price integrity), tracking, and IDOR.
"""
import json
from decimal import Decimal

import pytest

from smartfood.tests.conftest import make_init_data

pytestmark = pytest.mark.django_db

C = '/api/smartfood'
A = '/api/admins/smartfood'


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type='application/json')


def _patch(client, path, payload):
    return client.patch(path, data=json.dumps(payload), content_type='application/json')


# --------------------------------------------------------------------------- #
#  Auth (Telegram initData HMAC)                                               #
# --------------------------------------------------------------------------- #
class TestAuth:
    def test_valid_initdata_logs_in_and_creates_customer(self, client, bot_token):
        init = make_init_data(bot_token, {'id': 555001, 'first_name': 'Lola', 'language_code': 'ru'})
        r = _post(client, f'{C}/auth', {'init_data': init})
        assert r.status_code == 200, r.content
        body = r.json()
        assert body['success'] and body['data']['token']
        from smartfood.models import Customer, CustomerSession
        c = Customer.objects.get(telegram_id=555001)
        assert c.language == 'ru'
        # token is stored only as a hash, never raw
        assert not CustomerSession.objects.filter(payload=body['data']['token']).exists()
        assert CustomerSession.objects.filter(customer=c).count() == 1

    def test_tampered_hash_rejected(self, client, bot_token):
        init = make_init_data(bot_token, {'id': 1, 'first_name': 'X'})
        init = init[:-4] + 'dead'   # corrupt the hash
        r = _post(client, f'{C}/auth', {'init_data': init})
        assert r.status_code == 401

    def test_wrong_bot_token_rejected(self, client, bot_token):
        init = make_init_data('999999:OTHER-TOKEN', {'id': 1, 'first_name': 'X'})
        r = _post(client, f'{C}/auth', {'init_data': init})
        assert r.status_code == 401

    def test_protected_endpoint_needs_token(self, client, cfg):
        assert client.get(f'{C}/me').status_code == 401

    def test_me_with_token(self, auth_client, customer):
        r = auth_client.get(f'{C}/me')
        assert r.status_code == 200
        assert r.json()['data']['telegram_id'] == customer.telegram_id


# --------------------------------------------------------------------------- #
#  Gating (dynamic on/off + no active cashier)                                 #
# --------------------------------------------------------------------------- #
class TestGating:
    def test_catalog_closed_when_bot_off(self, auth_client, db, product):
        from smartfood.models import BotConfig
        cfg = BotConfig.load(); cfg.enabled = False; cfg.save()
        r = auth_client.get(f'{C}/catalog/products')
        assert r.status_code == 200 and r.json().get('closed') is True
        assert r.json().get('reason') == 'bot_off'

    def test_order_blocked_when_no_cashier(self, auth_client, cfg, product, address):
        # bot ON (cfg) but NO active shift exists
        items = [{'product_id': product.id, 'quantity': 1}]
        r = _post(auth_client, f'{C}/orders',
                  {'items': items, 'order_type': 'DELIVERY', 'address_id': address.id})
        assert r.status_code == 200 and r.json().get('reason') == 'no_cashier'

    def test_order_ok_with_active_cashier(self, auth_client, cfg, active_shift, product, address):
        items = [{'product_id': product.id, 'quantity': 1}]
        r = _post(auth_client, f'{C}/orders',
                  {'items': items, 'order_type': 'DELIVERY', 'address_id': address.id})
        assert r.status_code == 201, r.content


# --------------------------------------------------------------------------- #
#  Catalog (publish + stop-selling)                                           #
# --------------------------------------------------------------------------- #
class TestCatalog:
    def test_published_product_listed(self, auth_client, cfg, product):
        r = auth_client.get(f'{C}/catalog/products')
        ids = [p['id'] for p in r.json()['data']['items']]
        assert product.id in ids

    def test_stop_selling_hides_product(self, auth_client, cfg, product):
        product.bot.is_selling = False
        product.bot.save()
        r = auth_client.get(f'{C}/catalog/products')
        assert product.id not in [p['id'] for p in r.json()['data']['items']]

    def test_category_stop_hides_its_products(self, auth_client, cfg, product, category):
        category.bot.is_selling = False
        category.bot.save()
        r = auth_client.get(f'{C}/catalog/products')
        assert product.id not in [p['id'] for p in r.json()['data']['items']]

    def test_detail_exposes_sizes_and_toppings(self, auth_client, cfg, product):
        r = auth_client.get(f'{C}/catalog/products/{product.id}')
        data = r.json()['data']
        assert len(data['sizes']) == 2
        assert data['topping_groups'][0]['toppings']

    def test_detail_hidden_when_stopped(self, auth_client, cfg, product):
        product.bot.is_selling = False
        product.bot.save()
        assert auth_client.get(f'{C}/catalog/products/{product.id}').status_code == 404

    def test_detail_hidden_when_category_stopped(self, auth_client, cfg, product, category):
        category.bot.is_selling = False
        category.bot.save()
        assert auth_client.get(f'{C}/catalog/products/{product.id}').status_code == 404
