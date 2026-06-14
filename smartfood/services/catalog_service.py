"""Read-only catalog for the customer Mini App.

Only ever exposes bot rows that are BOTH published (accepted to the bot) and
selling (runtime in-stock), and whose POS base row is not deleted. A product is
also hidden if its category is unpublished/stop-selling, so a hidden category
hides its whole menu without touching each product.
"""
from django.db.models import Q

from base.helpers.response import ServiceResponse
from smartfood.models import BotCategory, BotProduct
from smartfood.serializers import category_dict, product_dict


class CatalogService:
    @staticmethod
    def categories(lang='uz'):
        qs = (BotCategory.objects.select_related('category')
              .filter(is_published=True, is_selling=True, category__is_deleted=False)
              .order_by('sort_order', 'id'))
        return ServiceResponse.success(data={'items': [category_dict(c, lang) for c in qs]})

    @staticmethod
    def products(lang='uz', category_id=None, tag=None, q=None):
        qs = (BotProduct.objects.select_related('product')
              .filter(is_published=True, is_selling=True, product__is_deleted=False,
                      product__category__bot__is_published=True,
                      product__category__bot__is_selling=True))
        if category_id:
            qs = qs.filter(product__category_id=category_id)
        if tag:
            qs = qs.filter(tag=tag)
        if q:
            qs = qs.filter(Q(product__name__icontains=q) | Q(name_uz__icontains=q)
                           | Q(name_ru__icontains=q) | Q(name_en__icontains=q))
        qs = qs.order_by('sort_order', 'id')
        return ServiceResponse.success(data={'items': [product_dict(p, lang, detail=False) for p in qs]})

    @staticmethod
    def product_detail(product_id, lang='uz'):
        # Same gate as the list query: a stop-selling product, or one whose
        # category is unpublished/stopped, must NOT leak its detail either.
        bp = (BotProduct.objects.select_related('product')
              .prefetch_related('product__bot_sizes', 'product__topping_groups__toppings')
              .filter(product_id=product_id, is_published=True, is_selling=True,
                      product__is_deleted=False,
                      product__category__bot__is_published=True,
                      product__category__bot__is_selling=True).first())
        if not bp:
            return ServiceResponse.not_found('Product not found')
        return ServiceResponse.success(data=product_dict(bp, lang, detail=True))
