"""Claim, send, retry, and reconcile the durable Telegram message outbox."""

from datetime import timedelta
import logging
import uuid

import requests
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Q, Value, When
from django.utils import timezone

from smartfood.credentials import customer_bot_token
from smartfood.models import BotBroadcast, BotOrder, BotOutboundMessage, Customer
from smartfood.services.media_service import managed_media_storage_name


_API = 'https://api.telegram.org/bot{token}/{method}'
_MAX_ATTEMPTS = 5
_LOCK_TIMEOUT = timedelta(minutes=5)
logger = logging.getLogger(__name__)

_ORDER_MESSAGES = {
    'placed': {
        'uz': '✅ {code} buyurtmangiz yuborildi. Holati o‘zgarganda shu yerda xabar beramiz.',
        'ru': '✅ Заказ {code} отправлен. Мы сообщим здесь, когда его статус изменится.',
        'en': '✅ Your order {code} was sent. We will update you here when its status changes.',
    },
    'dispatched': {
        'uz': '👨‍🍳 {code} buyurtmangiz restoran tomonidan qabul qilindi.',
        'ru': '👨‍🍳 Ресторан принял ваш заказ {code}.',
        'en': '👨‍🍳 The restaurant accepted your order {code}.',
    },
    'preparing': {
        'uz': '🍳 {code} buyurtmangiz tayyorlanmoqda.',
        'ru': '🍳 Ваш заказ {code} готовится.',
        'en': '🍳 Your order {code} is being prepared.',
    },
    'ready': {
        'uz': '🛍 {code} buyurtmangiz tayyor.',
        'ru': '🛍 Ваш заказ {code} готов.',
        'en': '🛍 Your order {code} is ready.',
    },
    'completed': {
        'uz': '🎉 {code} buyurtmangiz yakunlandi. Yoqimli ishtaha!',
        'ru': '🎉 Заказ {code} завершён. Приятного аппетита!',
        'en': '🎉 Your order {code} is complete. Enjoy your meal!',
    },
    'canceled': {
        'uz': 'Buyurtma {code} bekor qilindi.',
        'ru': 'Заказ {code} отменён.',
        'en': 'Order {code} was canceled.',
    },
    'rejected': {
        'uz': 'Kechirasiz, {code} buyurtmangiz qabul qilinmadi.',
        'ru': 'Извините, заказ {code} не был принят.',
        'en': 'Sorry, your order {code} could not be accepted.',
    },
}


def _order_text(order, event, language):
    choices = _ORDER_MESSAGES.get(event)
    if not choices:
        return ''
    text = (choices.get(language) or choices['uz']).format(code=order.code)
    if event == 'rejected' and order.reject_reason:
        text = f'{text}\n{order.reject_reason}'
    return text


def queue_order_status(order_id, event):
    order = (
        BotOrder.objects.select_related('customer')
        .filter(id=order_id)
        .first()
    )
    if order is None or order.customer.is_blocked:
        return False
    language = order.customer.language
    if language not in ('uz', 'ru', 'en'):
        language = 'uz'
    text = _order_text(order, event, language)
    if not text:
        return False
    _, created = BotOutboundMessage.objects.get_or_create(
        bot_order=order,
        event_key=event,
        defaults={
            'kind': BotOutboundMessage.Kind.ORDER_STATUS,
            'customer': order.customer,
            'chat_id': order.customer.telegram_id,
            'language': language,
            'text': text,
        },
    )
    return created


