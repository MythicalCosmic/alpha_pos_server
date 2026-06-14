"""Operator catalog management — publish the POS catalog to the bot and curate
its bot-only state (trilingual overrides, images, sizes, toppings, stop-selling).

The POS owns base.Product / base.Category (and price); the operator never edits
those here. "Accepting" a product/category just get-or-creates its thin shadow
(BotProduct / BotCategory) and flips is_published — every other field written
here is bot-only. Sizes / topping groups / toppings are NEW rows (no POS twin),
attached to base.Product by FK, so they are created/updated/deleted outright.
"""
from decimal import Decimal, InvalidOperation

from base.helpers.response import ServiceResponse
from smartfood.models import (
    BotCategory, BotProduct, Size, Topping, ToppingGroup,
)
from smartfood.serializers import (
    category_dict, product_dict, size_dict, topping_dict, topping_group_dict, uzs,
)

# Bot-only fields settable on each shadow / row (price is read live from the POS).
_PRODUCT_FIELDS = (
    'name_uz', 'name_ru', 'name_en', 'desc_uz', 'desc_ru', 'desc_en',
    'image_url', 'tag', 'kcal', 'sort_order', 'is_selling',
)
_CATEGORY_FIELDS = ('name_uz', 'name_ru', 'name_en', 'image_url', 'sort_order', 'is_selling')
_SIZE_FIELDS = ('name_uz', 'name_ru', 'name_en', 'price_delta', 'is_default', 'is_selling', 'sort_order')
_GROUP_FIELDS = ('name_uz', 'name_ru', 'name_en', 'is_required', 'min_select', 'max_select', 'sort_order')
_TOPPING_FIELDS = ('name_uz', 'name_ru', 'name_en', 'price', 'is_selling', 'sort_order')

# Fields that are stored as Decimal — cast via Decimal(str(...)) so client ints
# and floats land cleanly and a bad value is a clean 422, not a 500.
_DECIMAL_FIELDS = {'price_delta', 'price'}


def _apply(obj, fields, values):
    """Copy whitelisted, non-None keys from `values` onto `obj`. Returns the list
    of error keys for un-castable decimals (empty == ok)."""
    bad = []
    for key in fields:
        if key not in values or values[key] is None:
            continue
        val = values[key]
        if key in _DECIMAL_FIELDS:
            try:
                val = Decimal(str(val))
            except (InvalidOperation, ValueError, TypeError):
                bad.append(key)
                continue
        setattr(obj, key, val)
    return bad


