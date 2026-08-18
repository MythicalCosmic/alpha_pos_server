"""Validated product-image storage for the public Telegram catalog."""
import re
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from base.helpers.response import ServiceResponse
from smartfood.models import BotProduct
from smartfood.serializers import product_dict


MAX_PRODUCT_IMAGE_BYTES = 8 * 1024 * 1024
PUBLIC_PRODUCT_MEDIA_PREFIX = '/api/smartfood/media/products/'
STORAGE_PRODUCT_MEDIA_PREFIX = 'smartfood/products/'
_SAFE_FILENAME = re.compile(r'^[a-f0-9]{32}\.(?:jpg|png|webp)$')


def _image_kind(content):
    if content.startswith(b'\xff\xd8\xff'):
        return 'jpg', 'image/jpeg'
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png', 'image/png'
    if len(content) >= 12 and content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    return None, None


def storage_name_from_public_url(url):
    if not (url or '').startswith(PUBLIC_PRODUCT_MEDIA_PREFIX):
        return None
    filename = url[len(PUBLIC_PRODUCT_MEDIA_PREFIX):]
    if not _SAFE_FILENAME.fullmatch(filename):
        return None
    return STORAGE_PRODUCT_MEDIA_PREFIX + filename


def product_media_file(filename):
    if not _SAFE_FILENAME.fullmatch(filename or ''):
        return None
    storage_name = STORAGE_PRODUCT_MEDIA_PREFIX + filename
    if not default_storage.exists(storage_name):
        return None
    extension = filename.rsplit('.', 1)[-1]
    content_type = {
        'jpg': 'image/jpeg',
        'png': 'image/png',
        'webp': 'image/webp',
    }[extension]
    return default_storage.open(storage_name, 'rb'), content_type


class ProductMediaService:
    @staticmethod
    def upload(product_id, uploaded):
        shadow = (
            BotProduct.objects.select_related('product')
            .filter(product_id=product_id, is_published=True)
            .first()
        )
        if shadow is None:
            return ServiceResponse.not_found('Product is not imported to the bot')
        if uploaded is None:
            return ServiceResponse.validation_error({'image': 'Choose an image.'})

        content = uploaded.read(MAX_PRODUCT_IMAGE_BYTES + 1)
        if len(content) > MAX_PRODUCT_IMAGE_BYTES:
            return ServiceResponse.validation_error({
                'image': 'Image must be 8 MB or smaller.',
            })
        extension, _ = _image_kind(content)
        if extension is None:
            return ServiceResponse.validation_error({
                'image': 'Use a JPEG, PNG, or WebP image.',
            })

        filename = f'{uuid.uuid4().hex}.{extension}'
        storage_name = STORAGE_PRODUCT_MEDIA_PREFIX + filename
        saved_name = default_storage.save(storage_name, ContentFile(content))
        saved_filename = saved_name.rsplit('/', 1)[-1]
        new_url = PUBLIC_PRODUCT_MEDIA_PREFIX + saved_filename
        previous_name = storage_name_from_public_url(shadow.image_url)

        shadow.image_url = new_url
        shadow.save(update_fields=['image_url', 'updated_at'])
        if previous_name and previous_name != saved_name:
            default_storage.delete(previous_name)

        return ServiceResponse.success(
            data=product_dict(shadow),
            message='Product image uploaded',
        )

    @staticmethod
    def remove(product_id):
        shadow = (
            BotProduct.objects.select_related('product')
            .filter(product_id=product_id, is_published=True)
            .first()
        )
        if shadow is None:
            return ServiceResponse.not_found('Product is not imported to the bot')
        previous_name = storage_name_from_public_url(shadow.image_url)
        shadow.image_url = ''
        shadow.save(update_fields=['image_url', 'updated_at'])
        if previous_name:
            default_storage.delete(previous_name)
        return ServiceResponse.success(
            data=product_dict(shadow),
            message='Product image removed',
        )
