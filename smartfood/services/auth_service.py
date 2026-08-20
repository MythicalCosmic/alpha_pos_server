"""Telegram initData -> Customer upsert -> CustomerSession bearer token.

Mirrors the staff auth token shape: a 32-byte hex token is returned to the
client and only its SHA-256 digest is stored (CustomerSession.payload).
"""
import logging
import secrets
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.services.phone import is_canonical_uz_phone, normalize_uz_phone
from smartfood.models import Customer, CustomerSession
from smartfood.repositories import CustomerSessionRepository
from smartfood.security import verify_init_data, _auth_ttl
from smartfood.serializers import customer_dict

logger = logging.getLogger(__name__)


def _norm_lang(code):
    c = (code or '').lower()[:2]
    return c if c in ('uz', 'ru', 'en') else 'uz'


def _link_base_customer(sf_customer, branch_id=None):
    """Converge this Telegram customer onto the unified master base.Customer
    (keyed by phone, then telegram_id). This is what lets a phone-matched walk-in's
    in-store orders + loyalty show up for this Telegram account. Authentication
    does not yet know which restaurant will receive an order, so without an
    explicit branch this only links an existing row and does not create a
    cloud-owned FK parent. Best-effort."""
    try:
        # The auth/profile callers already own an outer atomic block. Keep this
        # best-effort cross-model write inside a savepoint and catch outside the
        # context so a database error cannot poison the caller's transaction.
        with transaction.atomic():
            from base.models import Customer as BaseCustomer
            customer, _ = BaseCustomer.resolve(
                phone=sf_customer.phone_number or None,
                telegram_id=sf_customer.telegram_id,
                name=sf_customer.name,
                branch_id=branch_id,
                create=bool(branch_id),
                adopt_node_owned=bool(branch_id),
            )
            return customer
    except Exception:  # noqa: BLE001 — never block auth on the cross-model link
        logger.exception('smartfood: base.Customer link failed for tg=%s',
                         sf_customer.telegram_id)
        return None


class CustomerAuthService:
    @staticmethod
    @transaction.atomic
    def login_with_init_data(init_data, user_agent='', ip=''):
        tg = verify_init_data(init_data)
        if not tg:
            return ServiceResponse.unauthorized('Invalid Telegram init data')

        customer, created = Customer.objects.select_for_update().get_or_create(
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
            changed_fields = set()
            # A confirmed delivery identity belongs to the customer, not to
            # subsequent Telegram profile edits. Telegram names remain useful
            # as checkout prefill only until the customer confirms the profile.
            profile_fields = ('username', 'photo_url')
            if customer.profile_confirmed_at is None:
                profile_fields = ('first_name', 'last_name', *profile_fields)
            for field in profile_fields:
                val = tg.get(field, '') or ''
                if val and getattr(customer, field) != val:
                    setattr(customer, field, val)
                    changed_fields.add(field)
            if not customer.language_overridden:
                telegram_language = _norm_lang(tg.get('language_code'))
                if customer.language != telegram_language:
                    customer.language = telegram_language
                    changed_fields.add('language')
            if changed_fields:
                # Limit the UPDATE to Telegram-owned fields. A concurrent
                # checkout confirmation must never be overwritten by stale
                # identity values read above.
                customer.save(update_fields=[*changed_fields, 'updated_at'])
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
    @transaction.atomic
    def update_profile(customer, *, first_name=None, last_name=None, name=None,
                       phone=None, language=None, confirm=None,
                       broadcast_opted_in=None):
        """Update identity and explicitly confirm the order-ready profile.

        Name/phone edits invalidate an earlier confirmation unless the same
        request explicitly confirms the resulting values.  This keeps the
        server-side checkout gate authoritative even for older clients.
        """
        customer = Customer.objects.select_for_update().get(id=customer.id)
        errors = {}

        if name is not None and first_name is None and last_name is None:
            parts = str(name).strip().split(maxsplit=1)
            first_name = parts[0] if parts else ''
            last_name = parts[1] if len(parts) > 1 else ''

        identity_changed = False
        for field, value, label in (
            ('first_name', first_name, 'First name'),
            ('last_name', last_name, 'Last name'),
        ):
            if value is None:
                continue
            cleaned = str(value).strip()
            if not cleaned:
                errors[field] = f'{label} is required.'
            elif len(cleaned) > 64:
                errors[field] = f'{label} must be 64 characters or fewer.'
            elif getattr(customer, field) != cleaned:
                setattr(customer, field, cleaned)
                identity_changed = True

        phone_changed = False
        if phone is not None:
            normalized_phone = normalize_uz_phone(phone)
            if not is_canonical_uz_phone(normalized_phone):
                errors['phone'] = 'Enter a valid Uzbekistan phone number.'
            elif customer.phone_number != normalized_phone:
                customer.phone_number = normalized_phone
                phone_changed = True

        if language is not None:
            raw_language = str(language).lower().strip()
            if raw_language not in ('uz', 'ru', 'en'):
                errors['language'] = 'Language must be uz, ru, or en.'
            else:
                customer.language = raw_language
                customer.language_overridden = True

        if confirm is not None and not isinstance(confirm, bool):
            errors['confirm'] = 'confirm must be a boolean.'
        if broadcast_opted_in is not None and not isinstance(broadcast_opted_in, bool):
            errors['broadcast_opted_in'] = 'broadcast_opted_in must be a boolean.'

        if errors:
            return ServiceResponse.validation_error(
                errors,
                'Please correct the account details.',
            )

        if identity_changed or phone_changed:
            customer.profile_confirmed_at = None

        if broadcast_opted_in is not None:
            customer.broadcast_opted_in = broadcast_opted_in

        if confirm is True:
            confirmation_errors = {}
            if not (customer.first_name or '').strip():
                confirmation_errors['first_name'] = 'First name is required.'
            if not (customer.last_name or '').strip():
                confirmation_errors['last_name'] = 'Last name is required.'
            if not is_canonical_uz_phone(customer.phone_number):
                confirmation_errors['phone'] = 'Enter a valid Uzbekistan phone number.'
            if confirmation_errors:
                return ServiceResponse.validation_error(
                    confirmation_errors,
                    'Complete the account before confirming it.',
                )
            customer.profile_confirmed_at = timezone.now()

        customer.save()
        if phone_changed:
            _link_base_customer(customer)
        return ServiceResponse.success(data=customer_dict(customer))
