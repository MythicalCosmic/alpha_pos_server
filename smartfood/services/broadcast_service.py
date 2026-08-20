"""Draft, validate, and atomically fan out manager Telegram broadcasts."""

from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from base.helpers.response import ServiceResponse
from smartfood.credentials import customer_bot_token
from smartfood.models import BotBroadcast, BotOutboundMessage, Customer
from smartfood.serializers import admin_broadcast_dict
from smartfood.services.media_service import (
    delete_managed_media,
    managed_media_storage_name,
)


def _pagination(page, per_page):
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = min(100, max(1, int(per_page or 20)))
    except (TypeError, ValueError):
        per_page = 20
    return page, per_page


def _clean_payload(payload, *, existing=None):
    errors = {}
    values = {}
    if 'title' in payload or existing is None:
        title = str(payload.get('title') or '').strip()
        if not title:
            errors['title'] = 'Give this draft an internal title.'
        elif len(title) > 120:
            errors['title'] = 'Title must be 120 characters or fewer.'
        else:
            values['title'] = title

    messages = payload.get('messages')
    if messages is not None and not isinstance(messages, dict):
        errors['messages'] = 'messages must be an object.'
        messages = {}
    messages = messages or {}
    for language in ('uz', 'ru', 'en'):
        direct_key = f'text_{language}'
        supplied = language in messages or direct_key in payload
        if not supplied:
            continue
        raw = messages.get(language, payload.get(direct_key, ''))
        if not isinstance(raw, str):
            errors[f'messages.{language}'] = 'Message must be text.'
            continue
        text = raw.strip()
        if len(text) > 4096:
            errors[f'messages.{language}'] = 'Message must be 4096 characters or fewer.'
        else:
            values[direct_key] = text
    return values, errors


def _send_errors(broadcast):
    errors = {}
    if not broadcast.text_uz.strip():
        errors['messages.uz'] = 'Write the Uzbek message before sending.'
    limit = 1024 if broadcast.image_url else 4096
    for language in ('uz', 'ru', 'en'):
        text = getattr(broadcast, f'text_{language}') or ''
        if len(text) > limit:
            errors[f'messages.{language}'] = (
                f'Photo captions must be {limit} characters or fewer.'
                if broadcast.image_url else
                f'Message must be {limit} characters or fewer.'
            )
    return errors


