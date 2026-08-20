"""Durable Telegram outbox retry, suppression, and ordering contracts."""

from datetime import timedelta

import pytest
import requests
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _message(customer, *, text='Hello', bot_order=None, event_key=''):
    from smartfood.models import BotOutboundMessage

    return BotOutboundMessage.objects.create(
        kind=(
            BotOutboundMessage.Kind.ORDER_STATUS
            if bot_order else BotOutboundMessage.Kind.BROADCAST
        ),
        customer=customer,
        bot_order=bot_order,
        event_key=event_key,
        chat_id=customer.telegram_id,
        language='uz',
        text=text,
    )


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_missing_or_invalid_bot_token_does_not_consume_attempts(
    settings,
    cfg,
    customer,
    monkeypatch,
):
    from smartfood.models import BotOutboundMessage
    from smartfood.services.outbound_message_service import OutboundMessageService

    settings.CUSTOMER_BOT_TOKEN = ''
    cfg.bot_token = ''
    cfg.save()
    missing = _message(customer)
    result = OutboundMessageService.process_due(limit=1)
    missing.refresh_from_db()
    assert result['retrying'] == 1
    assert missing.status == BotOutboundMessage.Status.PENDING
    assert missing.attempts == 0
    assert 'not configured' in missing.last_error

    settings.CUSTOMER_BOT_TOKEN = '123:invalid'
    BotOutboundMessage.objects.filter(id=missing.id).update(
        next_attempt_at=timezone.now() - timedelta(seconds=1),
    )
    monkeypatch.setattr(
        'smartfood.services.outbound_message_service.requests.post',
        lambda *_args, **_kwargs: Response(401, {
            'ok': False,
            'error_code': 401,
            'description': 'Unauthorized',
        }),
    )
    OutboundMessageService.process_due(limit=1)
    missing.refresh_from_db()
    assert missing.status == BotOutboundMessage.Status.PENDING
    assert missing.attempts == 0
    assert missing.next_attempt_at > timezone.now()


def test_rate_limit_respects_retry_after(bot_token, customer, monkeypatch):
    from smartfood.models import BotOutboundMessage
    from smartfood.services.outbound_message_service import OutboundMessageService

    message = _message(customer)
    before = timezone.now()
    monkeypatch.setattr(
        'smartfood.services.outbound_message_service.requests.post',
        lambda *_args, **_kwargs: Response(429, {
            'ok': False,
            'error_code': 429,
            'description': 'Too Many Requests',
            'parameters': {'retry_after': 123},
        }),
    )
    OutboundMessageService.process_due(limit=1)
    message.refresh_from_db()
    assert message.status == BotOutboundMessage.Status.PENDING
    assert message.attempts == 1
    assert message.next_attempt_at >= before + timedelta(seconds=122)


def test_missing_frozen_broadcast_image_fails_once_without_retry_storm(
    bot_token,
    customer,
):
    from smartfood.models import BotOutboundMessage
    from smartfood.services.outbound_message_service import OutboundMessageService

    message = _message(customer)
    message.image_url = '/api/smartfood/media/broadcasts/' + ('a' * 32) + '.jpg'
    message.save(update_fields=['image_url', 'updated_at'])

    result = OutboundMessageService.process_due(limit=1)
    message.refresh_from_db()
    assert result['failed'] == 1
    assert result['retrying'] == 0
    assert message.status == BotOutboundMessage.Status.FAILED
    assert message.attempts == 1
    assert 'unavailable' in message.last_error