class AdminCatalogService:
    # ---- products --------------------------------------------------------- #
    @staticmethod
    def list_unpublished_products():
        """POS products not yet live on the bot — either no shadow, or a shadow
        with is_published=False — so the operator can accept them."""
        from base.models import Product
        published_ids = set(
            BotProduct.objects.filter(is_published=True).values_list('product_id', flat=True)
        )
        rows = []
        for p in Product.objects.filter(is_deleted=False).order_by('id'):
            if p.id in published_ids:
                continue
            rows.append({
                'id': p.id,
                'name': p.name,
                'price': uzs(p.price),
                'category_id': p.category_id,
                'published': hasattr(p, 'bot'),
            })
        return ServiceResponse.success(data={'items': rows})

    @staticmethod
    def accept_product(product_id, **fields):
        from base.models import Product
        product = Product.objects.filter(id=product_id, is_deleted=False).first()
        if not product:
            return ServiceResponse.not_found('Product not found')
        shadow, _ = BotProduct.objects.get_or_create(product=product)
        bad = _apply(shadow, _PRODUCT_FIELDS, fields)
        if bad:
            return ServiceResponse.validation_error({k: 'invalid number' for k in bad})
        shadow.is_published = True
        shadow.save()
        return ServiceResponse.success(data=product_dict(shadow), message='Product accepted')

    @staticmethod
    def update_product(product_id, **fields):
        shadow = BotProduct.objects.filter(product_id=product_id).first()
        if not shadow:
            return ServiceResponse.not_found('Product not accepted to the bot')
        bad = _apply(shadow, _PRODUCT_FIELDS, fields)
        if bad:
            return ServiceResponse.validation_error({k: 'invalid number' for k in bad})
        shadow.save()
        return ServiceResponse.success(data=product_dict(shadow), message='Product updated')

    @staticmethod
    def set_product_selling(product_id, selling):
        shadow = BotProduct.objects.filter(product_id=product_id).first()
        if not shadow:
            return ServiceResponse.not_found('Product not accepted to the bot')
        shadow.is_selling = bool(selling)
        shadow.save(update_fields=['is_selling', 'updated_at'])
        return ServiceResponse.success(data=product_dict(shadow))

    # ---- categories ------------------------------------------------------- #
    @staticmethod
    def accept_category(category_id, **fields):
        from base.models import Category
        category = Category.objects.filter(id=category_id, is_deleted=False).first()
        if not category:
            return ServiceResponse.not_found('Category not found')
        shadow, _ = BotCategory.objects.get_or_create(category=category)
        _apply(shadow, _CATEGORY_FIELDS, fields)
        shadow.is_published = True
        shadow.save()
        return ServiceResponse.success(data=category_dict(shadow), message='Category accepted')

    @staticmethod
    def update_category(category_id, **fields):
        shadow = BotCategory.objects.filter(category_id=category_id).first()
        if not shadow:
            return ServiceResponse.not_found('Category not accepted to the bot')
        _apply(shadow, _CATEGORY_FIELDS, fields)
        shadow.save()
        return ServiceResponse.success(data=category_dict(shadow), message='Category updated')

    @staticmethod
    def set_category_selling(category_id, selling):
        shadow = BotCategory.objects.filter(category_id=category_id).first()
        if not shadow:
            return ServiceResponse.not_found('Category not accepted to the bot')
        shadow.is_selling = bool(selling)
        shadow.save(update_fields=['is_selling', 'updated_at'])
        return ServiceResponse.success(data=category_dict(shadow))

    # ---- sizes ------------------------------------------------------------ #
    @staticmethod
    def create_size(product_id, **fields):
        from base.models import Product
        if not Product.objects.filter(id=product_id, is_deleted=False).exists():
            return ServiceResponse.not_found('Product not found')
        size = Size(product_id=product_id)
        bad = _apply(size, _SIZE_FIELDS, fields)
        if bad:
            return ServiceResponse.validation_error({k: 'invalid number' for k in bad})
        size.save()
        return ServiceResponse.created(data=size_dict(size), message='Size created')

    @staticmethod
    def update_size(size_id, **fields):
        size = Size.objects.filter(id=size_id).first()
        if not size:
            return ServiceResponse.not_found('Size not found')
        bad = _apply(size, _SIZE_FIELDS, fields)
        if bad:
            return ServiceResponse.validation_error({k: 'invalid number' for k in bad})
        size.save()
        return ServiceResponse.success(data=size_dict(size), message='Size updated')

    @staticmethod
    def delete_size(size_id):
        size = Size.objects.filter(id=size_id).first()
        if not size:
            return ServiceResponse.not_found('Size not found')
        size.delete()
        return ServiceResponse.success(data={'id': size_id}, message='Size deleted')

    # ---- topping groups --------------------------------------------------- #
    @staticmethod
    def create_topping_group(product_id, **fields):
        from base.models import Product
        if not Product.objects.filter(id=product_id, is_deleted=False).exists():
            return ServiceResponse.not_found('Product not found')
        group = ToppingGroup(product_id=product_id)
        _apply(group, _GROUP_FIELDS, fields)
        group.save()
        return ServiceResponse.created(data=topping_group_dict(group), message='Topping group created')

    @staticmethod
    def update_topping_group(group_id, **fields):
        group = ToppingGroup.objects.filter(id=group_id).first()
        if not group:
            return ServiceResponse.not_found('Topping group not found')
        _apply(group, _GROUP_FIELDS, fields)
        group.save()
        return ServiceResponse.success(data=topping_group_dict(group), message='Topping group updated')

    @staticmethod
    def delete_topping_group(group_id):
        group = ToppingGroup.objects.filter(id=group_id).first()
        if not group:
            return ServiceResponse.not_found('Topping group not found')
        group.delete()
        return ServiceResponse.success(data={'id': group_id}, message='Topping group deleted')

    # ---- toppings --------------------------------------------------------- #
    @staticmethod
    def create_topping(group_id, **fields):
        if not ToppingGroup.objects.filter(id=group_id).exists():
            return ServiceResponse.not_found('Topping group not found')
        topping = Topping(group_id=group_id)
        bad = _apply(topping, _TOPPING_FIELDS, fields)
        if bad:
            return ServiceResponse.validation_error({k: 'invalid number' for k in bad})
        topping.save()
        return ServiceResponse.created(data=topping_dict(topping), message='Topping created')

    @staticmethod
    def update_topping(topping_id, **fields):
        topping = Topping.objects.filter(id=topping_id).first()
        if not topping:
            return ServiceResponse.not_found('Topping not found')
        bad = _apply(topping, _TOPPING_FIELDS, fields)
        if bad:
            return ServiceResponse.validation_error({k: 'invalid number' for k in bad})
        topping.save()
        return ServiceResponse.success(data=topping_dict(topping), message='Topping updated')

    @staticmethod
    def delete_topping(topping_id):
        topping = Topping.objects.filter(id=topping_id).first()
        if not topping:
            return ServiceResponse.not_found('Topping not found')
        topping.delete()
        return ServiceResponse.success(data={'id': topping_id}, message='Topping deleted')
