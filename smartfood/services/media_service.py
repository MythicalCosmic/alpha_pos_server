"""Validated media storage for Telegram catalog, banners, and rewards."""
import logging
import re
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from base.helpers.response import ServiceResponse
from smartfood.models import BotBanner, BotProduct, Reward
from smartfood.serializers import admin_banner_dict, product_dict


logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PRODUCT_IMAGE_BYTES = MAX_IMAGE_BYTES  # Backwards-compatible import.
_MEDIA = {
    'products': {
        'public': '/api/smartfood/media/products/',
        'storage': 'smartfood/products/',
    },
    'banners': {
        'public': '/api/smartfood/media/banners/',
        'storage': 'smartfood/banners/',
    },
    'rewards': {
        'public': '/api/smartfood/media/rewards/',
        'storage': 'smartfood/rewards/',
    },
}
PUBLIC_PRODUCT_MEDIA_PREFIX = _MEDIA['products']['public']
STORAGE_PRODUCT_MEDIA_PREFIX = _MEDIA['products']['storage']
_SAFE_FILENAME = re.compile(r'^[a-f0-9]{32}\.(?:jpg|png|webp)$')


def _admin_reward_payload(reward):
    # Import lazily: marketing_service intentionally imports this module only
    # inside delete operations. The shared payload builder keeps image responses
    # as authoritative as create/update/list responses.
    from smartfood.services.marketing_service import RewardCatalogService
    return RewardCatalogService._admin_payload(reward)


def _image_kind(content):
    if content.startswith(b'\xff\xd8\xff'):
        return 'jpg', 'image/jpeg'
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png', 'image/png'
    if len(content) >= 12 and content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    return None, None


def managed_media_storage_name(url, kind):
    config = _MEDIA.get(kind)
    if config is None or not (url or '').startswith(config['public']):
        return None
    filename = url[len(config['public']):]
    if not _SAFE_FILENAME.fullmatch(filename):
        return None
    return config['storage'] + filename


def storage_name_from_public_url(url):
    """Compatibility wrapper retained for product-media callers and tests."""
    return managed_media_storage_name(url, 'products')


def media_file(kind, filename):
    config = _MEDIA.get(kind)
    if config is None or not _SAFE_FILENAME.fullmatch(filename or ''):
        return None
    storage_name = config['storage'] + filename
    if not default_storage.exists(storage_name):
        return None
    extension = filename.rsplit('.', 1)[-1]
    content_type = {
        'jpg': 'image/jpeg',
        'png': 'image/png',
        'webp': 'image/webp',
    }[extension]
    return default_storage.open(storage_name, 'rb'), content_type


def product_media_file(filename):
    return media_file('products', filename)


def delete_managed_media(url, kind):
    """Delete only files minted by this service; external URLs are untouched."""
    storage_name = managed_media_storage_name(url, kind)
    if not storage_name:
        return
    try:
        default_storage.delete(storage_name)
    except OSError:
        # The database state is authoritative.  A failed best-effort cleanup is
        # logged without turning a successful operator action into an HTTP 500.
        logger.exception('Could not delete managed %s media', kind)


