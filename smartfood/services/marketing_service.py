"""Bot-local banners and loyalty reward catalog management.

The service keeps the customer surface and operator studio on the same rules:
only scheduled, active banners are public; rewards cannot be activated with an
invalid points cost or kind-specific configuration.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from base.helpers.response import ServiceResponse
from smartfood.models import BotBanner, Redemption, Reward
from smartfood.serializers import admin_banner_dict, admin_reward_dict, banner_dict
from smartfood.services.catalog_service import (
    customer_visible_product_rows,
    is_product_customer_visible,
)


_LANGS = ('uz', 'ru', 'en')
_BANNER_TEXT_LIMITS = {'title': 140, 'subtitle': 240}
_REWARD_TEXT_LIMITS = {'name': 120, 'desc': None}


def _text(value, limit=None):
    if value is None:
        return ''
    result = str(value).strip()
    if limit is not None and len(result) > limit:
        return None
    return result


def _integer(value, *, minimum=None, nullable=False):
    if nullable and (value is None or value == ''):
        return None, None
    if isinstance(value, bool):
        return None, 'Enter a whole number.'
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None, 'Enter a whole number.'
    if str(value).strip() not in {str(result), f'+{result}'}:
        return None, 'Enter a whole number.'
    if minimum is not None and result < minimum:
        return None, f'Use {minimum} or more.'
    return result, None


def _decimal(value, *, minimum=Decimal('0')):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None, 'Enter a valid amount.'
    if not result.is_finite() or result < minimum:
        return None, f'Use {minimum} or more.'
    return result, None


def _boolean(value):
    if isinstance(value, bool):
        return value, None
    return None, 'Use true or false.'


def _datetime(value):
    if value is None or value == '':
        return None, None
    if not isinstance(value, str):
        return None, 'Enter a valid ISO date and time.'
    result = parse_datetime(value.strip())
    if result is None:
        return None, 'Enter a valid ISO date and time.'
    if timezone.is_naive(result):
        result = timezone.make_aware(result, timezone.get_current_timezone())
    return result, None


def _apply_localized(obj, values, limits):
    errors = {}
    for prefix, limit in limits.items():
        for lang in _LANGS:
            field = f'{prefix}_{lang}'
            if field not in values:
                continue
            value = _text(values[field], limit)
            if value is None:
                errors[field] = f'Use {limit} characters or fewer.'
            else:
                setattr(obj, field, value)
    return errors


def _has_localized(obj, prefix):
    return any((getattr(obj, f'{prefix}_{lang}', '') or '').strip() for lang in _LANGS)


class BannerService:
    @staticmethod
    def public(lang='uz'):
        language = lang if lang in _LANGS else 'uz'
        now = timezone.now()
        visible_product_ids = customer_visible_product_rows().order_by().values('product_id')
        rows = (
            BotBanner.objects.filter(is_active=True)
            .exclude(image_url='')
            .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
            .filter(
                ~Q(action_type=BotBanner.Action.PRODUCT)
                | Q(product_id__in=visible_product_ids)
            )
            .order_by('sort_order', 'id')
        )
        return ServiceResponse.success(data={
            'items': [banner_dict(row, language) for row in rows],
        })

    @staticmethod
    def list_admin():
        rows = BotBanner.objects.select_related('product').order_by('sort_order', 'id')
        now = timezone.now()
        visible_product_ids = set(
            customer_visible_product_rows().values_list('product_id', flat=True)
        )
        visible_destination = (
            ~Q(action_type=BotBanner.Action.PRODUCT)
            | Q(product_id__in=visible_product_ids)
        )
        return ServiceResponse.success(data={
            'items': [
                admin_banner_dict(
                    row,
                    destination_available=(
                        row.action_type != BotBanner.Action.PRODUCT
                        or row.product_id in visible_product_ids
                    ),
                )
                for row in rows
            ],
            'summary': {
                'total': rows.count(),
                'live': rows.filter(is_active=True)
                .exclude(image_url='')
                .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
                .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
                .filter(visible_destination)
                .count(),
                'drafts': rows.filter(is_active=False).count(),
            },
        })

    @staticmethod
    def _apply(banner, values):
        errors = _apply_localized(banner, values, _BANNER_TEXT_LIMITS)

        if 'action_type' in values:
            action = str(values['action_type'] or '').upper()
            if action not in BotBanner.Action.values:
                errors['action_type'] = 'Choose a supported banner action.'
            else:
                banner.action_type = action

        if 'product_id' in values:
            raw_product = values['product_id']
            if raw_product is None or raw_product == '':
                banner.product_id = None
            else:
                product_id, error = _integer(raw_product, minimum=1)
                if error:
                    errors['product_id'] = error
                else:
                    banner.product_id = product_id

        for field in ('starts_at', 'ends_at'):
            if field in values:
                parsed, error = _datetime(values[field])
                if error:
                    errors[field] = error
                else:
                    setattr(banner, field, parsed)

        if 'sort_order' in values:
            parsed, error = _integer(values['sort_order'])
            if error:
                errors['sort_order'] = error
            else:
                banner.sort_order = parsed

        if 'is_active' in values:
            parsed, error = _boolean(values['is_active'])
            if error:
                errors['is_active'] = error
            else:
                banner.is_active = parsed

        if banner.action_type != BotBanner.Action.PRODUCT:
            banner.product_id = None
        elif banner.product_id is None:
            errors['product_id'] = 'Choose the product this banner should open.'
        elif banner.is_active and not is_product_customer_visible(banner.product_id):
            errors['product_id'] = 'Choose a product currently available in the bot.'

        if banner.starts_at and banner.ends_at and banner.ends_at <= banner.starts_at:
            errors['ends_at'] = 'End time must be after the start time.'
        if not _has_localized(banner, 'title'):
            errors['title_uz'] = 'Add a title in at least one language.'
        if banner.is_active and not banner.image_url:
            errors['image'] = 'Upload the banner image before making it live.'
        return errors

    @staticmethod
    @transaction.atomic
    def create(values):
        banner = BotBanner()
        errors = BannerService._apply(banner, values)
        if errors:
            return ServiceResponse.validation_error(errors)
        banner.save()
        return ServiceResponse.created(
            data=admin_banner_dict(banner),
            message='Banner created',
        )

    @staticmethod
    @transaction.atomic
    def update(banner_id, values):
        banner = BotBanner.objects.select_for_update().filter(id=banner_id).first()
        if banner is None:
            return ServiceResponse.not_found('Banner not found')
        errors = BannerService._apply(banner, values)
        if errors:
            return ServiceResponse.validation_error(errors)
        banner.save()
        return ServiceResponse.success(
            data=admin_banner_dict(banner),
            message='Banner updated',
        )

    @staticmethod
    @transaction.atomic
    def delete(banner_id):
        banner = BotBanner.objects.select_for_update().filter(id=banner_id).first()
        if banner is None:
            return ServiceResponse.not_found('Banner not found')
        image_url = banner.image_url
        banner.delete()
        from smartfood.services.media_service import delete_managed_media
        delete_managed_media(image_url, 'banners')
        return ServiceResponse.success(data={'id': banner_id}, message='Banner deleted')


class RewardCatalogService:
    @staticmethod
    def _customer_available(reward, visible_product_ids=None):
        """Mirror the public reward catalog's global visibility rules.

        Per-customer affordability and redemption limits are intentionally not
        included: this flag answers whether *any* customer can currently see
        the reward, which is what the operator summary needs to report.
        """
        if not reward.is_active or reward.points_cost <= 0:
            return False
        if reward.stock is not None and reward.stock <= 0:
            return False
        if reward.kind == Reward.Kind.FREE_PRODUCT:
            if visible_product_ids is None:
                return is_product_customer_visible(reward.product_id)
            return reward.product_id in visible_product_ids
        if reward.kind == Reward.Kind.DISCOUNT:
            return reward.discount_amount > 0
        return reward.kind in (Reward.Kind.FREE_DELIVERY, Reward.Kind.CUSTOM)

    @staticmethod
    def _admin_payload(reward, visible_product_ids=None):
        return admin_reward_dict(
            reward,
            customer_available=RewardCatalogService._customer_available(
                reward,
                visible_product_ids,
            ),
        )

    @staticmethod
    def list_admin():
        rows = list(
            Reward.objects.select_related('product').order_by('sort_order', 'id')
        )
        visible_product_ids = set(
            customer_visible_product_rows().values_list('product_id', flat=True)
        )
        customer_available = {
            row.id: RewardCatalogService._customer_available(
                row,
                visible_product_ids,
            )
            for row in rows
        }
        return ServiceResponse.success(data={
            'items': [
                admin_reward_dict(
                    row,
                    customer_available=customer_available[row.id],
                )
                for row in rows
            ],
            'summary': {
                'total': len(rows),
                'active': sum(customer_available.values()),
                'out_of_stock': sum(
                    row.is_active and row.stock == 0 for row in rows
                ),
            },
        })

    @staticmethod
    def _apply(reward, values, *, require_complete=False):
        errors = _apply_localized(reward, values, _REWARD_TEXT_LIMITS)

        if 'kind' in values:
            kind = str(values['kind'] or '').upper()
            if kind not in Reward.Kind.values:
                errors['kind'] = 'Choose a supported reward type.'
            else:
                reward.kind = kind

        for field, minimum, nullable in (
            ('points_cost', 1, False),
            ('stock', 0, True),
            ('per_customer_limit', 0, False),
            ('sort_order', None, False),
        ):
            if field not in values:
                continue
            parsed, error = _integer(values[field], minimum=minimum, nullable=nullable)
            if error:
                errors[field] = error
            else:
                setattr(reward, field, parsed)

        if 'discount_amount' in values:
            parsed, error = _decimal(values['discount_amount'])
            if error:
                errors['discount_amount'] = error
            else:
                reward.discount_amount = parsed

        if 'product_id' in values:
            raw_product = values['product_id']
            if raw_product is None or raw_product == '':
                reward.product_id = None
            else:
                product_id, error = _integer(raw_product, minimum=1)
                if error:
                    errors['product_id'] = error
                else:
                    reward.product_id = product_id

        if 'is_active' in values:
            parsed, error = _boolean(values['is_active'])
            if error:
                errors['is_active'] = error
            else:
                reward.is_active = parsed

        if not _has_localized(reward, 'name'):
            errors['name_uz'] = 'Add a reward name in at least one language.'
        if reward.points_cost < 1 and (
            require_complete or reward.is_active or 'points_cost' in values
        ):
            errors['points_cost'] = 'A reward must cost at least 1 point.'
        if reward.kind == Reward.Kind.FREE_PRODUCT:
            if reward.product_id is None:
                errors['product_id'] = 'Choose the product customers receive.'
            elif reward.is_active and not is_product_customer_visible(reward.product_id):
                errors['product_id'] = 'Choose a product currently available in the bot.'
            reward.discount_amount = Decimal('0')
        elif reward.kind == Reward.Kind.DISCOUNT:
            reward.product_id = None
            if reward.discount_amount <= 0:
                errors['discount_amount'] = 'Enter a discount greater than 0.'
        else:
            reward.product_id = None
            reward.discount_amount = Decimal('0')
        return errors

    @staticmethod
    @transaction.atomic
    def create(values):
        reward = Reward(is_active=False)
        errors = RewardCatalogService._apply(reward, values, require_complete=True)
        if errors:
            return ServiceResponse.validation_error(errors)
        reward.save()
        return ServiceResponse.created(
            data=RewardCatalogService._admin_payload(reward),
            message='Reward created',
        )

    @staticmethod
    @transaction.atomic
    def update(reward_id, values):
        reward = Reward.objects.select_for_update().filter(id=reward_id).first()
        if reward is None:
            return ServiceResponse.not_found('Reward not found')
        errors = RewardCatalogService._apply(reward, values)
        if errors:
            return ServiceResponse.validation_error(errors)
        reward.save()
        return ServiceResponse.success(
            data=RewardCatalogService._admin_payload(reward),
            message='Reward updated',
        )

    @staticmethod
    @transaction.atomic
    def delete(reward_id):
        reward = Reward.objects.select_for_update().filter(id=reward_id).first()
        if reward is None:
            return ServiceResponse.not_found('Reward not found')
        if Redemption.objects.filter(reward=reward).exists():
            return ServiceResponse.error(
                'This reward has redemption history. Turn it off instead.'
            )
        image_url = reward.image_url
        reward.delete()
        from smartfood.services.media_service import delete_managed_media
        delete_managed_media(image_url, 'rewards')
        return ServiceResponse.success(data={'id': reward_id}, message='Reward deleted')