def _claim(limit):
    now = timezone.now()
    stale_before = now - _LOCK_TIMEOUT
    claimed = []
    with transaction.atomic():
        earlier_order_event = BotOutboundMessage.objects.filter(
            bot_order_id=OuterRef('bot_order_id'),
            kind=BotOutboundMessage.Kind.ORDER_STATUS,
            id__lt=OuterRef('id'),
            status__in=(
                BotOutboundMessage.Status.PENDING,
                BotOutboundMessage.Status.PROCESSING,
            ),
        )
        rows = list(
            BotOutboundMessage.objects.select_for_update(skip_locked=True)
            .annotate(has_earlier_order_event=Exists(earlier_order_event))
            .filter(
                Q(status=BotOutboundMessage.Status.PENDING, next_attempt_at__lte=now)
                | Q(status=BotOutboundMessage.Status.PROCESSING, locked_at__lt=stale_before)
            )
            .filter(
                Q(kind=BotOutboundMessage.Kind.BROADCAST)
                | Q(has_earlier_order_event=False)
            )
            .annotate(delivery_priority=Case(
                When(kind=BotOutboundMessage.Kind.ORDER_STATUS, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ))
            .order_by('delivery_priority', 'next_attempt_at', 'id')[:limit]
        )
        for row in rows:
            claim_token = uuid.uuid4()
            row.status = BotOutboundMessage.Status.PROCESSING
            row.attempts += 1
            row.locked_at = now
            row.claim_token = claim_token
            row.save(update_fields=[
                'status', 'attempts', 'locked_at', 'claim_token', 'updated_at',
            ])
            claimed.append((row.id, claim_token))
    return claimed


def _telegram_send(message):
    token = customer_bot_token()
    if not token:
        return False, None, 'Telegram bot token is not configured.', False, 60, False, False

    method = 'sendPhoto' if message.image_url else 'sendMessage'
    data = {'chat_id': str(message.chat_id)}
    files = None
    handle = None
    if message.image_url:
        storage_name = managed_media_storage_name(message.image_url, 'broadcasts')
        if not storage_name or not default_storage.exists(storage_name):
            # Broadcast content is immutable after queueing, so a missing frozen
            # file cannot be repaired by retrying every recipient five times.
            return False, None, 'Broadcast image is unavailable.', True, None, True, False
        handle = default_storage.open(storage_name, 'rb')
        files = {'photo': (storage_name.rsplit('/', 1)[-1], handle)}
        data['caption'] = message.text
    else:
        data['text'] = message.text

    try:
        response = requests.post(
            _API.format(token=token, method=method),
            data=data,
            files=files,
            timeout=20,
        )
    except requests.RequestException:
        return False, None, 'Telegram request failed.', False, None, True, False
    finally:
        if handle is not None:
            handle.close()
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    if response.status_code >= 400:
        description = (
            str(payload.get('description') or '')
            if isinstance(payload, dict) else ''
        )
        error = description or f'Telegram returned HTTP {response.status_code}.'
        retry_after = None
        if response.status_code == 429 and isinstance(payload, dict):
            raw_retry = (payload.get('parameters') or {}).get('retry_after')
            try:
                retry_after = min(86400, max(1, int(raw_retry)))
            except (TypeError, ValueError):
                retry_after = 60
        configuration_error = response.status_code in (401, 404)
        permanent = response.status_code in (400, 403)
        lowered = error.lower()
        suppress_customer = response.status_code == 403 or any(marker in lowered for marker in (
            'bot was blocked', 'chat not found', 'user is deactivated', 'bot was kicked',
        ))
        return (
            False, None, error[:450], permanent,
            60 if configuration_error else retry_after,
            not configuration_error,
            suppress_customer,
        )
    if not isinstance(payload, dict):
        return False, None, 'Telegram returned an invalid response.', False, None, True, False
    if not isinstance(payload, dict) or payload.get('ok') is not True:
        description = str(payload.get('description') or 'Telegram rejected the message.')
        error_code = payload.get('error_code')
        configuration_error = error_code in (401, 404)
        permanent = error_code in (400, 403)
        raw_retry = (payload.get('parameters') or {}).get('retry_after')
        try:
            retry_after = min(86400, max(1, int(raw_retry)))
        except (TypeError, ValueError):
            retry_after = 60 if error_code == 429 else None
        lowered = description.lower()
        suppress_customer = error_code == 403 or any(marker in lowered for marker in (
            'bot was blocked', 'chat not found', 'user is deactivated', 'bot was kicked',
        ))
        return (
            False, None, description[:450], permanent,
            60 if configuration_error else retry_after,
            not configuration_error,
            suppress_customer,
        )
    telegram_id = (payload.get('result') or {}).get('message_id')
    return True, telegram_id, '', False, None, True, False


def _refresh_broadcast(broadcast_id):
    if not broadcast_id:
        return
    with transaction.atomic():
        broadcast = (
            BotBroadcast.objects.select_for_update()
            .filter(id=broadcast_id)
            .first()
        )
        if broadcast is None:
            return
        counts = {
            row['status']: row['count']
            for row in broadcast.messages.values('status').annotate(count=Count('id'))
        }
        delivered = counts.get(BotOutboundMessage.Status.SENT, 0)
        failed = counts.get(BotOutboundMessage.Status.FAILED, 0)
        skipped = counts.get(BotOutboundMessage.Status.SKIPPED, 0)
        terminal = delivered + failed + skipped
        now = timezone.now()
        broadcast.delivered_count = delivered
        broadcast.failed_count = failed
        broadcast.skipped_count = skipped
        if terminal >= broadcast.recipient_count:
            broadcast.finished_at = now
            if delivered == broadcast.recipient_count:
                broadcast.status = BotBroadcast.Status.SENT
            elif delivered or skipped:
                broadcast.status = BotBroadcast.Status.PARTIAL
            else:
                broadcast.status = BotBroadcast.Status.FAILED
        else:
            broadcast.status = BotBroadcast.Status.SENDING
            if broadcast.started_at is None:
                broadcast.started_at = now
        broadcast.save(update_fields=[
            'status', 'delivered_count', 'failed_count', 'skipped_count', 'started_at',
            'finished_at', 'updated_at',
        ])


def _finish(message_id, claim_token, success, telegram_id=None, error='', *,
            permanent=False, retry_after=None, consume_attempt=True,
            suppress_customer=False, skipped=False):
    broadcast_id = None
    terminal_failure = False
    with transaction.atomic():
        message = (
            BotOutboundMessage.objects.select_for_update()
            .filter(
                id=message_id,
                status=BotOutboundMessage.Status.PROCESSING,
                claim_token=claim_token,
            )
            .first()
        )
        if message is None:
            return False, False
        broadcast_id = message.broadcast_id
        if suppress_customer:
            Customer.objects.filter(id=message.customer_id).update(
                telegram_reachable=False,
            )
        elif success:
            Customer.objects.filter(id=message.customer_id).update(
                telegram_reachable=True,
            )
        message.locked_at = None
        message.claim_token = None
        if not consume_attempt:
            message.attempts = max(0, message.attempts - 1)
        if skipped:
            message.status = BotOutboundMessage.Status.SKIPPED
            message.last_error = error[:500]
        elif success:
            message.status = BotOutboundMessage.Status.SENT
            message.sent_at = timezone.now()
            message.telegram_message_id = telegram_id
            message.last_error = ''
        elif permanent or message.attempts >= _MAX_ATTEMPTS:
            message.status = BotOutboundMessage.Status.FAILED
            message.last_error = error[:500]
            terminal_failure = True
        else:
            delay = (
                retry_after
                if retry_after is not None else
                min(900, 30 * (2 ** max(0, message.attempts - 1)))
            )
            message.status = BotOutboundMessage.Status.PENDING
            message.next_attempt_at = timezone.now() + timedelta(seconds=delay)
            message.last_error = error[:500]
        message.save(update_fields=[
            'status', 'attempts', 'locked_at', 'claim_token', 'sent_at',
            'telegram_message_id', 'last_error', 'next_attempt_at', 'updated_at',
        ])
    _refresh_broadcast(broadcast_id)
    return True, terminal_failure


class OutboundMessageService:
    @staticmethod
    def process_due(limit=50):
        try:
            limit = min(500, max(1, int(limit)))
        except (TypeError, ValueError):
            limit = 50
        result = {
            'claimed': 0, 'sent': 0, 'failed': 0, 'retrying': 0, 'skipped': 0,
        }
        # Claim immediately before each network call. This keeps a slow send
        # from expiring the lease on later rows that this worker has not yet
        # started and prevents a crash from stranding a whole claimed batch.
        for _ in range(limit):
            claimed = _claim(1)
            if not claimed:
                break
            message_id, claim_token = claimed[0]
            result['claimed'] += 1
            message = BotOutboundMessage.objects.filter(id=message_id).first()
            if message is None:
                continue
            if message.kind == BotOutboundMessage.Kind.BROADCAST:
                recipient = Customer.objects.only(
                    'is_blocked', 'broadcast_opted_in', 'telegram_reachable',
                ).get(id=message.customer_id)
                if recipient.is_blocked:
                    skip_reason = 'Customer account was blocked before delivery.'
                elif not recipient.broadcast_opted_in:
                    skip_reason = 'Customer opted out before delivery.'
                elif not recipient.telegram_reachable:
                    skip_reason = 'Telegram chat became unreachable before delivery.'
                else:
                    skip_reason = ''
                if skip_reason:
                    finished, _ = _finish(
                        message_id,
                        claim_token,
                        False,
                        error=skip_reason,
                        skipped=True,
                    )
                    if finished:
                        result['skipped'] += 1
                    continue
            try:
                (success, telegram_id, error, permanent, retry_after,
                 consume_attempt, suppress_customer) = _telegram_send(message)
            except Exception:  # noqa: BLE001 — isolate each durable outbox row
                logger.exception('Unexpected Telegram send failure for outbox=%s', message_id)
                success, telegram_id = False, None
                error, permanent = 'Unexpected Telegram delivery failure.', False
                retry_after, consume_attempt, suppress_customer = None, True, False
            finished, terminal_failure = _finish(
                message_id,
                claim_token,
                success,
                telegram_id=telegram_id,
                error=error,
                permanent=permanent,
                retry_after=retry_after,
                consume_attempt=consume_attempt,
                suppress_customer=suppress_customer,
            )
            if not finished:
                continue
            if success:
                result['sent'] += 1
            elif terminal_failure:
                result['failed'] += 1
            else:
                result['retrying'] += 1
        return result
