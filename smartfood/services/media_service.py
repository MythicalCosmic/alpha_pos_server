"""Validated media storage for Telegram catalog, banners, and rewards."""
import logging
import re
import uuid
import warnings
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from PIL import Image, UnidentifiedImageError

from base.helpers.response import ServiceResponse
from smartfood.models import BotBanner, BotBroadcast, BotProduct, Reward
from smartfood.serializers import admin_banner_dict, admin_broadcast_dict, product_dict


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
    'broadcasts': {
        'public': '/api/smartfood/media/broadcasts/',
        'storage': 'smartfood/broadcasts/',
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


def _decode_image(content, extension, kind):
    """Fully decode media and enforce Telegram's sendPhoto geometry."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                detected = (image.format or '').upper()
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning,
            UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None, None, 'The file is damaged or is not a decodable image.'

    expected = {'jpg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}[extension]
    if detected != expected or width <= 0 or height <= 0:
        return None, None, 'The image format does not match its file content.'

    if kind == 'broadcasts':
        if width + height > 10000 or max(width, height) / min(width, height) > 20:
            return None, None, (
                'Telegram photos require width + height up to 10,000 pixels '
                'and an aspect ratio no wider than 20:1.'
            )
        # Telegram's photo endpoint is most predictable with JPEG/PNG. Convert
        # WebP once at upload time instead of letting every recipient fail.
        if extension == 'webp':
            with Image.open(BytesIO(content)) as source:
                if source.mode in ('RGBA', 'LA'):
                    rgba = source.convert('RGBA')
                    background = Image.new('RGB', rgba.size, 'white')
                    background.paste(rgba, mask=rgba.getchannel('A'))
                    converted = background
                else:
                    converted = source.convert('RGB')
                output = BytesIO()
                converted.save(output, format='JPEG', quality=92, optimize=True)
                content = output.getvalue()
                extension = 'jpg'
    return content, extension, ''


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
    content, extension, image_error = _decode_image(content, extension, kind)
    if image_error:
        return ServiceResponse.validation_error({'image': image_error})
    if len(content) > MAX_IMAGE_BYTES:
        return ServiceResponse.validation_error({
            'image': 'The converted image must be 8 MB or smaller.',
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
        transaction.on_commit(
            lambda url=previous_url: delete_managed_media(url, kind),
            robust=True,
        )
    return ServiceResponse.success(data=serializer(obj), message=message)


def _remove(obj, *, kind, serializer, message, deactivate=False):
    previous_url = obj.image_url
    obj.image_url = ''
    fields = ['image_url', 'updated_at']
    if deactivate and getattr(obj, 'is_active', False):
        obj.is_active = False
        fields.append('is_active')
    obj.save(update_fields=fields)
    transaction.on_commit(
        lambda url=previous_url: delete_managed_media(url, kind),
        robust=True,
    )
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


class BroadcastMediaService:
    @staticmethod
    def _get(broadcast_id, *, for_update=False):
        queryset = BotBroadcast.objects.select_related('created_by')
        if for_update:
            queryset = queryset.select_for_update()
        return queryset.filter(
            id=broadcast_id,
        ).first()

    @staticmethod
    def _editable(broadcast):
        if broadcast is None:
            return ServiceResponse.not_found('Broadcast not found')
        if broadcast.status != BotBroadcast.Status.DRAFT:
            return ({
                'success': False,
                'code': 'broadcast_locked',
                'message': 'A queued or sent broadcast cannot be edited.',
            }, 409)
        return None

    @staticmethod
    @transaction.atomic
    def upload(broadcast_id, uploaded):
        # Serialize against BroadcastService.send(), which locks the same row
        # before freezing its fan-out. Queued media can never be replaced or
        # removed underneath pending recipients.
        broadcast = BroadcastMediaService._get(broadcast_id, for_update=True)
        error = BroadcastMediaService._editable(broadcast)
        if error:
            return error
        return _upload(
            broadcast,
            uploaded,
            kind='broadcasts',
            serializer=admin_broadcast_dict,
            message='Broadcast photo uploaded',
        )

    @staticmethod
    @transaction.atomic
    def remove(broadcast_id):
        broadcast = BroadcastMediaService._get(broadcast_id, for_update=True)
        error = BroadcastMediaService._editable(broadcast)
        if error:
            return error
        return _remove(
            broadcast,
            kind='broadcasts',
            serializer=admin_broadcast_dict,
            message='Broadcast photo removed',
        )
