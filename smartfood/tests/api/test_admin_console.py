"""Focused contracts for the Telegram delivery administration console."""
import json
import uuid
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

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


def _image_bytes(image_format='PNG', size=(64, 64)):
    output = BytesIO()
    Image.new('RGB', size, '#e95c3f').save(output, format=image_format)
    return output.getvalue()


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
        product_row = next(
            item for item in listed.json()['data']['items']
            if item['id'] == product.id
        )
        assert product_row['names'] == {
            'uz': 'Classic',
            'ru': 'Classic',
            'en': 'Classic Burger',
        }
        assert product_row['name_overrides'] == {
            'uz': '',
            'ru': '',
            'en': 'Classic Burger',
        }
        assert product_row['description_overrides'] == {
            'uz': '',
            'ru': '',
            'en': '',
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
            _image_bytes('PNG'),
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

    def test_updates_customer_facing_product_details(self, operator_client, product):
        response = operator_client.patch(
            f'{A}/products/{product.id}',
            data=json.dumps({
                'name_uz': 'Klassik burger',
                'name_ru': 'Классический бургер',
                'name_en': 'Classic burger',
                'desc_uz': 'Yangi tayyorlanadi',
                'desc_ru': 'Готовится свежим',
                'desc_en': 'Made fresh',
                'tag': 'Popular',
                'kcal': 640,
                'sort_order': 4,
            }),
            content_type='application/json',
        )

        assert response.status_code == 200, response.content
        data = response.json()['data']
        assert data['name_overrides']['uz'] == 'Klassik burger'
        assert data['description_overrides']['en'] == 'Made fresh'
        product.bot.refresh_from_db()
        assert product.bot.name_ru == 'Классический бургер'
        assert product.bot.kcal == 640
        assert product.bot.sort_order == 4

    @pytest.mark.parametrize(
        ('payload', 'field'),
        [
            ({'sort_order': ''}, 'sort_order'),
            ({'sort_order': -1}, 'sort_order'),
            ({'sort_order': 2.5}, 'sort_order'),
            ({'kcal': -1}, 'kcal'),
            ({'kcal': 2.5}, 'kcal'),
            ({'name_uz': 'x' * 121}, 'name_uz'),
            ({'tag': 'x' * 21}, 'tag'),
            ({'is_selling': 'false'}, 'is_selling'),
        ],
    )
    def test_invalid_product_edits_return_field_errors(
        self,
        operator_client,
        product,
        payload,
        field,
    ):
        before = {
            'sort_order': product.bot.sort_order,
            'kcal': product.bot.kcal,
            'name_uz': product.bot.name_uz,
            'tag': product.bot.tag,
            'is_selling': product.bot.is_selling,
        }

        response = operator_client.patch(
            f'{A}/products/{product.id}',
            data=json.dumps(payload),
            content_type='application/json',
        )

        assert response.status_code == 422, response.content
        assert field in response.json()['errors']
        product.bot.refresh_from_db()
        assert {
            'sort_order': product.bot.sort_order,
            'kcal': product.bot.kcal,
            'name_uz': product.bot.name_uz,
            'tag': product.bot.tag,
            'is_selling': product.bot.is_selling,
        } == before

    def test_customer_availability_includes_the_category_gate(
        self,
        operator_client,
        product,
        category,
    ):
        initial = operator_client.get(f'{A}/catalog/products')
        item = next(
            row for row in initial.json()['data']['items']
            if row['id'] == product.id
        )
        assert item['available'] is True
        assert item['customer_available'] is True

        category.bot.is_selling = False
        category.bot.save(update_fields=['is_selling', 'updated_at'])

        stopped = operator_client.get(f'{A}/catalog/products')
        item = next(
            row for row in stopped.json()['data']['items']
            if row['id'] == product.id
        )
        assert item['available'] is True
        assert item['customer_available'] is False
        assert stopped.json()['data']['summary']['available_products'] == 0
        assert operator_client.get(
            f'{A}/catalog/products?availability=available'
        ).json()['data']['items'] == []
        unavailable = operator_client.get(
            f'{A}/catalog/products?availability=stopped'
        ).json()['data']['items']
        assert [row['id'] for row in unavailable] == [product.id]
        overview = operator_client.get(f'{A}/analytics/overview?days=7')
        assert overview.json()['data']['catalog']['available_products'] == 0


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

    def test_loyalty_economics_are_validated_and_returned(self, operator_client):
        updated = _post_json(
            operator_client,
            f'{A}/config',
            {'loyalty_earn_per': 1500, 'loyalty_point_value': 125},
        )

        assert updated.status_code == 200, updated.content
        assert updated.json()['data']['loyalty_earn_per'] == 1500
        assert updated.json()['data']['loyalty_point_value'] == 125

        invalid = _post_json(
            operator_client,
            f'{A}/config',
            {'loyalty_earn_per': -1},
        )
        assert invalid.status_code == 422

    def test_loyalty_earning_and_checkout_spending_are_independent(
        self,
        operator_client,
    ):
        spending_only = _post_json(
            operator_client,
            f'{A}/config',
            {'loyalty_earn_per': 0, 'loyalty_point_value': 125},
        )
        assert spending_only.status_code == 200, spending_only.content
        flags = spending_only.json()['data']['feature_flags']
        assert flags['loyalty'] is True
        assert flags['loyalty_earning'] is False
        assert flags['loyalty_spending'] is True

        earning_only = _post_json(
            operator_client,
            f'{A}/config',
            {'loyalty_earn_per': 1500, 'loyalty_point_value': 0},
        )
        assert earning_only.status_code == 200, earning_only.content
        flags = earning_only.json()['data']['feature_flags']
        assert flags['loyalty'] is True
        assert flags['loyalty_earning'] is True
        assert flags['loyalty_spending'] is False


class TestMarketingAdministration:
    def test_banner_draft_upload_publish_and_public_schedule(
        self,
        operator_client,
        auth_client,
        settings,
        tmp_path,
    ):
        settings.MEDIA_ROOT = tmp_path
        draft = _post_json(
            operator_client,
            f'{A}/marketing/banners',
            {
                'title_uz': 'Bugungi tanlov',
                'title_en': "Today's pick",
                'subtitle_uz': 'Bir bosishda menyuni oching',
                'action_type': 'CATALOG',
                'sort_order': 2,
                'is_active': False,
            },
        )
        assert draft.status_code == 201, draft.content
        banner_id = draft.json()['data']['id']

        cannot_publish = operator_client.patch(
            f'{A}/marketing/banners/{banner_id}',
            data=json.dumps({'is_active': True}),
            content_type='application/json',
        )
        assert cannot_publish.status_code == 422

        uploaded = operator_client.post(
            f'{A}/marketing/banners/{banner_id}/image',
            data={
                'image': SimpleUploadedFile(
                    'banner.webp',
                    _image_bytes('WEBP'),
                    content_type='image/webp',
                ),
            },
        )
        assert uploaded.status_code == 200, uploaded.content
        image_url = uploaded.json()['data']['image_url']
        assert image_url.startswith(f'{C}/media/banners/')

        published = operator_client.patch(
            f'{A}/marketing/banners/{banner_id}',
            data=json.dumps({'is_active': True}),
            content_type='application/json',
        )
        assert published.status_code == 200, published.content

        public = auth_client.get(f'{C}/banners?lang=en')
        assert public.status_code == 200, public.content
        assert public.json()['data']['items'] == [{
            'id': banner_id,
            'title': "Today's pick",
            'subtitle': 'Bir bosishda menyuni oching',
            'image_url': image_url,
            'action_type': 'CATALOG',
            'product_id': None,
        }]

        media = auth_client.get(image_url)
        assert media.status_code == 200
        assert media['Content-Type'] == 'image/webp'

        expired = operator_client.patch(
            f'{A}/marketing/banners/{banner_id}',
            data=json.dumps({'ends_at': '2020-01-01T00:00:00Z'}),
            content_type='application/json',
        )
        assert expired.status_code == 200, expired.content
        assert auth_client.get(f'{C}/banners').json()['data']['items'] == []

    def test_product_banner_disappears_when_destination_stops_selling(
        self,
        operator_client,
        auth_client,
        product,
    ):
        from smartfood.models import BotBanner

        banner = BotBanner.objects.create(
            title_uz='Mahsulot',
            image_url='/api/smartfood/media/banners/' + ('a' * 32) + '.webp',
            action_type=BotBanner.Action.PRODUCT,
            product=product,
            is_active=True,
        )
        public = auth_client.get(f'{C}/banners')
        assert public.json()['data']['items'][0]['id'] == banner.id

        product.bot.is_selling = False
        product.bot.save(update_fields=['is_selling', 'updated_at'])

        assert auth_client.get(f'{C}/banners').json()['data']['items'] == []
        rejected = operator_client.patch(
            f'{A}/marketing/banners/{banner.id}',
            data=json.dumps({'is_active': True}),
            content_type='application/json',
        )
        assert rejected.status_code == 422
        assert 'product_id' in rejected.json()['errors']

        paused = operator_client.patch(
            f'{A}/marketing/banners/{banner.id}',
            data=json.dumps({'is_active': False}),
            content_type='application/json',
        )
        assert paused.status_code == 200, paused.content
        assert paused.json()['data']['is_active'] is False

    def test_reward_catalog_management_and_customer_visibility(
        self,
        operator_client,
        auth_client,
        settings,
        tmp_path,
    ):
        settings.MEDIA_ROOT = tmp_path
        invalid = _post_json(
            operator_client,
            f'{A}/loyalty/rewards',
            {'name_uz': 'Noto‘g‘ri', 'kind': 'CUSTOM', 'points_cost': 0},
        )
        assert invalid.status_code == 422

        created = _post_json(
            operator_client,
            f'{A}/loyalty/rewards',
            {
                'name_uz': 'Sirli sovg‘a',
                'name_en': 'Mystery gift',
                'desc_uz': 'Kassada kodni ko‘rsating',
                'kind': 'CUSTOM',
                'points_cost': 75,
                'stock': 5,
                'per_customer_limit': 1,
                'sort_order': 1,
                'is_active': True,
            },
        )
        assert created.status_code == 201, created.content
        reward_id = created.json()['data']['id']

        uploaded = operator_client.post(
            f'{A}/loyalty/rewards/{reward_id}/image',
            data={
                'image': SimpleUploadedFile(
                    'reward.png',
                    _image_bytes('PNG'),
                    content_type='image/png',
                ),
            },
        )
        assert uploaded.status_code == 200, uploaded.content
        image_url = uploaded.json()['data']['image_url']

        listed = operator_client.get(f'{A}/loyalty/rewards')
        assert listed.status_code == 200
        assert listed.json()['data']['summary']['active'] == 1
        assert listed.json()['data']['items'][0]['stock'] == 5
        assert uploaded.json()['data']['customer_available'] is True

        customer_catalog = auth_client.get(f'{C}/rewards?lang=en')
        item = customer_catalog.json()['data']['items'][0]
        assert item['id'] == reward_id
        assert item['name'] == 'Mystery gift'
        assert item['image_url'] == image_url

        media = auth_client.get(image_url)
        assert media.status_code == 200
        assert media['Content-Type'] == 'image/png'

    def test_reward_catalog_exposes_reached_customer_limit(
        self,
        operator_client,
        auth_client,
        customer,
    ):
        from smartfood.models import Redemption, Reward

        customer.loyalty_points = 500
        customer.save(update_fields=['loyalty_points', 'updated_at'])
        created = _post_json(
            operator_client,
            f'{A}/loyalty/rewards',
            {
                'name_uz': 'Bir marta',
                'kind': 'CUSTOM',
                'points_cost': 100,
                'per_customer_limit': 1,
                'is_active': True,
            },
        )
        assert created.status_code == 201, created.content
        reward = Reward.objects.get(id=created.json()['data']['id'])
        Redemption.objects.create(
            customer=customer,
            reward=reward,
            code='GIFT-LIMIT1',
            points_spent=reward.points_cost,
            reward_name=reward.name_uz,
            kind=reward.kind,
        )

        catalog = auth_client.get(f'{C}/rewards?lang=uz')
        assert catalog.status_code == 200, catalog.content
        item = catalog.json()['data']['items'][0]
        assert item['affordable'] is True
        assert item['limit_reached'] is True
        assert item['can_redeem'] is False

        rejected = auth_client.post(f'{C}/rewards/{reward.id}/redeem')
        assert rejected.status_code == 400
        assert 'limit' in rejected.json()['message'].lower()

    def test_free_product_reward_becomes_unavailable_when_category_stops(
        self,
        operator_client,
        auth_client,
        product,
        category,
    ):
        created = _post_json(
            operator_client,
            f'{A}/loyalty/rewards',
            {
                'name_uz': 'Bepul mahsulot',
                'kind': 'FREE_PRODUCT',
                'product_id': product.id,
                'points_cost': 100,
                'is_active': True,
            },
        )
        assert created.status_code == 201, created.content
        reward_id = created.json()['data']['id']
        assert created.json()['data']['customer_available'] is True

        category.bot.is_selling = False
        category.bot.save(update_fields=['is_selling', 'updated_at'])

        listed = operator_client.get(f'{A}/loyalty/rewards')
        assert listed.status_code == 200, listed.content
        item = next(
            row for row in listed.json()['data']['items']
            if row['id'] == reward_id
        )
        assert item['is_active'] is True
        assert item['customer_available'] is False
        assert listed.json()['data']['summary']['active'] == 0
        assert auth_client.get(f'{C}/rewards').json()['data']['items'] == []

        paused = operator_client.patch(
            f'{A}/loyalty/rewards/{reward_id}',
            data=json.dumps({'is_active': False}),
            content_type='application/json',
        )
        assert paused.status_code == 200, paused.content
        assert paused.json()['data']['is_active'] is False

    def test_media_storage_failure_is_a_stable_service_error(
        self,
        operator_client,
        product,
        monkeypatch,
    ):
        from smartfood.services import media_service

        def fail_save(*_args, **_kwargs):
            raise OSError('disk unavailable')

        monkeypatch.setattr(media_service.default_storage, 'save', fail_save)
        response = operator_client.post(
            f'{A}/products/{product.id}/image',
            data={
                'image': SimpleUploadedFile(
                    'dish.png',
                    _image_bytes('PNG'),
                    content_type='image/png',
                ),
            },
        )

        assert response.status_code == 503
        assert response.json()['success'] is False
        assert 'storage' in response.json()['message'].lower()