class BroadcastService:
    @staticmethod
    def list(*, status='', q='', page=1, per_page=20):
        page, per_page = _pagination(page, per_page)
        qs = BotBroadcast.objects.select_related('created_by')
        if status in dict(BotBroadcast.Status.choices):
            qs = qs.filter(status=status)
        query = str(q or '').strip()
        if query:
            qs = qs.filter(title__icontains=query)
        total = qs.count()
        offset = (page - 1) * per_page
        items = [admin_broadcast_dict(row) for row in qs[offset:offset + per_page]]
        return ServiceResponse.success(data={
            'items': items,
            'audience': {
                'eligible': Customer.objects.filter(
                    is_blocked=False,
                    broadcast_opted_in=True,
                    telegram_reachable=True,
                ).count(),
            },
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': (total + per_page - 1) // per_page,
            },
        })

    @staticmethod
    def get(broadcast_id):
        broadcast = (
            BotBroadcast.objects.select_related('created_by')
            .filter(id=broadcast_id)
            .first()
        )
        if broadcast is None:
            return ServiceResponse.not_found('Broadcast not found')
        data = admin_broadcast_dict(broadcast)
        data['current_audience'] = Customer.objects.filter(
            is_blocked=False,
            broadcast_opted_in=True,
            telegram_reachable=True,
        ).count()
        return ServiceResponse.success(data=data)

    @staticmethod
    @transaction.atomic
    def create(payload, actor=None):
        values, errors = _clean_payload(payload)
        if errors:
            return ServiceResponse.validation_error(errors)
        broadcast = BotBroadcast.objects.create(
            **values,
            created_by=actor,
            updated_by=actor,
        )
        return ServiceResponse.created(data=admin_broadcast_dict(broadcast))

    @staticmethod
    @transaction.atomic
    def update(broadcast_id, payload, actor=None):
        broadcast = (
            BotBroadcast.objects.select_for_update()
            .select_related('created_by')
            .filter(id=broadcast_id)
            .first()
        )
        if broadcast is None:
            return ServiceResponse.not_found('Broadcast not found')
        if broadcast.status != BotBroadcast.Status.DRAFT:
            return ({
                'success': False,
                'code': 'broadcast_locked',
                'message': 'A queued or sent broadcast cannot be edited.',
            }, 409)
        values, errors = _clean_payload(payload, existing=broadcast)
        if errors:
            return ServiceResponse.validation_error(errors)
        for field, value in values.items():
            setattr(broadcast, field, value)
        broadcast.updated_by = actor
        broadcast.save()
        return ServiceResponse.success(data=admin_broadcast_dict(broadcast))

    @staticmethod
    @transaction.atomic
    def delete(broadcast_id):
        broadcast = (
            BotBroadcast.objects.select_for_update()
            .filter(id=broadcast_id)
            .first()
        )
        if broadcast is None:
            return ServiceResponse.not_found('Broadcast not found')
        if broadcast.status != BotBroadcast.Status.DRAFT:
            return ({
                'success': False,
                'code': 'broadcast_locked',
                'message': 'Only drafts can be deleted.',
            }, 409)
        image_url = broadcast.image_url
        broadcast.delete()
        if image_url:
            transaction.on_commit(
                lambda: delete_managed_media(image_url, 'broadcasts'),
                robust=True,
            )
        return ServiceResponse.success(message='Draft deleted')

    @staticmethod
    @transaction.atomic
    def send(broadcast_id, actor=None, expected_updated_at=None):
        broadcast = (
            BotBroadcast.objects.select_for_update()
            .select_related('created_by')
            .filter(id=broadcast_id)
            .first()
        )
        if broadcast is None:
            return ServiceResponse.not_found('Broadcast not found')
        if broadcast.status != BotBroadcast.Status.DRAFT:
            return ({
                'success': False,
                'code': 'broadcast_locked',
                'message': 'This broadcast has already left drafts.',
            }, 409)
        expected_version = str(expected_updated_at or '').strip()
        if not expected_version:
            return ServiceResponse.validation_error({
                'expected_updated_at': 'Review the latest draft before sending.',
            })
        if expected_version != broadcast.updated_at.isoformat():
            return ({
                'success': False,
                'code': 'broadcast_changed',
                'message': (
                    'This draft changed after it was reviewed. '
                    'Reload it and confirm the exact message again.'
                ),
                'data': admin_broadcast_dict(broadcast),
            }, 409)
        errors = _send_errors(broadcast)
        if errors:
            return ServiceResponse.validation_error(
                errors,
                'Complete the broadcast before sending.',
            )
        if not customer_bot_token():
            return ({
                'success': False,
                'code': 'bot_token_missing',
                'message': 'Configure the Telegram bot token before sending.',
            }, 409)
        if broadcast.image_url:
            storage_name = managed_media_storage_name(
                broadcast.image_url,
                'broadcasts',
            )
            try:
                image_exists = bool(
                    storage_name and default_storage.exists(storage_name)
                )
            except OSError:
                return ({
                    'success': False,
                    'code': 'broadcast_media_unavailable',
                    'message': 'Image storage is temporarily unavailable. Try again.',
                }, 503)
            if not image_exists:
                return ({
                    'success': False,
                    'code': 'broadcast_image_missing',
                    'message': 'The attached photo is missing. Remove it or upload it again.',
                }, 409)

        recipients = Customer.objects.filter(
            is_blocked=False,
            broadcast_opted_in=True,
            telegram_reachable=True,
        ).only(
            'id', 'telegram_id', 'language',
        )
        messages = []
        recipient_count = 0
        for customer in recipients.iterator(chunk_size=1000):
            recipient_count += 1
            language = customer.language if customer.language in ('uz', 'ru', 'en') else 'uz'
            text = getattr(broadcast, f'text_{language}') or broadcast.text_uz
            messages.append(BotOutboundMessage(
                kind=BotOutboundMessage.Kind.BROADCAST,
                customer=customer,
                broadcast=broadcast,
                chat_id=customer.telegram_id,
                language=language,
                text=text,
                image_url=broadcast.image_url,
            ))
            if len(messages) == 1000:
                BotOutboundMessage.objects.bulk_create(messages, batch_size=1000)
                messages = []
        if messages:
            BotOutboundMessage.objects.bulk_create(messages, batch_size=1000)
        if recipient_count == 0:
            return ({
                'success': False,
                'code': 'empty_audience',
                'message': 'There are no eligible Telegram users to message.',
            }, 409)

        now = timezone.now()
        broadcast.status = BotBroadcast.Status.QUEUED
        broadcast.recipient_count = recipient_count
        broadcast.delivered_count = 0
        broadcast.failed_count = 0
        broadcast.skipped_count = 0
        broadcast.queued_at = now
        broadcast.updated_by = actor
        broadcast.save()
        return ServiceResponse.success(
            data=admin_broadcast_dict(broadcast),
            message=f'Broadcast queued for {recipient_count} users.',
        )
