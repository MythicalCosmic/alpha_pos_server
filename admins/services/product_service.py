from decimal import Decimal, InvalidOperation
from base.repositories import ProductRepository, CategoryRepository
from base.helpers.response import ServiceResponse


ALLOWED_ORDER_FIELDS = {
    'name', '-name', 'price', '-price',
    'created_at', '-created_at', 'updated_at', '-updated_at',
    'id', '-id', 'category__name', '-category__name',
}


def _serialize_product(product):
    return {
        'id': product.id,
        'name': product.name,
        'description': product.description,
        'price': str(product.price),
        'colors': product.colors,
        'is_instant': product.is_instant,
        'category_id': product.category_id,
        'category': {
            'id': product.category.id,
            'name': product.category.name,
            'slug': product.category.slug,
        } if product.category else None,
        'is_deleted': product.is_deleted,
        'created_at': product.created_at.isoformat() if product.created_at else None,
        'updated_at': product.updated_at.isoformat() if product.updated_at else None,
    }


def _serialize_product_short(product):
    return {
        'id': product.id,
        'name': product.name,
        'description': product.description,
        'price': str(product.price),
        'colors': product.colors,
        'is_instant': product.is_instant,
        'category_id': product.category_id,
    }


def _parse_price(value):
    try:
        price = Decimal(str(value))
        if price <= 0:
            return None, "Price must be greater than 0"
        return price, None
    except (InvalidOperation, ValueError, TypeError):
        return None, "Price must be a valid number"


