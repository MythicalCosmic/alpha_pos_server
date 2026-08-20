import json
import uuid
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from PIL import Image


pytestmark = pytest.mark.django_db

C = '/api/smartfood'
A = '/api/admins/smartfood'


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type='application/json')


def _patch(client, path, payload):
    return client.patch(path, data=json.dumps(payload), content_type='application/json')


def _image_bytes(image_format='PNG', size=(1200, 628)):
    output = BytesIO()
    Image.new('RGB', size, '#5c7cfa').save(output, format=image_format)
    return output.getvalue()


class TestCheckoutIdentity:
    def test_best_effort_pos_customer_link_does_not_poison_auth_transaction(
        self,
        customer,
        monkeypatch,
    ):
        from base.models import Customer as BaseCustomer
        from smartfood.models import CustomerSession
        from smartfood.services.auth_service import _link_base_customer

        def broken_resolve(**_kwargs):
            raise IntegrityError('simulated identity-link conflict')

        monkeypatch.setattr(BaseCustomer, 'resolve', broken_resolve)
        with transaction.atomic():
            assert _link_base_customer(customer) is None
            CustomerSession.objects.create(
                customer=customer,
                payload='a' * 64,
                expires_at=timezone.now(),
            )
        assert CustomerSession.objects.filter(payload='a' * 64).exists()

    def test_profile_requires_separate_names_phone_and_confirmation(
        self,
        auth_client,
        customer,
    ):
        customer.last_name = ''
        customer.phone_number = ''
        customer.profile_confirmed_at = None
        customer.save()

        incomplete = _patch(auth_client, f'{C}/me', {'confirm': True})
        assert incomplete.status_code == 422
        assert set(incomplete.json()['errors']) == {'last_name', 'phone'}

        completed = _patch(auth_client, f'{C}/me', {
            'first_name': 'Aziz',
            'last_name': 'Karimov',
            'phone': '+998 (90) 123-45-67',
            'confirm': True,
        })
        assert completed.status_code == 200, completed.content
        data = completed.json()['data']
        assert data['first_name'] == 'Aziz'
        assert data['last_name'] == 'Karimov'
        assert data['phone'] == '998901234567'
        assert data['profile_complete'] is True
        assert data['profile_missing'] == []

    def test_language_tracks_telegram_until_customer_overrides_it(
        self,
        client,
        bot_token,
        customer,
        monkeypatch,
    ):
        from smartfood.services.auth_service import CustomerAuthService

        monkeypatch.setattr(
            'smartfood.services.auth_service.verify_init_data',
            lambda _value: {
                'id': customer.telegram_id,
                'first_name': customer.first_name,
                'last_name': customer.last_name,
                'language_code': 'ru',
            },
        )
        CustomerAuthService.login_with_init_data('valid')
        customer.refresh_from_db()
        assert customer.language == 'ru'

        CustomerAuthService.update_profile(customer, language='en')
        customer.refresh_from_db()
        assert customer.language_overridden is True
        CustomerAuthService.login_with_init_data('valid')
        customer.refresh_from_db()
        assert customer.language == 'en'

    def test_telegram_refresh_does_not_replace_confirmed_delivery_name(
        self,
        customer,
        monkeypatch,
    ):
        from smartfood.services.auth_service import CustomerAuthService

        customer.first_name = 'Chosen'
        customer.last_name = 'Recipient'
        customer.profile_confirmed_at = timezone.now()
        customer.save()
        monkeypatch.setattr(
            'smartfood.services.auth_service.verify_init_data',
            lambda _value: {
                'id': customer.telegram_id,
                'first_name': 'Telegram',
                'last_name': 'Profile',
                'username': 'fresh_handle',
                'language_code': 'uz',
            },
        )

        CustomerAuthService.login_with_init_data('valid')
        customer.refresh_from_db()
        assert (customer.first_name, customer.last_name) == ('Chosen', 'Recipient')
        assert customer.username == 'fresh_handle'

    def test_order_rejects_incomplete_profile_and_unpinned_location(
        self,
        auth_client,
        cfg,
        active_shift,
        product,
        address,
        customer,
    ):
        payload = {
            'client_order_id': str(uuid.uuid4()),
            'items': [{'product_id': product.id, 'quantity': 1}],
            'order_type': 'DELIVERY',
            'address_id': address.id,
        }
        customer.profile_confirmed_at = None
        customer.save(update_fields=['profile_confirmed_at', 'updated_at'])
        missing_profile = _post(auth_client, f'{C}/orders', payload)
        assert missing_profile.status_code == 422
        assert missing_profile.json()['code'] == 'profile_required'

        customer.profile_confirmed_at = timezone.now()
        customer.save(update_fields=['profile_confirmed_at', 'updated_at'])
        address.lat = None
        address.lng = None
        address.save(update_fields=['lat', 'lng', 'updated_at'])
        payload['client_order_id'] = str(uuid.uuid4())
        missing_location = _post(auth_client, f'{C}/orders', payload)
        assert missing_location.status_code == 422
        assert missing_location.json()['code'] == 'location_required'

    def test_address_api_requires_a_map_pin(self, auth_client):
        response = _post(auth_client, f'{C}/addresses', {
            'line': 'Amir Temur 12',
        })
        assert response.status_code == 422
        assert set(response.json()['errors']) == {'lat', 'lng'}

    def test_address_api_rejects_oversized_text_before_database_save(self, auth_client):
        response = _post(auth_client, f'{C}/addresses', {
            'line': 'x' * 501,
            'label': 'y' * 41,
            'lat': '41.311158',
            'lng': '69.279737',
        })
        assert response.status_code == 422
        assert set(response.json()['errors']) == {'line', 'label'}

    def test_broadcast_preference_is_server_backed(self, auth_client, customer):
        response = _patch(auth_client, f'{C}/me', {'broadcast_opted_in': False})
        assert response.status_code == 200
        assert response.json()['data']['broadcast_opted_in'] is False
        customer.refresh_from_db()
        assert customer.broadcast_opted_in is False


