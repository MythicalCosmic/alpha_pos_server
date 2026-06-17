"""Telegram initData -> Customer upsert -> CustomerSession bearer token.

Mirrors the staff auth token shape: a 32-byte hex token is returned to the
client and only its SHA-256 digest is stored (CustomerSession.payload).
"""
import logging
import secrets
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from base.helpers.response import ServiceResponse
from smartfood.models import Customer, CustomerSession
from smartfood.repositories import CustomerSessionRepository
from smartfood.security import verify_init_data, _auth_ttl
from smartfood.serializers import customer_dict

logger = logging.getLogger(__name__)


def _norm_lang(code):
    c = (code or '').lower()[:2]
    return c if c in ('uz', 'ru', 'en') else 'uz'


def _link_base_customer(sf_customer):
    """Converge this Telegram customer onto the unified master base.Customer
    (keyed by phone, then telegram_id). This is what lets a phone-matched walk-in's
    in-store orders + loyalty show up for this Telegram account. Best-effort."""
    try:
        from base.models import Customer as BaseCustomer
        BaseCustomer.resolve(
            phone=sf_customer.phone_number or None,
            telegram_id=sf_customer.telegram_id,
            name=sf_customer.name,
        )
    except Exception:  # noqa: BLE001 — never block auth on the cross-model link
        logger.exception('smartfood: base.Customer link failed for tg=%s',
                         sf_customer.telegram_id)


class CustomerAuthService:
    @staticmethod
    def login_with_init_data(init_data, user_agent='', ip=''):
        tg = verify_init_data(init_data)
        if not tg:
            return ServiceResponse.unauthorized('Invalid Telegram init data')

        customer, created = Customer.objects.get_or_create(
            telegram_id=tg.get('id'),
            defaults={
                'first_name': tg.get('first_name', '') or '',
                'last_name': tg.get('last_name', '') or '',
                'username': tg.get('username', '') or '',
                'language': _norm_lang(tg.get('language_code')),
                'photo_url': tg.get('photo_url', '') or '',
            },
        )
        if not created:
            changed = False
            for field in ('first_name', 'last_name', 'username', 'photo_url'):
                val = tg.get(field, '') or ''
                if val and getattr(customer, field) != val:
                    setattr(customer, field, val)
                    changed = True
            if changed:
                customer.save()
        if customer.is_blocked:
            return ServiceResponse.forbidden('Account blocked')

        # Link to the unified master client (by telegram_id now; by phone once set).
        _link_base_customer(customer)

        raw = secrets.token_hex(32)
        ttl = _auth_ttl()
        CustomerSession.objects.create(
            customer=customer,
            payload=CustomerSessionRepository.hash_token(raw),
            user_agent=(user_agent or '')[:256],
            ip_address=(ip or '')[:45],
            expires_at=timezone.now() + timedelta(seconds=ttl),
        )
        return ServiceResponse.success(data={
            'token': raw,
            'token_type': 'Bearer',
            'expires_in': ttl,
            'is_new': created,
            'customer': customer_dict(customer),
        })

    @staticmethod
    def logout(session):
        if session is not None:
            cache.delete('smartfood:session:' + session.payload)
            CustomerSession.objects.filter(id=session.id).delete()
        return ServiceResponse.success(message='Logged out')

    @staticmethod
    def update_profile(customer, name=None, phone=None, language=None):
        if name is not None:
            parts = str(name).strip().split(' ', 1)
            customer.first_name = parts[0][:64]
            customer.last_name = (parts[1] if len(parts) > 1 else '')[:64]
        if phone is not None:
            customer.phone_number = str(phone)[:20]
        if language is not None:
            customer.language = _norm_lang(language)
        customer.save()
        # A phone entered here is the cross-channel key — converge the master client.
        if phone is not None:
            _link_base_customer(customer)
        return ServiceResponse.success(data=customer_dict(customer))