class AdminProductService:

    @staticmethod
    def get_all_products(page=1, per_page=20, search=None, category_ids=None,
                         order_by='-created_at', include_deleted=False, popular=True):
        if include_deleted:
            queryset = ProductRepository.model.objects.select_related('category').all()
        else:
            queryset = ProductRepository.model.objects.select_related('category').filter(is_deleted=False)

        if search:
            queryset = ProductRepository.search(queryset, search)

        if category_ids:
            if isinstance(category_ids, str):
                category_ids = [int(x.strip()) for x in category_ids.split(',') if x.strip().isdigit()]
            if category_ids:
                queryset = queryset.filter(category_id__in=category_ids)

        if order_by not in ALLOWED_ORDER_FIELDS:
            order_by = '-created_at'
        if popular:
            # Top-selling first (default). Composes with the category/search
            # filters above. popular=False restores the plain order_by.
            from base.repositories.order_item import OrderItemRepository
            queryset = OrderItemRepository.apply_popularity_order(
                queryset, fallback_order_by=order_by)
        else:
            queryset = queryset.order_by(order_by)

        page_obj, paginator = ProductRepository.paginate(queryset, page, per_page)

        products = [_serialize_product(p) for p in page_obj.object_list]

        return ServiceResponse.success(data={
            'products': products,
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_products': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })

    @staticmethod
    def get_products_by_category(category_id):
        category = CategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found("Category not found")

        products = ProductRepository.get_by_category_id(category_id).select_related('category').order_by('name')
        return ServiceResponse.success(data={
            'products': [_serialize_product(p) for p in products],
            'category': {
                'id': category.id,
                'name': category.name,
                'slug': category.slug,
            },
        })

    @staticmethod
    def get_product_by_id(product_id, include_deleted=False):
        if include_deleted:
            product = ProductRepository.get_by_id_include_deleted(product_id)
        else:
            product = ProductRepository.get_by_id_cached(product_id)

        if not product:
            return ServiceResponse.not_found("Product not found")

        return ServiceResponse.success(data={'product': _serialize_product(product)})

    @staticmethod
    def create_product(name, description, price, category_id, colors=None, is_instant=False):
        name = name.strip()

        price, error = _parse_price(price)
        if error:
            return ServiceResponse.validation_error(
                errors={'price': error},
                message=error,
            )

        category = CategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found("Category not found")

        if ProductRepository.name_exists(name, category_id):
            return ServiceResponse.error("Product with this name already exists in this category")

        product = ProductRepository.create(
            name=name,
            description=description or '',
            price=price,
            category=category,
            colors=colors or [],
            is_instant=bool(is_instant),
        )

        ProductRepository.invalidate_cache()

        return ServiceResponse.created(
            data={'product': _serialize_product_short(product)},
            message="Product created successfully",
        )

    @staticmethod
    def update_product(product_id, **kwargs):
        product = ProductRepository.get_by_id(product_id)
        if not product:
            return ServiceResponse.not_found("Product not found")

        if 'price' in kwargs:
            price, error = _parse_price(kwargs['price'])
            if error:
                return ServiceResponse.validation_error(
                    errors={'price': error},
                    message=error,
                )
            kwargs['price'] = price

        if 'category_id' in kwargs:
            category = CategoryRepository.get_by_id(kwargs['category_id'])
            if not category:
                return ServiceResponse.not_found("Category not found")
            kwargs['category'] = category
            del kwargs['category_id']

        if 'name' in kwargs and kwargs['name']:
            new_name = kwargs['name'].strip()
            cat_id = kwargs.get('category', product.category).id if 'category' in kwargs else product.category_id
            if new_name.lower() != product.name.lower() or cat_id != product.category_id:
                if ProductRepository.name_exists(new_name, cat_id, exclude_id=product_id):
                    return ServiceResponse.error("Product with this name already exists in this category")
            kwargs['name'] = new_name

        if 'is_instant' in kwargs and kwargs['is_instant'] is not None:
            kwargs['is_instant'] = bool(kwargs['is_instant'])

        allowed_fields = {'name', 'description', 'price', 'category', 'colors', 'is_instant'}
        for key, value in kwargs.items():
            if key in allowed_fields and hasattr(product, key):
                setattr(product, key, value)

        product.save()
        ProductRepository.invalidate_cache()

        return ServiceResponse.success(
            data={'product': _serialize_product_short(product)},
            message="Product updated successfully",
        )

    @staticmethod
    def delete_product(product_id, hard_delete=False):
        if hard_delete:
            product = ProductRepository.get_by_id_include_deleted(product_id)
        else:
            product = ProductRepository.get_by_id(product_id)

        if not product:
            return ServiceResponse.not_found("Product not found")

        if not hard_delete and product.is_deleted:
            return ServiceResponse.error("Product is already deleted")

        if hard_delete:
            # Physical cloud deletes cannot be represented by the timestamp
            # change feed. Retain a synchronized tombstone row so all branches
            # stop selling the product instead of keeping a stale copy.
            product.delete()
            ProductRepository.invalidate_cache()
            return ServiceResponse.success(message="Product archived successfully")

        product.is_deleted = True
        product.save(update_fields=['is_deleted', 'synced_at', 'sync_version'])
        ProductRepository.invalidate_cache()
        return ServiceResponse.success(message="Product deleted successfully")

    @staticmethod
    def restore_product(product_id):
        product = ProductRepository.get_by_id_include_deleted(product_id)
        if not product:
            return ServiceResponse.not_found("Product not found")

        if not product.is_deleted:
            return ServiceResponse.error("Product is not deleted")

        product.is_deleted = False
        product.save()
        ProductRepository.invalidate_cache()

        return ServiceResponse.success(
            data={
                'product': {
                    'id': product.id,
                    'name': product.name,
                }
            },
            message="Product restored successfully",
        )

    @staticmethod
    def get_product_stats():
        stats = ProductRepository.get_stats()
        return ServiceResponse.success(data=stats)

    @staticmethod
    def get_deleted_products(page=1, per_page=20):
        queryset = ProductRepository.get_deleted()
        page_obj, paginator = ProductRepository.paginate(queryset, page, per_page)

        products = []
        for p in page_obj.object_list:
            products.append({
                'id': p.id,
                'name': p.name,
                'category_name': p.category.name if p.category else None,
                'updated_at': p.updated_at.isoformat() if p.updated_at else None,
            })

        return ServiceResponse.success(data={
            'products': products,
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_products': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })

    @staticmethod
    def bulk_delete(product_ids):
        if not product_ids or not isinstance(product_ids, list):
            return ServiceResponse.validation_error(
                errors={'ids': 'Invalid product IDs'},
                message="Validation failed",
            )

        deleted = ProductRepository.bulk_soft_delete(product_ids)
        ProductRepository.invalidate_cache()

        return ServiceResponse.success(
            data={'deleted_count': deleted},
            message=f"{deleted} product(s) deleted successfully",
        )

    @staticmethod
    def bulk_restore(product_ids):
        if not product_ids or not isinstance(product_ids, list):
            return ServiceResponse.validation_error(
                errors={'ids': 'Invalid product IDs'},
                message="Validation failed",
            )

        restored = 0
        for pid in product_ids:
            result, _ = AdminProductService.restore_product(pid)
            if result.get('success'):
                restored += 1

        return ServiceResponse.success(
            data={'restored_count': restored},
            message=f"{restored} product(s) restored successfully",
        )