class TestAdminAudience:
    def test_registry_summary_list_and_detail(
        self,
        operator_client,
        customer,
        address,
        cashier,
    ):
        from base.models import Order
        from smartfood.models import BotOrder, BotVisit

        BotVisit.objects.create(customer=customer, client_visit_id=uuid.uuid4())
        pos_order = Order.objects.create(
            user=cashier,
            cashier=cashier,
            status=Order.Status.COMPLETED,
            order_type=Order.OrderType.DELIVERY,
            order_origin=Order.Origin.TELEGRAM,
            total_amount=Decimal('52000'),
            is_paid=True,
            paid_at=timezone.now(),
            branch_id='branch-a',
        )
        BotOrder.objects.create(
            customer=customer,
            status=BotOrder.Status.DISPATCHED,
            total=Decimal('52000'),
            phone_number=customer.phone_number,
            pos_order=pos_order,
        )
        summary = operator_client.get(f'{A}/users/summary')
        assert summary.status_code == 200
        assert summary.json()['data']['total_users'] == 1
        assert summary.json()['data']['profile_complete'] == 1
        assert summary.json()['data']['customers_with_orders'] == 1

        listed = operator_client.get(f'{A}/users?profile=complete&ordered=true')
        assert listed.status_code == 200, listed.content
        item = listed.json()['data']['items'][0]
        assert item['telegram_id'] == customer.telegram_id
        assert item['visit_count'] == 1
        assert item['order_count'] == 1
        assert item['total_spent'] == 52000

        detail = operator_client.get(f'{A}/users/{customer.id}')
        assert detail.status_code == 200
        assert detail.json()['data']['addresses'][0]['id'] == address.id
        assert detail.json()['data']['recent_orders'][0]['totals']['total'] == 52000

    def test_checkout_ready_summary_and_filter_use_the_exact_identity_contract(
        self,
        operator_client,
        customer,
    ):
        from smartfood.models import Customer

        Customer.objects.create(
            telegram_id=777002,
            first_name='   ',
            last_name='Legacy',
            phone_number='998۹۰۱۲۳۴۵۶۷',
            profile_confirmed_at=timezone.now(),
        )
        summary = operator_client.get(f'{A}/users/summary').json()['data']
        assert summary['total_users'] == 2
        assert summary['profile_complete'] == 1
        complete = operator_client.get(f'{A}/users?profile=complete').json()['data']
        incomplete = operator_client.get(f'{A}/users?profile=incomplete').json()['data']
        assert [row['id'] for row in complete['items']] == [customer.id]
        assert incomplete['pagination']['total'] == 1