def _upload(obj, uploaded, *, kind, serializer, message, update_fields=None):
    if uploaded is None:
        return ServiceResponse.validation_error({'image': 'Choose an image.'})
    try:
        content = uploaded.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        logger.exception('Could not read uploaded %s image', kind)
        return ({'success': False, 'message': 'The image could not be read.'}, 400)
    if len(content) > MAX_IMAGE_BYTES:
        return ServiceResponse.validation_error({
            'image': 'Image must be 8 MB or smaller.',
        })
    extension, _ = _image_kind(content)
    if extension is None:
        return ServiceResponse.validation_error({
            'image': 'Use a JPEG, PNG, or WebP image.',
        })

    filename = f'{uuid.uuid4().hex}.{extension}'
    storage_name = _MEDIA[kind]['storage'] + filename
    try:
        saved_name = default_storage.save(storage_name, ContentFile(content))
    except OSError:
        logger.exception('Could not persist uploaded %s image', kind)
        return ({
            'success': False,
            'message': 'Image storage is temporarily unavailable. Try again shortly.',
        }, 503)

    saved_filename = saved_name.rsplit('/', 1)[-1]
    if not _SAFE_FILENAME.fullmatch(saved_filename):
        delete_managed_media(_MEDIA[kind]['public'] + saved_filename, kind)
        return ({'success': False, 'message': 'The image could not be stored safely.'}, 500)

    new_url = _MEDIA[kind]['public'] + saved_filename
    previous_url = obj.image_url
    obj.image_url = new_url
    fields = list(update_fields or ['image_url', 'updated_at'])
    try:
        obj.save(update_fields=fields)
    except Exception:
        delete_managed_media(new_url, kind)
        raise
    if previous_url != new_url:
        delete_managed_media(previous_url, kind)
    return ServiceResponse.success(data=serializer(obj), message=message)


def _remove(obj, *, kind, serializer, message, deactivate=False):
    previous_url = obj.image_url
    obj.image_url = ''
    fields = ['image_url', 'updated_at']
    if deactivate and getattr(obj, 'is_active', False):
        obj.is_active = False
        fields.append('is_active')
    obj.save(update_fields=fields)
    delete_managed_media(previous_url, kind)
    return ServiceResponse.success(data=serializer(obj), message=message)


class ProductMediaService:
    @staticmethod
    def _get(product_id):
        return (
            BotProduct.objects.select_related('product', 'product__category')
            .filter(product_id=product_id, is_published=True)
            .first()
        )

    @staticmethod
    def upload(product_id, uploaded):
        shadow = ProductMediaService._get(product_id)
        if shadow is None:
            return ServiceResponse.not_found('Product is not imported to the bot')
        return _upload(
            shadow,
            uploaded,
            kind='products',
            serializer=product_dict,
            message='Product image uploaded',
        )

    @staticmethod
    def remove(product_id):
        shadow = ProductMediaService._get(product_id)
        if shadow is None:
            return ServiceResponse.not_found('Product is not imported to the bot')
        return _remove(
            shadow,
            kind='products',
            serializer=product_dict,
            message='Product image removed',
        )


class BannerMediaService:
    @staticmethod
    def _get(banner_id):
        return BotBanner.objects.filter(id=banner_id).first()

    @staticmethod
    def upload(banner_id, uploaded):
        banner = BannerMediaService._get(banner_id)
        if banner is None:
            return ServiceResponse.not_found('Banner not found')
        return _upload(
            banner,
            uploaded,
            kind='banners',
            serializer=admin_banner_dict,
            message='Banner image uploaded',
        )

    @staticmethod
    def remove(banner_id):
        banner = BannerMediaService._get(banner_id)
        if banner is None:
            return ServiceResponse.not_found('Banner not found')
        return _remove(
            banner,
            kind='banners',
            serializer=admin_banner_dict,
            message='Banner image removed and banner paused',
            deactivate=True,
        )


class RewardMediaService:
    @staticmethod
    def _get(reward_id):
        return Reward.objects.filter(id=reward_id).first()

    @staticmethod
    def upload(reward_id, uploaded):
        reward = RewardMediaService._get(reward_id)
        if reward is None:
            return ServiceResponse.not_found('Reward not found')
        return _upload(
            reward,
            uploaded,
            kind='rewards',
            serializer=_admin_reward_payload,
            message='Reward image uploaded',
        )

    @staticmethod
    def remove(reward_id):
        reward = RewardMediaService._get(reward_id)
        if reward is None:
            return ServiceResponse.not_found('Reward not found')
        return _remove(
            reward,
            kind='rewards',
            serializer=_admin_reward_payload,
            message='Reward image removed',
        )