def test_permanent_chat_failure_suppresses_and_success_restores_reachability(
    bot_token,
    customer,
    monkeypatch,
):
    from smartfood.models import BotOutboundMessage
    from smartfood.services.outbound_message_service import OutboundMessageService

    failed = _message(customer)
    monkeypatch.setattr(
        'smartfood.services.outbound_message_service.requests.post',
        lambda *_args, **_kwargs: Response(403, {
            'ok': False,
            'error_code': 403,
            'description': 'Forbidden: bot was blocked by the user',
        }),
    )
    result = OutboundMessageService.process_due(limit=1)
    failed.refresh_from_db()
    customer.refresh_from_db()
    assert result['failed'] == 1
    assert failed.status == BotOutboundMessage.Status.FAILED
    assert customer.telegram_reachable is False

    from smartfood.models import BotOrder
    order = BotOrder.objects.create(
        customer=customer,
        order_type=BotOrder.OrderType.PICKUP,
        phone_number=customer.phone_number,
    )
    recovered = _message(
        customer,
        text='Operational recovery',
        bot_order=order,
        event_key='recovery',
    )
    monkeypatch.setattr(
        'smartfood.services.outbound_message_service.requests.post',
        lambda *_args, **_kwargs: Response(200, {
            'ok': True,
            'result': {'message_id': 91},
        }),
    )
    OutboundMessageService.process_due(limit=1)
    recovered.refresh_from_db()
    customer.refresh_from_db()
    assert recovered.status == BotOutboundMessage.Status.SENT
    assert customer.telegram_reachable is True


def test_order_status_retry_blocks_newer_status_until_older_one_sends(
    bot_token,
    customer,
    monkeypatch,
):
    from smartfood.models import BotOrder, BotOutboundMessage
    from smartfood.services.outbound_message_service import OutboundMessageService

    order = BotOrder.objects.create(
        customer=customer,
        order_type=BotOrder.OrderType.PICKUP,
        phone_number=customer.phone_number,
    )
    older = _message(customer, text='preparing', bot_order=order, event_key='preparing')
    newer = _message(customer, text='ready', bot_order=order, event_key='ready')
    calls = []

    def flaky_post(_url, *, data, files, timeout):
        calls.append(data['text'])
        if len(calls) == 1:
            raise requests.ConnectionError('temporary outage')
        return Response(200, {'ok': True, 'result': {'message_id': len(calls)}})

    monkeypatch.setattr(
        'smartfood.services.outbound_message_service.requests.post',
        flaky_post,
    )
    first = OutboundMessageService.process_due(limit=10)
    older.refresh_from_db()
    newer.refresh_from_db()
    assert first['claimed'] == 1
    assert older.status == BotOutboundMessage.Status.PENDING
    assert newer.status == BotOutboundMessage.Status.PENDING
    assert newer.attempts == 0

    BotOutboundMessage.objects.filter(id=older.id).update(
        next_attempt_at=timezone.now() - timedelta(seconds=1),
    )
    second = OutboundMessageService.process_due(limit=10)
    older.refresh_from_db()
    newer.refresh_from_db()
    assert second['sent'] == 2
    assert older.status == newer.status == BotOutboundMessage.Status.SENT
    assert calls == ['preparing', 'preparing', 'ready']


def test_order_status_has_priority_over_an_older_broadcast(
    bot_token,
    customer,
    monkeypatch,
):
    from smartfood.models import BotOrder, BotOutboundMessage
    from smartfood.services.outbound_message_service import OutboundMessageService

    broadcast = _message(customer, text='marketing')
    order = BotOrder.objects.create(
        customer=customer,
        order_type=BotOrder.OrderType.PICKUP,
        phone_number=customer.phone_number,
    )
    status = _message(
        customer,
        text='order ready',
        bot_order=order,
        event_key='ready',
    )
    sent = []

    def fake_post(_url, *, data, files, timeout):
        sent.append(data['text'])
        return Response(200, {'ok': True, 'result': {'message_id': 1}})

    monkeypatch.setattr(
        'smartfood.services.outbound_message_service.requests.post',
        fake_post,
    )
    result = OutboundMessageService.process_due(limit=1)
    broadcast.refresh_from_db()
    status.refresh_from_db()
    assert result['sent'] == 1
    assert status.status == BotOutboundMessage.Status.SENT
    assert broadcast.status == BotOutboundMessage.Status.PENDING
    assert sent == ['order ready']
