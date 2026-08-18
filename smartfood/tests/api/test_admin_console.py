"""Focused contracts for the Telegram delivery administration console."""
import json
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from smartfood.tests.conftest import make_init_data


pytestmark = pytest.mark.django_db

C = '/api/smartfood'
A = '/api/admins/smartfood'


def _post_json(client, path, payload):
    return client.post(
        path,
        data=json.dumps(payload),
        content_type='application/json',
    )


class TestVisitAnalytics:
    def test_visit_event_is_idempotent(self, auth_client, customer):
        visit_id = str(uuid.uuid4())

        first = _post_json(
            auth_client,
            f'{C}/analytics/visit',
            {'client_visit_id': visit_id},
        )
        replay = _post_json(
            auth_client,
            f'{C}/analytics/visit',
            {'client_visit_id': visit_id},
        )

        assert first.status_code == 200
        assert first.json()['data']['recorded'] is True
        assert replay.status_code == 200
        assert replay.json()['data']['recorded'] is False
        from smartfood.models import BotVisit
        assert BotVisit.objects.filter(customer=customer).count() == 1

    def test_overview_and_visitor_conversion(self, operator_client, customer):
        from smartfood.models import BotOrder, BotVisit

        BotVisit.objects.create(customer=customer, client_visit_id=uuid.uuid4())
        BotVisit.objects.create(customer=customer, client_visit_id=uuid.uuid4())
        BotOrder.objects.create(customer=customer, total=39000)

        response = operator_client.get(f'{A}/analytics/overview?days=7')
        assert response.status_code == 200, response.content
        data = response.json()['data']
        assert data['metrics'] == {
            'app_opens': 2,
            'unique_visitors': 1,
            'converted_visitors': 1,
            'orders': 1,
            'conversion_rate': 100.0,
        }
        assert len(data['series']) == 7
        assert data['series'][-1]['date'] == timezone.localdate().isoformat()

        visitors = operator_client.get(f'{A}/visitors?converted=true')
        assert visitors.status_code == 200
        item = visitors.json()['data']['items'][0]
        assert item['telegram_id'] == customer.telegram_id
        assert item['visit_count'] == 2
        assert item['order_count'] == 1
        assert item['converted'] is True

    def test_daily_conversion_excludes_customers_without_a_same_day_visit(
        self,
        operator_client,
        customer,
    ):
        from smartfood.models import BotOrder, BotVisit, Customer

        BotVisit.objects.create(customer=customer, client_visit_id=uuid.uuid4())
        BotOrder.objects.create(customer=customer, total=39000)
        untracked = Customer.objects.create(
            telegram_id=777002,
            first_name='No visit',
        )
        BotOrder.objects.create(customer=untracked, total=22000)

        response = operator_client.get(f'{A}/analytics/overview?days=7')

        assert response.status_code == 200, response.content
        today = response.json()['data']['series'][-1]
        assert today['unique_visitors'] == 1
        assert today['orders'] == 2
        assert today['converted_visitors'] == 1


class TestCatalogAdministration:
    def test_list_and_import_products(self, operator_client, product, category):
        from base.models import Product

        source = Product.objects.create(
            name='Lemonade',
            price=12000,
            category=category,
        )
        listed = operator_client.get(f'{A}/catalog/products')
        assert listed.status_code == 200
        assert product.id in {
            item['id'] for item in listed.json()['data']['items']
        }
        assert listed.json()['data']['summary']['published_products'] == 1

        filtered = operator_client.get(f'{A}/catalog/products?q=does-not-exist')
        assert filtered.json()['data']['items'] == []
        assert filtered.json()['data']['summary']['published_products'] == 1

        imported = _post_json(
            operator_client,
            f'{A}/catalog/import',
            {'product_ids': [source.id]},
        )
        assert imported.status_code == 200, imported.content
        assert imported.json()['data']['items'][0]['id'] == source.id
        assert source.bot.is_published is True
        assert category.bot.is_published is True

    def test_upload_and_serve_product_image(
        self,
        operator_client,
        product,
        settings,
        tmp_path,
    ):
        settings.MEDIA_ROOT = tmp_path
        image = SimpleUploadedFile(
            'dish.png',
            b'\x89PNG\r\n\x1a\n' + (b'product-image' * 20),
            content_type='image/png',
        )
        uploaded = operator_client.post(
            f'{A}/products/{product.id}/image',
            data={'image': image},
        )
        assert uploaded.status_code == 200, uploaded.content
        image_url = uploaded.json()['data']['image_url']
        assert image_url.startswith(f'{C}/media/products/')
        product.bot.refresh_from_db()
        assert product.bot.image_url == image_url

        public = operator_client.get(image_url)
        assert public.status_code == 200
        assert public['Content-Type'] == 'image/png'
        assert public['Cache-Control'] == 'public, max-age=31536000, immutable'

    def test_rejects_non_image_upload(self, operator_client, product, settings, tmp_path):
        settings.MEDIA_ROOT = tmp_path
        uploaded = operator_client.post(
            f'{A}/products/{product.id}/image',
            data={
                'image': SimpleUploadedFile(
                    'dish.html',
                    b'<script>alert(1)</script>',
                    content_type='text/html',
                ),
            },
        )
        assert uploaded.status_code == 422

    def test_optional_calories_can_be_cleared(self, operator_client, product):
        product.bot.kcal = 640
        product.bot.save(update_fields=['kcal', 'updated_at'])

        response = operator_client.patch(
            f'{A}/products/{product.id}',
            data=json.dumps({'kcal': None}),
            content_type='application/json',
        )

        assert response.status_code == 200, response.content
        assert response.json()['data']['kcal'] is None
        product.bot.refresh_from_db()
        assert product.bot.kcal is None


class TestRuntimeBotSettings:
    def test_token_is_masked_and_used_for_customer_auth(
        self,
        operator_client,
        client,
        cfg,
        settings,
    ):
        runtime_token = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd'
        settings.CUSTOMER_BOT_TOKEN = '999999:ENVIRONMENT-TOKEN-IS-DIFFERENT'

        updated = _post_json(
            operator_client,
            f'{A}/config',
            {'bot_token': runtime_token},
        )
        assert updated.status_code == 200, updated.content
        body = updated.json()
        assert runtime_token not in json.dumps(body)
        assert body['data']['bot']['token_configured'] is True
        assert body['data']['bot']['token_source'] == 'runtime'
        assert body['data']['bot']['token_masked'].endswith('abcd')
        assert body['data']['bot']['environment_fallback_configured'] is True

        init_data = make_init_data(
            runtime_token,
            {'id': 908070, 'first_name': 'Visitor'},
        )
        auth = _post_json(client, f'{C}/auth', {'init_data': init_data})
        assert auth.status_code == 200, auth.content