class TestBroadcasts:
    def test_customer_webhook_opens_mini_app_without_contact_gate(
        self,
        client,
        monkeypatch,
        settings,
        cfg,
        bot_token,
    ):
        settings.CUSTOMER_WEBHOOK_SECRET = 'customer-webhook-secret'
        settings.CUSTOMER_WEBAPP_URL = 'https://delivery.example/webapp/'
        cfg.enabled = False
        cfg.save(update_fields=['enabled', 'updated_at'])
        posted = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload
                self.ok = True

            def json(self):
                return self.payload

        def fake_post(url, **kwargs):
            posted.append((url, kwargs))
            if url.endswith('/sendMessage'):
                return FakeResponse({'ok': True, 'result': {'message_id': 812}})
            return FakeResponse({'ok': True, 'result': True})

        monkeypatch.setattr(
            'notifications.services.customer_bot.requests.post',
            fake_post,
        )
        response = client.post(
            '/api/customer-bot/webhook/',
            data=json.dumps({
                'update_id': 9001,
                'message': {
                    'chat': {'id': 5511},
                    'from': {'id': 5511, 'language_code': 'ru'},
                    'text': '/start',
                },
            }),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='customer-webhook-secret',
        )

        assert response.status_code == 200
        assert response.json() == {'ok': True}
        assert len(posted) == 2
        clear_url, clear_request = posted[0]
        assert clear_url.endswith('/sendMessage')
        assert 'можете посмотреть меню' in clear_request['json']['text']
        assert clear_request['json']['reply_markup'] == {'remove_keyboard': True}

        edit_url, edit_request = posted[1]
        assert edit_url.endswith('/editMessageReplyMarkup')
        assert edit_request['json']['message_id'] == 812
        inline = edit_request['json']['reply_markup']['inline_keyboard']
        assert inline[0][0]['web_app']['url'] == settings.CUSTOMER_WEBAPP_URL
        assert 'request_contact' not in json.dumps(posted)

        from smartfood.models import Customer
        unreachable = Customer.objects.create(
            telegram_id=5511,
            first_name='Legacy',
            last_name='Chat',
            phone_number='998901112244',
            telegram_reachable=False,
        )
        response = client.post(
            '/api/customer-bot/webhook/',
            data=json.dumps({
                'update_id': 9002,
                'message': {
                    'chat': {'id': 5511},
                    'from': {'id': 5511, 'language_code': 'ru'},
                    'text': '/start',
                },
            }),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='customer-webhook-secret',
        )
        assert response.status_code == 200
        unreachable.refresh_from_db()
        assert unreachable.telegram_reachable is True

    def test_customer_webhook_keeps_webapp_entry_when_markup_edit_fails(
        self,
        client,
        monkeypatch,
        settings,
        bot_token,
    ):
        from requests import RequestException

        settings.CUSTOMER_WEBHOOK_SECRET = 'customer-webhook-secret'
        settings.CUSTOMER_WEBAPP_URL = 'https://delivery.example/webapp/'
        posted = []

        class FakeResponse:
            ok = True

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        def fake_post(url, **kwargs):
            posted.append((url, kwargs))
            if url.endswith('/editMessageReplyMarkup'):
                raise RequestException('temporary edit failure')
            return FakeResponse({'ok': True, 'result': {'message_id': 813}})

        monkeypatch.setattr(
            'notifications.services.customer_bot.requests.post',
            fake_post,
        )
        response = client.post(
            '/api/customer-bot/webhook/',
            data=json.dumps({
                'update_id': 9003,
                'message': {
                    'chat': {'id': 6611},
                    'from': {'id': 6611, 'language_code': 'en'},
                    'text': '/start',
                },
            }),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='customer-webhook-secret',
        )

        assert response.status_code == 200
        assert [url.rsplit('/', 1)[-1] for url, _ in posted] == [
            'sendMessage',
            'editMessageReplyMarkup',
            'sendMessage',
        ]
        fallback = posted[-1][1]['json']
        assert fallback['reply_markup']['inline_keyboard'][0][0]['web_app']['url'] == settings.CUSTOMER_WEBAPP_URL

    def test_draft_fans_out_localized_immutable_messages(
        self,
        operator_client,
        bot_token,
        customer,
    ):
        from smartfood.models import BotBroadcast, BotOutboundMessage, Customer

        russian = Customer.objects.create(
            telegram_id=880022,
            first_name='Anna',
            last_name='Petrova',
            phone_number='998901112233',
            language='ru',
            profile_confirmed_at=timezone.now(),
        )
        blocked = Customer.objects.create(
            telegram_id=880033,
            first_name='Blocked',
            last_name='User',
            language='en',
            is_blocked=True,
        )
        opted_out = Customer.objects.create(
            telegram_id=880044,
            first_name='No',
            last_name='Marketing',
            broadcast_opted_in=False,
        )
        created = _post(operator_client, f'{A}/broadcasts', {
            'title': 'Friday offer',
            'messages': {
                'uz': 'Bugun maxsus taklif',
                'ru': 'Сегодня специальное предложение',
                'en': '',
            },
        })
        assert created.status_code == 201, created.content
        created_data = created.json()['data']
        broadcast_id = created_data['id']

        queued = _post(operator_client, f'{A}/broadcasts/{broadcast_id}/send', {
            'expected_updated_at': created_data['updated_at'],
        })
        assert queued.status_code == 200, queued.content
        assert queued.json()['data']['status'] == BotBroadcast.Status.QUEUED
        assert queued.json()['data']['recipient_count'] == 2

        rows = {
            row.customer_id: row
            for row in BotOutboundMessage.objects.filter(broadcast_id=broadcast_id)
        }
        assert rows[customer.id].text == 'Bugun maxsus taklif'
        assert rows[russian.id].text == 'Сегодня специальное предложение'
        assert blocked.id not in rows
        assert opted_out.id not in rows

        edit_after_queue = _patch(operator_client, f'{A}/broadcasts/{broadcast_id}', {
            'title': 'Changed',
        })
        assert edit_after_queue.status_code == 409

    def test_opt_out_after_queue_is_skipped_before_network_delivery(
        self,
        monkeypatch,
        operator_client,
        bot_token,
        customer,
    ):
        from smartfood.models import BotBroadcast, BotOutboundMessage
        from smartfood.services.outbound_message_service import OutboundMessageService

        created = _post(operator_client, f'{A}/broadcasts', {
            'title': 'Queued offer',
            'messages': {'uz': 'Taklif'},
        }).json()['data']
        queued = _post(
            operator_client,
            f"{A}/broadcasts/{created['id']}/send",
            {'expected_updated_at': created['updated_at']},
        )
        assert queued.status_code == 200
        customer.broadcast_opted_in = False
        customer.save(update_fields=['broadcast_opted_in', 'updated_at'])
        monkeypatch.setattr(
            'smartfood.services.outbound_message_service.requests.post',
            lambda *_args, **_kwargs: pytest.fail('opted-out customer was sent a message'),
        )

        result = OutboundMessageService.process_due(limit=10)
        assert result['skipped'] == 1
        message = BotOutboundMessage.objects.get(broadcast_id=created['id'])
        assert message.status == BotOutboundMessage.Status.SKIPPED
        broadcast = BotBroadcast.objects.get(id=created['id'])
        assert broadcast.skipped_count == 1
        assert broadcast.failed_count == 0
        assert broadcast.status == BotBroadcast.Status.PARTIAL

    def test_send_rejects_a_draft_changed_after_operator_review(
        self,
        operator_client,
        bot_token,
        customer,
    ):
        from smartfood.models import BotBroadcast, BotOutboundMessage

        created = _post(operator_client, f'{A}/broadcasts', {
            'title': 'Reviewed copy',
            'messages': {'uz': 'Eski matn'},
        }).json()['data']
        changed = _patch(
            operator_client,
            f"{A}/broadcasts/{created['id']}",
            {'messages': {'uz': 'Yangi matn'}},
        ).json()['data']

        stale = _post(
            operator_client,
            f"{A}/broadcasts/{created['id']}/send",
            {'expected_updated_at': created['updated_at']},
        )
        assert stale.status_code == 409
        assert stale.json()['code'] == 'broadcast_changed'
        assert stale.json()['data']['messages']['uz'] == 'Yangi matn'
        assert BotBroadcast.objects.get(id=created['id']).status == BotBroadcast.Status.DRAFT
        assert not BotOutboundMessage.objects.filter(broadcast_id=created['id']).exists()

        queued = _post(
            operator_client,
            f"{A}/broadcasts/{created['id']}/send",
            {'expected_updated_at': changed['updated_at']},
        )
        assert queued.status_code == 200
        assert queued.json()['data']['status'] == BotBroadcast.Status.QUEUED

    def test_broadcast_media_is_decoded_converted_and_locked_after_queue(
        self,
        operator_client,
        bot_token,
        customer,
        settings,
        tmp_path,
    ):
        settings.MEDIA_ROOT = tmp_path
        created = _post(operator_client, f'{A}/broadcasts', {
            'title': 'Photo offer',
            'messages': {'uz': 'Bugungi taklif'},
        }).json()['data']
        path = f"{A}/broadcasts/{created['id']}/image"
        uploaded = operator_client.post(path, data={
            'image': SimpleUploadedFile(
                'offer.webp',
                _image_bytes('WEBP'),
                content_type='image/webp',
            ),
        })
        assert uploaded.status_code == 200, uploaded.content
        image_url = uploaded.json()['data']['image_url']
        assert image_url.endswith('.jpg')
        media = operator_client.get(image_url)
        assert media.status_code == 200
        assert media['Content-Type'] == 'image/jpeg'

        uploaded_data = uploaded.json()['data']
        queued = _post(operator_client, f"{A}/broadcasts/{created['id']}/send", {
            'expected_updated_at': uploaded_data['updated_at'],
        })
        assert queued.status_code == 200
        replacement = operator_client.post(path, data={
            'image': SimpleUploadedFile('new.png', _image_bytes(), content_type='image/png'),
        })
        removed = operator_client.delete(path)
        assert replacement.status_code == 409
        assert removed.status_code == 409
        assert operator_client.get(image_url).status_code == 200

    def test_missing_broadcast_photo_cannot_be_queued(
        self,
        operator_client,
        bot_token,
        customer,
        settings,
        tmp_path,
    ):
        from django.core.files.storage import default_storage
        from smartfood.models import BotBroadcast, BotOutboundMessage
        from smartfood.services.media_service import managed_media_storage_name

        settings.MEDIA_ROOT = tmp_path
        created = _post(operator_client, f'{A}/broadcasts', {
            'title': 'Missing photo',
            'messages': {'uz': 'Rasmli taklif'},
        }).json()['data']
        path = f"{A}/broadcasts/{created['id']}/image"
        uploaded = operator_client.post(path, data={
            'image': SimpleUploadedFile(
                'offer.png',
                _image_bytes(),
                content_type='image/png',
            ),
        }).json()['data']
        storage_name = managed_media_storage_name(
            uploaded['image_url'],
            'broadcasts',
        )
        default_storage.delete(storage_name)

        response = _post(
            operator_client,
            f"{A}/broadcasts/{created['id']}/send",
            {'expected_updated_at': uploaded['updated_at']},
        )
        assert response.status_code == 409
        assert response.json()['code'] == 'broadcast_image_missing'
        assert BotBroadcast.objects.get(id=created['id']).status == BotBroadcast.Status.DRAFT
        assert BotOutboundMessage.objects.filter(broadcast_id=created['id']).count() == 0

    def test_broadcast_media_rejects_corrupt_and_telegram_invalid_geometry(
        self,
        operator_client,
        settings,
        tmp_path,
    ):
        settings.MEDIA_ROOT = tmp_path
        created = _post(operator_client, f'{A}/broadcasts', {
            'title': 'Media validation',
            'messages': {'uz': 'Tekshiruv'},
        }).json()['data']
        path = f"{A}/broadcasts/{created['id']}/image"
        corrupt = operator_client.post(path, data={
            'image': SimpleUploadedFile(
                'broken.png',
                b'\x89PNG\r\n\x1a\ntruncated',
                content_type='image/png',
            ),
        })
        too_wide = operator_client.post(path, data={
            'image': SimpleUploadedFile(
                'wide.png',
                _image_bytes('PNG', size=(1000, 10)),
                content_type='image/png',
            ),
        })
        assert corrupt.status_code == 422
        assert too_wide.status_code == 422
        assert 'aspect ratio' in too_wide.json()['errors']['image']

    def test_worker_sends_once_and_finishes_broadcast(
        self,
        monkeypatch,
        operator_client,
        bot_token,
        customer,
    ):
        from smartfood.models import BotBroadcast, BotOutboundMessage
        from smartfood.services.outbound_message_service import OutboundMessageService

        created = _post(operator_client, f'{A}/broadcasts', {
            'title': 'Service note',
            'messages': {'uz': 'Bugun soat 22:00 gacha ochiqmiz.'},
        }).json()['data']
        _post(operator_client, f"{A}/broadcasts/{created['id']}/send", {
            'expected_updated_at': created['updated_at'],
        })
        sent = []

        class Response:
            status_code = 200

            def json(self):
                return {'ok': True, 'result': {'message_id': 7001}}

        def fake_post(url, data, files, timeout):
            sent.append((url, data, files, timeout))
            return Response()

        monkeypatch.setattr(
            'smartfood.services.outbound_message_service.requests.post',
            fake_post,
        )
        result = OutboundMessageService.process_due(limit=10)
        assert result == {
            'claimed': 1,
            'sent': 1,
            'failed': 0,
            'retrying': 0,
            'skipped': 0,
        }
        message = BotOutboundMessage.objects.get(broadcast_id=created['id'])
        assert message.status == BotOutboundMessage.Status.SENT
        assert message.telegram_message_id == 7001
        broadcast = BotBroadcast.objects.get(id=created['id'])
        assert broadcast.status == BotBroadcast.Status.SENT
        assert broadcast.delivered_count == 1
        assert len(sent) == 1
        assert OutboundMessageService.process_due(limit=10)['claimed'] == 0

    def test_start_reply_opens_webapp_without_requesting_contact(
        self,
        monkeypatch,
        settings,
        cfg,
        bot_token,
    ):
        from smartfood.management.commands.run_customer_bot import Command

        settings.CUSTOMER_WEBAPP_URL = 'https://delivery.example/webapp/'
        posted = []

        class Response:
            ok = True

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        def fake_post(url, **kwargs):
            posted.append((url, kwargs))
            if url.endswith('/sendMessage'):
                return Response({'ok': True, 'result': {'message_id': 991}})
            return Response({'ok': True, 'result': True})

        monkeypatch.setattr(
            'smartfood.management.commands.run_customer_bot.requests.post',
            fake_post,
        )
        Command()._handle(bot_token, {
            'message': {
                'chat': {'id': 5511},
                'from': {'language_code': 'ru'},
                'text': '/start',
            },
        })
        assert len(posted) == 2
        clear_payload = posted[0][1]['json']
        assert clear_payload['reply_markup'] == {'remove_keyboard': True}
        assert 'Откройте меню' in clear_payload['text']
        edit_payload = posted[1][1]['json']
        assert edit_payload['message_id'] == 991
        assert edit_payload['reply_markup']['inline_keyboard'][0][0]['web_app']['url'] == settings.CUSTOMER_WEBAPP_URL
        assert 'request_contact' not in json.dumps(posted)

    def test_start_still_opens_webapp_while_ordering_is_closed(
        self,
        monkeypatch,
        settings,
        cfg,
        bot_token,
    ):
        from smartfood.management.commands.run_customer_bot import Command

        settings.CUSTOMER_WEBAPP_URL = 'https://delivery.example/webapp/'
        cfg.enabled = False
        cfg.save(update_fields=['enabled', 'updated_at'])
        posted = []

        class Response:
            ok = True

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        def fake_post(url, **kwargs):
            posted.append((url, kwargs))
            if url.endswith('/sendMessage'):
                return Response({'ok': True, 'result': {'message_id': 992}})
            return Response({'ok': True, 'result': True})

        monkeypatch.setattr(
            'smartfood.management.commands.run_customer_bot.requests.post',
            fake_post,
        )
        Command()._handle(bot_token, {
            'message': {
                'chat': {'id': 5511},
                'from': {'language_code': 'en'},
                'text': '/start',
            },
        })
        assert len(posted) == 2
        clear_payload = posted[0][1]['json']
        assert 'browse the menu' in clear_payload['text']
        assert clear_payload['reply_markup'] == {'remove_keyboard': True}
        edit_payload = posted[1][1]['json']
        assert edit_payload['reply_markup']['inline_keyboard'][0][0]['web_app']['url'] == settings.CUSTOMER_WEBAPP_URL

    def test_webhook_setup_gates_polling_but_menu_setup_is_optional(
        self,
        monkeypatch,
        settings,
        bot_token,
    ):
        from smartfood.management.commands.run_customer_bot import Command

        settings.CUSTOMER_WEBAPP_URL = 'https://delivery.example/webapp/'

        class Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self.ok = status_code < 400
                self.payload = payload

            def json(self):
                return self.payload

        responses = iter([
            Response(500, {'ok': False, 'description': 'temporary'}),
            Response(200, {'ok': True, 'result': True}),
            Response(500, {'ok': False, 'description': 'menu unavailable'}),
        ])
        monkeypatch.setattr(
            'smartfood.management.commands.run_customer_bot.requests.post',
            lambda *_args, **_kwargs: next(responses),
        )
        assert Command._configure_token(bot_token) is False
        assert Command._configure_token(bot_token) is True
        assert Command._configure_menu_button(bot_token) is False
